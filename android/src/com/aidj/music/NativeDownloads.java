package com.aidj.music;

import android.app.DownloadManager;
import android.content.*;
import android.database.Cursor;
import android.os.Environment;
import org.json.*;
import java.io.*;
import java.util.*;

final class NativeDownloads {
    static File folder(Context c)throws IOException{
        File folder=c.getExternalFilesDir(Environment.DIRECTORY_MUSIC);
        if(folder==null||(!folder.isDirectory()&&!folder.mkdirs()))throw new IOException("Storage unavailable");return folder;
    }
    static File part(Context c,String md5)throws IOException{return new File(folder(c),md5+".part");}
    static boolean paused(Context c){return "true".equals(State.get(c).read("downloadPaused","false"));}
    static void markPaused(Context c,String reason,String error){State db=State.get(c);db.write("downloadPauseReason",reason);db.write("downloadError",error);db.write("downloadPaused","true");}
    private static void clearPause(Context c){State db=State.get(c);db.write("downloadPauseReason","");db.write("downloadError","");db.write("downloadScheduleError","");db.write("downloadPaused","false");DownloadRunner.phase="Подготавливаю загрузку";}
    static boolean hasWork(Context c){State db=State.get(c);return db.hasWork();}
    static synchronized void acceptImported(Context c,JSONObject track,File file)throws Exception{
        if(!State.verify(file,track))throw new Exception("Файл не прошёл проверку.");
        State db=State.get(c);String md5=track.getString("md5");
        if(db.file(md5)!=null){file.delete();return;}
        DownloadRunner.abortFile(md5);removeLegacy(c,db.download(md5));
        String extension="m4a".equals(track.optString("format"))?".m4a":".mp3";
        File destination=new File(file.getParentFile(),md5+"-import"+extension);
        if(destination.exists()&&!destination.delete())throw new IOException("Cannot save imported file");
        if(!file.renameTo(destination))throw new IOException("Cannot save imported file");
        db.imported(track,destination);part(c,md5).delete();
    }
    static void add(Context c,JSONArray tracks)throws Exception{enqueue(c,tracks,false);}
    static void addUser(Context c,JSONArray tracks)throws Exception{enqueue(c,tracks,true);}
    private static synchronized void enqueue(Context c,JSONArray tracks,boolean user)throws Exception{
        State db=State.get(c);boolean extend=hasWork(c);JSONArray targets=new JSONArray();Set<String> seen=new HashSet<>();
        if(extend)try{JSONArray old=new JSONArray(db.read("downloadTargets","[]"));for(int i=0;i<old.length();i++)if(seen.add(old.getString(i)))targets.put(old.getString(i));}catch(JSONException ignored){}
        if(extend)for(String status:new String[]{"active","pending","retry"}){JSONArray rows=db.list(status);for(int i=0;i<rows.length();i++){String md5=rows.optJSONObject(i).optJSONObject("track").optString("md5");if(seen.add(md5))targets.put(md5);}}
        db.queueAll(tracks);
        for(int i=0;i<tracks.length();i++){String digest=tracks.getJSONObject(i).getString("md5");if(seen.add(digest))targets.put(digest);}
        db.write("downloadTargets",targets.toString());if(user)clearPause(c);migrate(c);if(!paused(c)&&hasWork(c))DownloadBatch.request(c,user||MainActivity.isVisible());
    }
    private static void removeLegacy(Context c,JSONObject row){
        if(row!=null&&row.optLong("requestId")>0)((DownloadManager)c.getSystemService(Context.DOWNLOAD_SERVICE)).remove(row.optLong("requestId"));
    }
    static synchronized void migrate(Context c){
        State db=State.get(c);DownloadManager manager=(DownloadManager)c.getSystemService(Context.DOWNLOAD_SERVICE);
        for(String state:new String[]{"active","failed"}){
            JSONArray rows=db.list(state);
            for(int i=0;i<rows.length();i++){
                JSONObject row=rows.optJSONObject(i);long id=row.optLong("requestId");if(id<=0)continue;
                JSONObject track=row.optJSONObject("track");String md5=track.optString("md5");File source=new File(row.optString("path"));
                try(Cursor query=manager.query(new DownloadManager.Query().setFilterById(id))){
                    boolean complete=query!=null&&query.moveToFirst()&&query.getInt(query.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))==DownloadManager.STATUS_SUCCESSFUL;
                    if(complete&&State.verify(source,track)){db.update(md5,"ready",source.getAbsolutePath(),0,"");continue;}
                    // Keep an open descriptor before the old manager removes its file.
                    if(source.isFile())try(InputStream in=new FileInputStream(source)){
                        manager.remove(id);File target=part(c,md5);
                        if(!target.exists())try(OutputStream out=new FileOutputStream(target)){byte[] buffer=new byte[65536];long left=track.optLong("bytes");int n;while(left>0&&(n=in.read(buffer,0,(int)Math.min(left,buffer.length)))!=-1){out.write(buffer,0,n);left-=n;}}
                    }else manager.remove(id);
                    db.update(md5,"pending","",0,"");
                }catch(Exception ignored){removeLegacy(c,row);db.update(md5,"pending","",0,"");}
            }
        }
    }
    static synchronized void reconcile(Context c){migrate(c);if(!paused(c)&&hasWork(c))DownloadBatch.request(c,MainActivity.isVisible());}
    static synchronized void toggle(Context c){
        setPaused(c,!(paused(c)||(!hasWork(c)&&State.get(c).list("failed").length()>0)));
    }
    static synchronized void setPaused(Context c,boolean pause){
        State db=State.get(c);
        if(!pause){
            Set<String> requested=new HashSet<>();try{JSONArray ids=new JSONArray(db.read("downloadTargets","[]"));for(int i=0;i<ids.length();i++)requested.add(ids.optString(i));}catch(JSONException ignored){}
            JSONArray failed=db.list("failed");for(int i=0;i<failed.length();i++){JSONObject row=failed.optJSONObject(i);String md5=row.optJSONObject("track").optString("md5");if(requested.isEmpty()||requested.contains(md5))db.update(md5,"pending",row.optString("path"),0,"");}
            clearPause(c);DownloadBatch.request(c,true);
        }else{markPaused(c,"user","Пауза включена вручную.");DownloadBatch.stop(c);}
    }
    static synchronized void cancel(Context c){
        State db=State.get(c);markPaused(c,"cancelled","");DownloadBatch.stop(c);
        for(String status:new String[]{"active","pending","retry","failed"}){
            JSONArray rows=db.list(status);for(int i=0;i<rows.length();i++){
                JSONObject row=rows.optJSONObject(i);String md5=row.optJSONObject("track").optString("md5");removeLegacy(c,row);db.remove(md5);
                try{part(c,md5).delete();}catch(IOException ignored){}
            }
        }
        db.write("downloadTargets","[]");db.write("downloadError","");
    }
    private static String cachedSnapshot=null;private static long snapshotAt=0,snapshotGeneration=-1;
    static synchronized JSONObject snapshot(Context c)throws JSONException{
        State db=State.get(c);long clock=android.os.SystemClock.elapsedRealtime();
        if(cachedSnapshot!=null&&snapshotGeneration==db.generation&&clock-snapshotAt<1000)return new JSONObject(cachedSnapshot);
        JSONArray targets=new JSONArray(db.read("downloadTargets","[]"));Set<String> requested=new HashSet<>();for(int i=0;i<targets.length();i++)requested.add(targets.optString(i));
        JSONArray all=db.list(null),active=new JSONArray(),pending=new JSONArray(),retry=new JSONArray(),failed=new JSONArray();Map<String,JSONObject> rowsById=new HashMap<>();
        for(int i=0;i<all.length();i++){JSONObject row=all.optJSONObject(i);rowsById.put(row.optJSONObject("track").optString("md5"),row);String status=row.optString("status");
            if("active".equals(status))active.put(row);else if("pending".equals(status))pending.put(row);else if("retry".equals(status))retry.put(row);else if("failed".equals(status)&&(requested.isEmpty()||requested.contains(row.optJSONObject("track").optString("md5"))))failed.put(row);
        }
        JSONObject out=new JSONObject();out.put("active",active.length()>0?active.optJSONObject(0).optJSONObject("track"):JSONObject.NULL);
        JSONArray waiting=new JSONArray();for(JSONArray rows:new JSONArray[]{pending,retry,failed})for(int i=0;i<rows.length();i++)waiting.put(rows.optJSONObject(i).optJSONObject("track"));
        int done=0,total=0;long bytes=0,allBytes=0;
        for(int i=0;i<targets.length();i++){
            String md5=targets.optString(i);JSONObject row=rowsById.get(md5);if(row==null)continue;total++;JSONObject track=row.optJSONObject("track");long expected=track.optLong("bytes");allBytes+=expected;
            File ready=new File(row.optString("path"));if("ready".equals(row.optString("status"))&&ready.isFile()&&ready.length()==expected){done++;bytes+=expected;}else try{bytes+=Math.min(expected,part(c,md5).length());}catch(IOException ignored){}
        }
        if(total==0){done=db.files().length();total=done+active.length()+waiting.length();}
        String error=db.read("downloadError","");if(error.isEmpty()&&failed.length()>0&&!hasWork(c)){
            JSONObject first=failed.optJSONObject(0);String detail=first.optString("error");if(detail.isEmpty())detail="Не удалось скачать файл. Нажми «Повторить».";
            error=first.optJSONObject("track").optString("title","Песня")+": "+detail;
        }
        out.put("pending",waiting);out.put("completed",done);out.put("total",total);out.put("paused",paused(c));
        out.put("failedCount",failed.length());out.put("needsRetry",!hasWork(c)&&failed.length()>0);out.put("pauseReason",db.read("downloadPauseReason",""));
        out.put("error",error.isEmpty()?JSONObject.NULL:error);out.put("phase",DownloadRunner.phase);out.put("bytesDownloaded",bytes);out.put("bytesTotal",allBytes);
        out.put("percent",allBytes>0?(int)Math.min(100,bytes*100/allBytes):0);cachedSnapshot=out.toString();snapshotAt=clock;snapshotGeneration=db.generation;return out;
    }
    static JSONObject diagnostics(Context c)throws JSONException{
        State db=State.get(c);JSONObject result=new JSONObject();
        result.put("versionCode",BuildConfig.VERSION_CODE);result.put("sdk",android.os.Build.VERSION.SDK_INT);result.put("paused",paused(c));
        for(String key:new String[]{"PauseReason","Mode","ScheduleError","StopReason","LastHttp","LastCause"})result.put(Character.toLowerCase(key.charAt(0))+key.substring(1),db.read("download"+key,""));
        for(String status:new String[]{"active","pending","retry","failed","ready"})result.put(status,db.list(status).length());
        try{result.put("lastBytes",Long.parseLong(db.read("downloadLastBytes","0")));}catch(NumberFormatException ignored){}
        try{result.put("freeBytes",folder(c).getUsableSpace());}catch(IOException ignored){}
        return result;
    }
}
