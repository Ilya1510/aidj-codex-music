package com.aidj.music;

import android.app.DownloadManager;
import android.content.*;
import android.database.Cursor;
import android.net.Uri;
import android.os.Environment;
import org.json.*;
import java.io.File;

final class NativeDownloads {
    static synchronized void acceptImported(Context c,JSONObject track,File file)throws Exception{
        if(!State.verify(file,track))throw new Exception("Файл не прошёл проверку.");
        State db=State.get(c);String md5=track.getString("md5");
        if(db.file(md5)!=null){file.delete();return;}
        JSONObject old=db.download(md5);
        if(old!=null&&old.optLong("requestId")>0)((DownloadManager)c.getSystemService(Context.DOWNLOAD_SERVICE)).remove(old.optLong("requestId"));
        String extension="m4a".equals(track.optString("format"))?".m4a":".mp3";
        File destination=new File(file.getParentFile(),md5+"-import"+extension);
        if(destination.exists()&&!destination.delete())throw new Exception("Не удалось сохранить файл.");
        if(!file.renameTo(destination))throw new Exception("Не удалось сохранить файл.");
        db.imported(track,destination);
    }
    static synchronized void add(Context c,JSONArray tracks)throws Exception{
        State db=State.get(c);for(int i=0;i<tracks.length();i++)db.queue(tracks.getJSONObject(i));db.write("downloadPaused","false");reconcile(c);
    }
    static synchronized void reconcile(Context c){
        State db=State.get(c);DownloadManager dm=(DownloadManager)c.getSystemService(Context.DOWNLOAD_SERVICE);
        JSONArray active=db.list("active");
        for(int i=0;i<active.length();i++){
            JSONObject row=active.optJSONObject(i),track=row.optJSONObject("track");String md5=track.optString("md5");
            try(Cursor cursor=dm.query(new DownloadManager.Query().setFilterById(row.optLong("requestId")))){
                if(cursor==null||!cursor.moveToFirst()){db.update(md5,"pending",row.optString("path"),0,"");continue;}
                int status=cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
                if(status==DownloadManager.STATUS_SUCCESSFUL){
                    File file=new File(row.optString("path"));
                    if(State.verify(file,track))db.update(md5,"ready",file.getAbsolutePath(),row.optLong("requestId"),"");
                    else{file.delete();db.update(md5,"failed","",0,"Файл не прошёл проверку. Нажми «Продолжить». ");db.write("downloadPaused","true");}
                }else if(status==DownloadManager.STATUS_FAILED){db.update(md5,"failed",row.optString("path"),row.optLong("requestId"),"Загрузка прервалась. Проверь сеть или подключение к боту.");db.write("downloadPaused","true");}
                else return;
            }catch(Exception e){return;}
        }
        if("true".equals(db.read("downloadPaused","false")))return;
        JSONArray pending=db.list("pending");if(pending.length()==0)return;
        JSONObject track=pending.optJSONObject(0).optJSONObject("track");String md5=track.optString("md5");
        try{
            File folder=c.getExternalFilesDir(Environment.DIRECTORY_MUSIC);if(folder==null)throw new Exception();folder.mkdirs();
            String extension="m4a".equals(track.optString("format"))?".m4a":".mp3";File file=new File(folder,md5+extension);
            if(file.exists()&&State.verify(file,track)){db.update(md5,"ready",file.getAbsolutePath(),0,"");reconcile(c);return;}
            if(file.exists())file.delete();
            DownloadManager.Request request=new DownloadManager.Request(Uri.parse(Api.base(c)+"/api/audio/"+track.getString("id")));
            request.addRequestHeader("Cookie",Api.cookie(c));request.setTitle(track.optString("artist")+" — "+track.optString("title"));request.setDescription("AI DJ");
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE);request.setAllowedOverRoaming(false);request.setDestinationUri(Uri.fromFile(file));
            long id=dm.enqueue(request);db.update(md5,"active",file.getAbsolutePath(),id,"");
        }catch(Exception e){db.update(md5,"failed","",0,"Не удалось начать загрузку. Проверь подключение и свободное место.");db.write("downloadPaused","true");}
    }
    static synchronized void toggle(Context c){
        State db=State.get(c);boolean paused="true".equals(db.read("downloadPaused","false"));db.write("downloadPaused",paused?"false":"true");
        if(paused){JSONArray failed=db.list("failed");for(int i=0;i<failed.length();i++){JSONObject r=failed.optJSONObject(i);JSONObject t=r.optJSONObject("track");db.update(t.optString("md5"),"pending",r.optString("path"),0,"");}reconcile(c);}
    }
    static synchronized void cancel(Context c){
        State db=State.get(c);db.write("downloadPaused","true");DownloadManager dm=(DownloadManager)c.getSystemService(Context.DOWNLOAD_SERVICE);
        for(String status:new String[]{"active","pending","failed"}){JSONArray rows=db.list(status);for(int i=0;i<rows.length();i++){JSONObject r=rows.optJSONObject(i);if(r.optLong("requestId")>0)dm.remove(r.optLong("requestId"));db.remove(r.optJSONObject("track").optString("md5"));}}
    }
    static synchronized JSONObject snapshot(Context c)throws JSONException{
        State db=State.get(c);JSONArray active=db.list("active"),pending=db.list("pending"),failed=db.list("failed");int done=db.files().length();
        JSONObject out=new JSONObject();out.put("active",active.length()>0?active.optJSONObject(0).optJSONObject("track"):JSONObject.NULL);
        JSONArray waiting=new JSONArray();for(int i=0;i<pending.length();i++)waiting.put(pending.optJSONObject(i).optJSONObject("track"));for(int i=0;i<failed.length();i++)waiting.put(failed.optJSONObject(i).optJSONObject("track"));
        out.put("pending",waiting);out.put("completed",done);out.put("total",done+active.length()+waiting.length());out.put("paused","true".equals(db.read("downloadPaused","false")));
        out.put("error",failed.length()>0?failed.optJSONObject(0).optString("error"):JSONObject.NULL);return out;
    }
}
