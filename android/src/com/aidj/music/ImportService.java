package com.aidj.music;

import android.app.*;
import android.content.*;
import android.os.*;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.*;
import java.util.concurrent.*;
import javax.crypto.*;

/** Explicit, short-lived, encrypted transfer over phone loopback only. */
public final class ImportService extends Service {
    static ImportService instance;
    private final ExecutorService worker=Executors.newFixedThreadPool(2);
    private final Handler handler=new Handler(Looper.getMainLooper());
    private final Map<String,JSONObject> manifest=new ConcurrentHashMap<>();
    private ServerSocket listener;private TransferCrypto crypto;
    private String token,origin;private volatile boolean running=true,ready=false;private volatile int copied=0;
    private volatile String error="";
    private final Runnable expire=()->stopSelf();
    @Override public void onCreate(){
        super.onCreate();instance=this;
        NotificationManager nm=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel("aidj-import","Перенос песен",NotificationManager.IMPORTANCE_LOW));
        Intent stop=new Intent(this,ImportService.class).setAction("stop");
        PendingIntent cancel=PendingIntent.getService(this,11,stop,PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n=new Notification.Builder(this,"aidj-import").setSmallIcon(R.drawable.ic_music).setContentTitle("Перенос песен из Mini App")
            .setContentText("Копирование внутри телефона").setOngoing(true).addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel,"Завершить",cancel).build()).build();
        startForeground(101,n);
        try{
            origin=Api.base(this);byte[] secret=new byte[32];new SecureRandom().nextBytes(secret);token=TransferCrypto.encode(secret);crypto=new TransferCrypto(token);
            listener=new ServerSocket(0,2,InetAddress.getByName("127.0.0.1"));
            worker.execute(this::serve);
        }catch(Exception e){error="Не удалось начать перенос.";stopSelf();}
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(intent!=null&&"stop".equals(intent.getAction())){stopSelf();return START_NOT_STICKY;}
        handler.removeCallbacks(expire);handler.postDelayed(expire,15*60*1000L);
        worker.execute(()->{
            try{
                JSONObject body=new JSONObject();body.put("port",listener.getLocalPort());body.put("token",token);
                JSONObject result=Api.request(this,"/api/import-register",body);
                if(result.optInt("status")!=200)throw new Exception();ready=true;error="";
            }catch(Exception e){error="Не удалось подключиться к серверу. Проверь сеть и вход в бота.";}
        });return START_NOT_STICKY;
    }
    @Override public IBinder onBind(Intent intent){return null;}
    @Override public void onTimeout(int startId,int type){stopSelf();}
    private void serve(){
        while(running){
            try(Socket socket=listener.accept()){
                socket.setSoTimeout(60000);handle(socket);
            }catch(Exception ignored){if(!running)break;}
        }
    }
    private String line(InputStream in)throws IOException{
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();int b;
        while((b=in.read())!=-1){if(b=='\n')break;if(b!='\r')bytes.write(b);if(bytes.size()>8192)throw new IOException("header too large");}
        return bytes.toString("UTF-8");
    }
    private void handle(Socket socket)throws Exception{
        BufferedInputStream in=new BufferedInputStream(socket.getInputStream());String[] first=line(in).split(" ");if(first.length!=3)return;
        String method=first[0],path=first[1];Map<String,String> headers=new HashMap<>();int count=0;
        for(String value;(value=line(in))!=null&&!value.isEmpty();){int colon=value.indexOf(':');if(colon<=0||++count>40)return;headers.put(value.substring(0,colon).toLowerCase(Locale.ROOT),value.substring(colon+1).trim());}
        String host="127.0.0.1:"+listener.getLocalPort();
        if(!origin.equals(headers.get("origin"))||!host.equals(headers.get("host"))){plain(socket,403);return;}
        if("OPTIONS".equals(method)){plain(socket,204);return;}
        if(!"POST".equals(method)){plain(socket,405);return;}
        if(!path.equals("/manifest")&&!path.equals("/finish")&&!path.matches("/file/[a-f0-9]{32}")){plain(socket,404);return;}
        long length;
        try{length=Long.parseLong(headers.get("content-length"));}catch(Exception e){plain(socket,411);return;}
        if(length<16||length>40L*1024*1024+16){plain(socket,413);return;}
        byte[] iv;
        try{iv=TransferCrypto.decode(headers.get("x-aidj-iv"));}catch(Exception e){plain(socket,403);return;}
        Cipher decrypt=crypto.cipher(Cipher.DECRYPT_MODE,iv,path);
        LimitedInputStream limited=new LimitedInputStream(in,length);
        try(InputStream body=new CipherInputStream(limited,decrypt)){
            if(path.equals("/manifest")){
                ByteArrayOutputStream bytes=new ByteArrayOutputStream();copy(body,bytes,2*1024*1024);
                JSONArray songs=new JSONObject(bytes.toString("UTF-8")).getJSONArray("tracks");if(songs.length()>5000)throw new IOException("too many tracks");
                manifest.clear();JSONArray missing=new JSONArray(),existing=new JSONArray();
                for(int i=0;i<songs.length();i++){
                    JSONObject track=songs.getJSONObject(i);String md5=track.getString("md5");long size=track.getLong("bytes");
                    if(!md5.matches("[a-f0-9]{32}")||!track.getString("id").matches("[a-f0-9]{24}")||size<=0||size>40L*1024*1024)throw new IOException("invalid track");
                    manifest.put(md5,track);
                    if(State.get(this).file(md5)!=null)existing.put(md5);else missing.put(md5);
                }
                JSONObject reply=new JSONObject();reply.put("missing",missing);reply.put("existing",existing);encrypted(socket,path,200,reply);
            }else if(path.equals("/finish")){
                copy(body,new ByteArrayOutputStream(),1024);
                JSONObject reply=new JSONObject();reply.put("ok",true);reply.put("copied",copied);encrypted(socket,path,200,reply);handler.postDelayed(()->stopSelf(),2000);
            }else{
                String md5=path.substring(6);JSONObject track=manifest.get(md5);if(track==null||length!=track.optLong("bytes")+16){plain(socket,400);return;}
                File directory=getExternalFilesDir(Environment.DIRECTORY_MUSIC);if(directory==null)throw new IOException("storage unavailable");directory.mkdirs();
                if(directory.getUsableSpace()<track.optLong("bytes")+10*1024*1024){JSONObject reply=new JSONObject();reply.put("error","На телефоне не хватает свободного места.");encrypted(socket,path,507,reply);return;}
                File temporary=new File(directory,md5+".import-part");
                try{
                    try(OutputStream out=new FileOutputStream(temporary)){copy(body,out,track.getLong("bytes"));}
                    NativeDownloads.acceptImported(this,track,temporary);copied++;
                    JSONObject reply=new JSONObject();reply.put("ok",true);reply.put("md5",md5);encrypted(socket,path,200,reply);
                }finally{if(temporary.exists())temporary.delete();}
            }
        }catch(Exception e){JSONObject reply=new JSONObject();reply.put("error","Файл не удалось перенести. Оригинал в Mini App сохранён.");encrypted(socket,path,400,reply);}
    }
    private void copy(InputStream in,OutputStream out,long limit)throws IOException{
        byte[] buffer=new byte[65536];int n;long total=0;
        while((n=in.read(buffer))!=-1){total+=n;if(total>limit)throw new IOException("file too large");out.write(buffer,0,n);}
    }
    private void common(OutputStream out,int status,long length,String nonce)throws IOException{
        String header="HTTP/1.1 "+status+" "+(status==200?"OK":status==204?"No Content":"Error")+"\r\nContent-Type: application/octet-stream\r\nContent-Length: "+length+"\r\nConnection: close\r\nCache-Control: no-store\r\nAccess-Control-Allow-Origin: "+origin+"\r\nAccess-Control-Allow-Methods: POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type, X-AIDJ-IV\r\nAccess-Control-Expose-Headers: X-AIDJ-IV\r\nAccess-Control-Allow-Private-Network: true\r\nAccess-Control-Max-Age: 600\r\n";
        if(nonce!=null)header+="X-AIDJ-IV: "+nonce+"\r\n";out.write((header+"\r\n").getBytes(StandardCharsets.UTF_8));
    }
    private void plain(Socket socket,int status)throws IOException{common(socket.getOutputStream(),status,0,null);socket.getOutputStream().flush();}
    private void encrypted(Socket socket,String path,int status,JSONObject body)throws Exception{
        byte[] iv=crypto.nonce();byte[] bytes=crypto.cipher(Cipher.ENCRYPT_MODE,iv,"reply:"+path).doFinal(body.toString().getBytes(StandardCharsets.UTF_8));
        OutputStream out=socket.getOutputStream();common(out,status,bytes.length,TransferCrypto.encode(iv));out.write(bytes);out.flush();
    }
    JSONObject snapshot(){JSONObject out=new JSONObject();try{out.put("ready",ready);out.put("copied",copied);out.put("error",error);}catch(Exception ignored){}return out;}
    @Override public void onDestroy(){running=false;ready=false;handler.removeCallbacks(expire);try{if(listener!=null)listener.close();}catch(Exception ignored){}worker.shutdownNow();instance=null;super.onDestroy();}
    private static final class LimitedInputStream extends InputStream {
        private final InputStream in;private long remaining;
        LimitedInputStream(InputStream in,long length){this.in=in;remaining=length;}
        @Override public int read()throws IOException{if(remaining==0)return -1;int b=in.read();if(b<0)throw new EOFException();remaining--;return b;}
        @Override public int read(byte[] b,int offset,int length)throws IOException{if(remaining==0)return -1;int n=in.read(b,offset,(int)Math.min(length,remaining));if(n<0)throw new EOFException();remaining-=n;return n;}
    }
}
