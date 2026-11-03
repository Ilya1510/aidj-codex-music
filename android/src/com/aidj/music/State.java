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
    private State(Context c){super(c,"music.sqlite",null,1);}
    public void onCreate(SQLiteDatabase db){
        db.execSQL("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)");
        db.execSQL("CREATE TABLE downloads(md5 TEXT PRIMARY KEY,track TEXT NOT NULL,path TEXT NOT NULL DEFAULT '',request_id INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '')");
    }
    public void onUpgrade(SQLiteDatabase db,int old,int next){}
    synchronized String read(String key,String fallback){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT v FROM meta WHERE k=?",new String[]{key})){return c.moveToFirst()?c.getString(0):fallback;}
    }
    synchronized void write(String key,String value){ContentValues v=new ContentValues();v.put("k",key);v.put("v",value);getWritableDatabase().insertWithOnConflict("meta",null,v,SQLiteDatabase.CONFLICT_REPLACE);}
    synchronized JSONObject download(String md5){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT track,path,request_id,status,error FROM downloads WHERE md5=?",new String[]{md5})){
            if(!c.moveToFirst())return null;
            JSONObject o=new JSONObject();o.put("track",new JSONObject(c.getString(0)));o.put("path",c.getString(1));o.put("requestId",c.getLong(2));o.put("status",c.getString(3));o.put("error",c.getString(4));return o;
        }catch(JSONException e){return null;}
    }
    synchronized void queue(JSONObject track)throws JSONException{
        String key=track.getString("md5");if(!key.matches("[a-f0-9]{32}")||!track.getString("id").matches("[a-f0-9]{24}"))throw new JSONException("invalid track");
        long bytes=track.getLong("bytes");if(bytes<=0||bytes>40L*1024*1024)throw new JSONException("invalid size");
        JSONObject old=download(key);if(old!=null&&("active".equals(old.optString("status"))||"pending".equals(old.optString("status"))||file(key)!=null))return;
        ContentValues v=new ContentValues();v.put("md5",key);v.put("track",track.toString());v.put("status","pending");v.put("error","");v.put("request_id",0);
        getWritableDatabase().insertWithOnConflict("downloads",null,v,SQLiteDatabase.CONFLICT_REPLACE);
    }
    synchronized void update(String md5,String status,String path,long requestId,String error){
        ContentValues v=new ContentValues();v.put("status",status);v.put("path",path);v.put("request_id",requestId);v.put("error",error);getWritableDatabase().update("downloads",v,"md5=?",new String[]{md5});
    }
    synchronized File file(String md5){
        JSONObject row=download(md5);if(row==null||!"ready".equals(row.optString("status")))return null;
        File f=new File(row.optString("path"));if(!f.isFile()||f.length()!=row.optJSONObject("track").optLong("bytes"))return null;return f;
    }
    synchronized JSONArray list(String status){
        JSONArray result=new JSONArray();
        try(Cursor c=getReadableDatabase().rawQuery("SELECT md5 FROM downloads WHERE status=? ORDER BY rowid",new String[]{status})){
            while(c.moveToNext()){JSONObject row=download(c.getString(0));if(row!=null)result.put(row);}
        }return result;
    }
    synchronized JSONArray files(){
        JSONArray result=new JSONArray();JSONArray rows=list("ready");
        for(int i=0;i<rows.length();i++){JSONObject row=rows.optJSONObject(i);JSONObject track=row.optJSONObject("track");if(file(track.optString("md5"))!=null)result.put(track);}
        return result;
    }
    synchronized void remove(String md5){JSONObject row=download(md5);if(row!=null&&!row.optString("path").isEmpty())new File(row.optString("path")).delete();getWritableDatabase().delete("downloads","md5=?",new String[]{md5});}
    synchronized void imported(JSONObject track,File file)throws JSONException{
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
