package com.aidj.music;

import android.content.Context;
import android.net.*;
import org.json.*;
import java.io.*;
import java.net.*;
import java.util.concurrent.*;

/** One durable sequential queue, independent of WebView and Activity lifetime. */
final class DownloadRunner {
    interface Listener {void progress();void finished(boolean remaining);}
    static volatile String phase="";
    private static final ExecutorService worker=Executors.newSingleThreadExecutor();
    private static volatile Token active;
    private static final ExecutorService disconnects=Executors.newSingleThreadExecutor();
    static final class Token implements DownloadTransfer.Control {
        volatile boolean stopped=false,skip=false;volatile String md5="";volatile HttpURLConnection connection;volatile Network network;
        long lastProgress=0;Listener listener;
        public HttpURLConnection open(URL url)throws IOException{return (HttpURLConnection)(network!=null?network.openConnection(url):url.openConnection());}
        public boolean cancelled(){return stopped||skip;}
        public void connection(HttpURLConnection c){connection=c;}
        public void progress(long bytes){long now=android.os.SystemClock.elapsedRealtime();if(now-lastProgress>1000){lastProgress=now;listener.progress();}}
        void cancel(){stopped=true;disconnect();}
        void skip(){skip=true;disconnect();}
        private void disconnect(){HttpURLConnection c=connection;if(c!=null)disconnects.execute(c::disconnect);}
    }
    static void abortFile(String md5){Token token=active;if(token!=null&&md5.equals(token.md5))token.skip();}
    static void stop(){Token token=active;if(token!=null)token.cancel();}
    static boolean running(){return active!=null&&!active.stopped;}
    static void start(Context context,Token token,Listener listener){
        token.listener=listener;Context c=context.getApplicationContext();
        worker.execute(()->{
            if(token.stopped)return;active=token;
            try{run(c,token,listener);}catch(Exception ignored){phase="Ожидаю продолжения";}
            finally{
                if(active==token)active=null;
                if(!token.stopped)listener.finished(!NativeDownloads.paused(c)&&NativeDownloads.hasWork(c));
            }
        });
    }
    static boolean network(Context c){
        ConnectivityManager manager=(ConnectivityManager)c.getSystemService(Context.CONNECTIVITY_SERVICE);
        NetworkCapabilities network=manager.getNetworkCapabilities(manager.getActiveNetwork());
        return network!=null&&network.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
    }
    private static void run(Context c,Token token,Listener listener)throws Exception{
        State db=State.get(c);
        NativeDownloads.migrate(c);long lastAck=0;
        while(!token.stopped&&!NativeDownloads.paused(c)){
            if(!NativeDownloads.hasWork(c))break;
            if(!network(c)){phase="Жду подключения к сети";listener.progress();Thread.sleep(1000);continue;}
            JSONObject chosen=db.next(System.currentTimeMillis());
            if(chosen==null){phase="Повторю загрузку автоматически";listener.progress();Thread.sleep(1000);continue;}
            JSONObject track=chosen.getJSONObject("track");String md5=track.getString("md5");
            token.md5=md5;token.skip=false;File part=NativeDownloads.part(c,md5);
            try{
                if(db.file(md5)!=null){db.update(md5,"ready",db.file(md5).getAbsolutePath(),0,"");continue;}
                String extension="m4a".equals(track.optString("format"))?".m4a":".mp3";File destination=new File(NativeDownloads.folder(c),md5+extension);
                if(DownloadTransfer.verify(destination,track.getLong("bytes"),md5)){db.imported(track,destination);continue;}
                if(part.getParentFile().getUsableSpace()<Math.max(0,track.getLong("bytes")-part.length())+8L*1024*1024){
                    db.write("downloadError","Не хватает места на устройстве. Освободи место и нажми «Продолжить».");db.write("downloadPaused","true");break;
                }
                db.update(md5,"active",destination.getAbsolutePath(),0,"");phase="Скачиваю";listener.progress();
                URL url=new URL(Api.validate(Api.base(c))+"/api/audio/"+track.getString("id"));
                DownloadTransfer.fetch(url,Api.cookie(c),part,track.getLong("bytes"),md5,token);
                synchronized(NativeDownloads.class){
                    if(db.file(md5)!=null){part.delete();continue;}
                    if(token.cancelled()||NativeDownloads.paused(c)||db.download(md5)==null)continue;
                    if(destination.exists()&&!destination.delete())throw new IOException("Cannot replace file");
                    if(!part.renameTo(destination))throw new IOException("Cannot commit file");
                    db.imported(track,destination);
                }
                long ackNow=android.os.SystemClock.elapsedRealtime();if(ackNow-lastAck>10000){NativeInbox.request(c);lastAck=ackNow;}listener.progress();
            }catch(Exception error){
                if(token.stopped||token.skip||NativeDownloads.paused(c)||db.download(md5)==null)continue;
                int code=error instanceof DownloadTransfer.Failure?((DownloadTransfer.Failure)error).code:0;
                int attempts=chosen.optInt("attempts")+1;
                if(code==401||code==403){db.write("downloadError",DownloadPolicy.error(code));db.write("downloadPaused","true");break;}
                if(DownloadPolicy.permanent(code,attempts))db.update(md5,"failed",chosen.optString("path"),0,DownloadPolicy.error(code));
                else db.retry(md5,attempts,System.currentTimeMillis()+DownloadPolicy.retryDelay(attempts),"");
            }finally{if(db.download(md5)==null||db.file(md5)!=null)part.delete();token.md5="";}
        }
        phase=NativeDownloads.paused(c)?"Загрузка приостановлена":"";NativeInbox.request(c);listener.progress();
    }
}
