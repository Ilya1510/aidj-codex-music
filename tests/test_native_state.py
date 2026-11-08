"""Run real State metadata reads with a bounded Android CursorWindow fixture."""
from tests.test_native_downloads import compile_probe


def test_large_unicode_catalog_and_queue_read_without_oversized_cursor_rows(tmp_path):
    fixtures={
        'android/content/Context.java':'package android.content;public class Context{public Context getApplicationContext(){return this;}}',
        'android/content/ContentValues.java':'package android.content;public class ContentValues extends java.util.HashMap<String,Object>{}',
        'android/os/SystemClock.java':'package android.os;public class SystemClock{public static long elapsedRealtime(){return 0;}}',
        'android/database/Cursor.java':'''package android.database;public interface Cursor extends AutoCloseable{
            boolean moveToFirst();boolean moveToNext();String getString(int i);int getInt(int i);long getLong(int i);void close();}''',
        'android/database/sqlite/SQLiteOpenHelper.java':'''package android.database.sqlite;public abstract class SQLiteOpenHelper{
            private final SQLiteDatabase db=new SQLiteDatabase();public SQLiteOpenHelper(android.content.Context c,String n,Object f,int v){}
            public SQLiteDatabase getReadableDatabase(){return db;}public SQLiteDatabase getWritableDatabase(){return db;}
            public abstract void onCreate(SQLiteDatabase d);public abstract void onUpgrade(SQLiteDatabase d,int o,int n);}''',
        'android/database/sqlite/SQLiteDatabase.java':r'''package android.database.sqlite;
import java.util.*;import android.database.Cursor;import android.content.ContentValues;
public class SQLiteDatabase{
 public static final int CONFLICT_REPLACE=5;private final Map<String,String> meta=new HashMap<>();
 public void execSQL(String s){}public void beginTransaction(){}public void endTransaction(){}public void setTransactionSuccessful(){}
 public int update(String t,ContentValues v,String w,String[] a){return 0;}public int delete(String t,String w,String[] a){return 0;}
 public long insertWithOnConflict(String t,String n,ContentValues v,int c){meta.put((String)v.get("k"),(String)v.get("v"));return 1;}
 public Cursor rawQuery(String sql,String[] args){
  final boolean sliced=sql.startsWith("SELECT substr(v,");final String value=meta.get(args[args.length-1]);
  final int total=value==null?0:value.codePointCount(0,value.length());
  int start=sliced?Integer.parseInt(args[0])-1:0,end=sliced?Math.min(total,start+65536):total;
  final String row=value==null?null:value.substring(value.offsetByCodePoints(0,Math.min(start,total)),value.offsetByCodePoints(0,end));
  return new Cursor(){public boolean moveToFirst(){if(row!=null&&row.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>2*1024*1024)throw new IllegalStateException("Row too big to fit into CursorWindow");return row!=null;}
   public boolean moveToNext(){return false;}public String getString(int i){return row;}public int getInt(int i){return total;}public long getLong(int i){return total;}public void close(){}};
 }
}''',
        'com/aidj/music/StateProbe.java':r'''package com.aidj.music;
public class StateProbe{
 static void check(boolean ok,String s){if(!ok)throw new AssertionError(s);}
 public static void main(String[] a){State state=State.get(new android.content.Context());
  check(state.read("missing","fallback").equals("fallback"),"missing metadata");
  state.write("empty","");check(state.read("empty","fallback").equals(""),"empty differs from missing");
  state.write("session","small");check(state.read("session","").equals("small"),"small values retained");
  StringBuilder b=new StringBuilder();for(int i=0;i<700000;i++)b.append("ё🎧a");String large=b.toString();
  for(String key:new String[]{"web_catalog","nativeQueue"}){state.write(key,large);check(state.read(key,"").equals(large),"large Unicode value survives every slice: "+key);}
  System.out.println("STATE_PROBE_PASSED");
 }
}'''}
    compile_probe(tmp_path,fixtures,['State.java'],'StateProbe')
