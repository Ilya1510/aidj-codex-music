package com.aidj.music;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import org.json.*;
import java.io.*;
import java.security.MessageDigest;

final class State extends SQLiteOpenHelper {
    private static State instance;
    static synchronized State get(Context c) { if(instance==null)instance=new State(c.getApplicationContext());return instance; }
    volatile long generation=0;private String cachedFiles=null;private long filesCachedAt=0;
    private State(Context c){super(c,"music.sqlite",null,2);}
    public void onCreate(SQLiteDatabase db){
        db.execSQL("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)");
        db.execSQL("CREATE TABLE downloads(md5 TEXT PRIMARY KEY,track TEXT NOT NULL,path TEXT NOT NULL DEFAULT '',request_id INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,retry_at INTEGER NOT NULL DEFAULT 0)");
    }
    public void onUpgrade(SQLiteDatabase db,int old,int next){
        if(old<2){db.execSQL("ALTER TABLE downloads ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0");db.execSQL("ALTER TABLE downloads ADD COLUMN retry_at INTEGER NOT NULL DEFAULT 0");}
    }
    synchronized String read(String key,String fallback){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT v FROM meta WHERE k=?",new String[]{key})){return c.moveToFirst()?c.getString(0):fallback;}
    }
    synchronized void write(String key,String value){if(key.startsWith("download"))generation++;ContentValues v=new ContentValues();v.put("k",key);v.put("v",value);getWritableDatabase().insertWithOnConflict("meta",null,v,SQLiteDatabase.CONFLICT_REPLACE);}
    synchronized JSONObject download(String md5){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT track,path,request_id,status,error,attempts,retry_at FROM downloads WHERE md5=?",new String[]{md5})){
            if(!c.moveToFirst())return null;
            JSONObject o=new JSONObject();o.put("track",new JSONObject(c.getString(0)));o.put("path",c.getString(1));o.put("requestId",c.getLong(2));o.put("status",c.getString(3));o.put("error",c.getString(4));o.put("attempts",c.getInt(5));o.put("retryAt",c.getLong(6));return o;
        }catch(JSONException e){return null;}
    }
    synchronized void queue(JSONObject track)throws JSONException{
        String key=track.getString("md5");if(!key.matches("[a-f0-9]{32}")||!track.getString("id").matches("[a-f0-9]{24}"))throw new JSONException("invalid track");
        long bytes=track.getLong("bytes");if(bytes<=0||bytes>40L*1024*1024)throw new JSONException("invalid size");
        JSONObject old=download(key);if(old!=null&&("active".equals(old.optString("status"))||"pending".equals(old.optString("status"))||"retry".equals(old.optString("status"))||file(key)!=null))return;
        ContentValues v=new ContentValues();v.put("md5",key);v.put("track",track.toString());v.put("status","pending");v.put("error","");v.put("request_id",0);if(old!=null)v.put("path",old.optString("path"));
        generation++;getWritableDatabase().insertWithOnConflict("downloads",null,v,SQLiteDatabase.CONFLICT_REPLACE);
    }
    synchronized void queueAll(JSONArray tracks)throws JSONException{
        SQLiteDatabase db=getWritableDatabase();db.beginTransaction();
        try{for(int i=0;i<tracks.length();i++)queue(tracks.getJSONObject(i));db.setTransactionSuccessful();}finally{db.endTransaction();}
    }
    synchronized void retry(String md5,int attempts,long when,String error){
        ContentValues v=new ContentValues();v.put("status","retry");v.put("attempts",attempts);v.put("retry_at",when);v.put("error",error);v.put("request_id",0);
        generation++;getWritableDatabase().update("downloads",v,"md5=?",new String[]{md5});
    }
    synchronized void refreshFiles(){generation++;cachedFiles=null;}
    synchronized void update(String md5,String status,String path,long requestId,String error){
        generation++;cachedFiles=null;ContentValues v=new ContentValues();v.put("status",status);v.put("path",path);v.put("request_id",requestId);v.put("error",error);getWritableDatabase().update("downloads",v,"md5=?",new String[]{md5});
    }
    synchronized File file(String md5){
        JSONObject row=download(md5);if(row==null||!"ready".equals(row.optString("status")))return null;
        File f=new File(row.optString("path"));if(!f.isFile()||f.length()!=row.optJSONObject("track").optLong("bytes"))return null;return f;
    }
    private JSONObject row(Cursor c)throws JSONException{
        JSONObject value=new JSONObject();value.put("track",new JSONObject(c.getString(0)));value.put("path",c.getString(1));value.put("requestId",c.getLong(2));value.put("status",c.getString(3));value.put("error",c.getString(4));value.put("attempts",c.getInt(5));value.put("retryAt",c.getLong(6));return value;
    }
    synchronized boolean hasWork(){try(Cursor c=getReadableDatabase().rawQuery("SELECT 1 FROM downloads WHERE status IN ('active','pending','retry') LIMIT 1",null)){return c.moveToFirst();}}
    synchronized JSONObject next(long now){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT track,path,request_id,status,error,attempts,retry_at FROM downloads WHERE status IN ('active','pending') OR (status='retry' AND retry_at<=?) ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'retry' THEN 1 ELSE 2 END,rowid LIMIT 1",new String[]{String.valueOf(now)})){
            return c.moveToFirst()?row(c):null;
        }catch(JSONException ignored){return null;}
    }
    synchronized JSONArray list(String status){
        JSONArray result=new JSONArray();String sql="SELECT track,path,request_id,status,error,attempts,retry_at FROM downloads"+(status==null?"":" WHERE status=?")+" ORDER BY rowid";
        try(Cursor c=getReadableDatabase().rawQuery(sql,status==null?null:new String[]{status})){
            while(c.moveToNext())try{result.put(row(c));}catch(JSONException ignored){}
        }return result;
    }
    synchronized JSONArray files(){
        if(cachedFiles!=null&&android.os.SystemClock.elapsedRealtime()-filesCachedAt<5000)try{return new JSONArray(cachedFiles);}catch(JSONException ignored){}
        JSONArray result=new JSONArray();JSONArray rows=list("ready");
        for(int i=0;i<rows.length();i++){JSONObject row=rows.optJSONObject(i);JSONObject track=row.optJSONObject("track");File file=new File(row.optString("path"));if(file.isFile()&&file.length()==track.optLong("bytes"))result.put(track);}
        cachedFiles=result.toString();filesCachedAt=android.os.SystemClock.elapsedRealtime();return result;
    }
    synchronized void remove(String md5){generation++;cachedFiles=null;JSONObject row=download(md5);if(row!=null&&!row.optString("path").isEmpty())new File(row.optString("path")).delete();getWritableDatabase().delete("downloads","md5=?",new String[]{md5});}
    synchronized void imported(JSONObject track,File file)throws JSONException{generation++;cachedFiles=null;
        ContentValues value=new ContentValues();value.put("md5",track.getString("md5"));value.put("track",track.toString());value.put("path",file.getAbsolutePath());value.put("status","ready");value.put("request_id",0);value.put("error","");
        getWritableDatabase().insertWithOnConflict("downloads",null,value,SQLiteDatabase.CONFLICT_REPLACE);
    }
    static boolean verify(File file,JSONObject track){
        if(file==null||file.length()!=track.optLong("bytes"))return false;
        try(InputStream in=new FileInputStream(file)){
            MessageDigest hash=MessageDigest.getInstance("MD5");byte[] buffer=new byte[65536];int n;
            while((n=in.read(buffer))!=-1)hash.update(buffer,0,n);
            StringBuilder hex=new StringBuilder();for(byte b:hash.digest())hex.append(String.format("%02x",b&255));return hex.toString().equals(track.optString("md5"));
        }catch(Exception e){return false;}
    }
}
