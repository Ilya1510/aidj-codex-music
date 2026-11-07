package com.aidj.music;

import android.content.Context;
import android.net.ConnectivityManager;
import org.json.*;
import java.io.*;
import java.nio.file.*;
import java.security.*;
import java.util.*;
import java.util.concurrent.*;

final class State {
    static final State instance=new State();final Map<String,JSONObject> rows=new LinkedHashMap<>();final Map<String,String> meta=new HashMap<>();
    static State get(Context c){return instance;}
    synchronized String read(String key,String fallback){return meta.getOrDefault(key,fallback);}
    synchronized void write(String key,String value){meta.put(key,value);}
    synchronized JSONObject download(String md5){return rows.get(md5);}
    synchronized JSONArray list(String status){JSONArray list=new JSONArray();for(JSONObject row:rows.values())if(status.equals(row.optString("status")))list.put(row);return list;}
    synchronized JSONObject next(long now){
        for(String status:new String[]{"active","retry","pending"})for(JSONObject row:rows.values())if(status.equals(row.optString("status"))&&(!"retry".equals(status)||row.optLong("retryAt")<=now))return row;
        return null;
    }
    synchronized File file(String md5){JSONObject r=rows.get(md5);if(r==null||!"ready".equals(r.optString("status")))return null;File file=new File(r.optString("path"));return file.isFile()?file:null;}
    synchronized void update(String md5,String status,String path,long request,String error){JSONObject row=rows.get(md5);if(row!=null){row.put("status",status);row.put("path",path);row.put("error",error);}}
    synchronized void retry(String md5,int attempts,long when,String error){JSONObject row=rows.get(md5);if(row!=null){row.put("status","retry");row.put("attempts",attempts);row.put("retryAt",when);}}
    synchronized void imported(JSONObject track,File file){JSONObject row=rows.get(track.optString("md5"));row.put("path",file.getAbsolutePath());row.put("status","ready");}
    synchronized JSONObject add(char id,byte[] content)throws Exception{
        MessageDigest md=MessageDigest.getInstance("MD5");StringBuilder hash=new StringBuilder();for(byte b:md.digest(content))hash.append(String.format("%02x",b&255));
        JSONObject track=new JSONObject().put("md5",hash.toString()).put("bytes",content.length).put("id",String.join("",Collections.nCopies(24,String.valueOf(id)))).put("format","mp3");
        rows.put(hash.toString(),new JSONObject().put("track",track).put("status","pending").put("path","").put("attempts",0));return track;
    }
}
final class NativeDownloads {
    static File root;
    static File folder(Context c){return root;}
    static File part(Context c,String md5){return new File(root,md5+".part");}
    static boolean paused(Context c){return "true".equals(State.instance.read("downloadPaused","false"));}
    static void markPaused(Context c,String reason,String message){State.instance.write("downloadPauseReason",reason);State.instance.write("downloadError",message);State.instance.write("downloadPaused","true");}
    static boolean hasWork(Context c){return State.instance.list("active").length()+State.instance.list("pending").length()+State.instance.list("retry").length()>0;}
    static void migrate(Context c){}
}
final class NativeInbox {static int acknowledgements=0;static void request(Context c){acknowledgements++;}}
final class Api {static String server;static int session=401;static String base(Context c){return server;}static String validate(String value){return value;}static String cookie(Context c){return "aidj_session=test-only";}static JSONObject request(Context c,String path,JSONObject body){return new JSONObject().put("status",session);}}

public final class DownloadRunnerProbe {
    static class Events implements DownloadRunner.Listener {
        final CountDownLatch done=new CountDownLatch(1);boolean remaining;
        public void progress(){}
        public void finished(boolean value){remaining=value;done.countDown();}
        void await()throws Exception{check(done.await(12,TimeUnit.SECONDS),"queue must finish");}
    }
    static void check(boolean condition,String text){if(!condition)throw new AssertionError(text);}
    static byte[] data(char letter,int count){byte[] bytes=new byte[count];Arrays.fill(bytes,(byte)letter);return bytes;}
    static DownloadRunner.Token run(Context c,Events events){DownloadRunner.Token token=new DownloadRunner.Token();DownloadRunner.start(c,token,events);return token;}
    static void waitFor(java.util.function.BooleanSupplier predicate,String label)throws Exception{long until=System.currentTimeMillis()+5000;while(!predicate.getAsBoolean()&&System.currentTimeMillis()<until)Thread.sleep(20);check(predicate.getAsBoolean(),label);}
    public static void main(String[] args)throws Exception{
        try{
            Context c=new Context();NativeDownloads.root=new File(args[1]);Api.server=args[0];State db=State.instance;
            JSONObject a=db.add('a',data('A',300000)),b=db.add('b',data('B',200000)),missing=db.add('c',data('C',100000));
            Events first=new Events();run(c,first);first.await();
            check(!first.remaining,"transient retry must be automatic");
            check(db.file(a.getString("md5"))!=null&&db.file(b.getString("md5"))!=null,"other songs finish even after a 404");
            check("failed".equals(db.download(missing.getString("md5")).optString("status")),"permanent failure stays per song");
            check(!NativeDownloads.paused(c),"one unavailable file must not pause the batch");
            check(NativeInbox.acknowledgements>0,"verified files are acknowledged");
            JSONObject d=db.add('d',data('D',1000000));String digest=d.getString("md5");File part=NativeDownloads.part(c,digest);
            Events interrupted=new Events();DownloadRunner.Token token=run(c,interrupted);
            waitFor(()->part.length()>65536,"download must start before locking/stopping the owner job");
            db.write("downloadPaused","true");token.cancel();waitFor(()->!DownloadRunner.running(),"cancel returns control promptly");
            long kept=part.length();check(kept>0&&kept<1000000,"partial bytes persist");
            db.write("downloadPaused","false");Events resumed=new Events();run(c,resumed);resumed.await();
            check(db.file(digest)!=null,"rescheduled job resumes the interrupted file");
            check(DownloadTransfer.verify(db.file(digest),1000000,digest),"only verified complete bytes become ready");
            int before=NativeInbox.acknowledgements;
            db.update(digest,"pending","",0,"");Events cached=new Events();run(c,cached);cached.await();
            check(db.file(digest)!=null,"existing verified destination is reused");
            JSONObject e=db.add('e',data('E',50000));ConnectivityManager.online=false;
            Events offline=new Events();run(c,offline);waitFor(()->DownloadRunner.phase.contains("сети"),"offline status");
            check(db.download(e.getString("md5")).optInt("attempts")==0,"offline waiting must not consume retry attempts");
            ConnectivityManager.online=true;offline.await();check(db.file(e.getString("md5"))!=null,"network restoration resumes without user interaction");
            db.add('f',data('F',1000));Events auth=new Events();run(c,auth);auth.await();
            check(NativeDownloads.paused(c)&&db.read("downloadError","").contains("подключение"),"expired auth has an actionable status instead of retry spam");
            db.write("downloadPaused","false");db.rows.remove(db.rows.values().stream().filter(r->r.getJSONObject("track").getString("id").startsWith("f")).findFirst().get().getJSONObject("track").getString("md5"));
            Api.session=200;JSONObject g=db.add('g',data('G',10000)),h=db.add('h',data('H',10000));
            Events proxy=new Events();run(c,proxy);proxy.await();
            check(!NativeDownloads.paused(c)&&db.file(g.getString("md5"))!=null&&db.file(h.getString("md5"))!=null,"one audio 403 with valid session retries without pausing other songs");
            JSONObject vpnSong=db.add('i',data('I',10000));Events vpn=new Events();DownloadRunner.Token stale=new DownloadRunner.Token();
            stale.network=new android.net.Network(){@Override public java.net.URLConnection openConnection(java.net.URL u)throws java.io.IOException{throw new java.net.UnknownHostException("stale VPN route");}};
            DownloadRunner.start(c,stale,vpn);waitFor(()->"dns".equals(db.read("downloadLastCause","")),"transport cause must be classified");
            check(stale.network==null&&DownloadRunner.phase.contains("сервер"),"stale route is discarded and the batch waits instead of spinning through titles");
            vpn.await();check(db.file(vpnSong.getString("md5"))!=null,"download recovers through the current route after VPN changes");
            check(NetworkStatus.snapshot().getBoolean("reachable"),"successful download clears unavailable server evidence");
            System.out.println("RUNNER_PASSED retry remaining_files pause resume cached offline auth verified_ack");System.exit(0);
        }catch(Throwable error){System.err.println(error.getClass().getSimpleName()+": "+error.getMessage());System.exit(1);}
    }
}
