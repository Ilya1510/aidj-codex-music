"""Run the real NativeDownloads facade with Android/SQLite boundary fixtures."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import urllib.request

import pytest

ROOT=Path(__file__).resolve().parents[1]


def compile_probe(tmp_path,fixtures,sources,probe):
    tools=Path.home()/'.local/share/aidj-codex-music/android-tools'
    javac=str(tools/'jdk17/bin/javac') if (tools/'jdk17/bin/javac').exists() else shutil.which('javac')
    java=str(tools/'jdk17/bin/java') if (tools/'jdk17/bin/java').exists() else shutil.which('java')
    if not javac or not java:pytest.skip('JDK required')
    jar=tools/'test-json/json.jar'
    if not jar.exists():
        jar=tmp_path/'json.jar'
        with urllib.request.urlopen('https://repo.maven.apache.org/maven2/org/json/json/20240303/json-20240303.jar',timeout=20) as r:jar.write_bytes(r.read(256*1024))
    assert hashlib.sha256(jar.read_bytes()).hexdigest()=='3cf6cd6892e32e2b4c1c39e0f52f5248a2f5b37646fdfbb79a66b46b618414ed'
    files=[]
    for name,source in fixtures.items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(source);files.append(str(path))
    files.extend(str(ROOT/'android/src/com/aidj/music'/name) for name in sources)
    subprocess.run([javac,'-J-Xmx128m','-J-XX:ActiveProcessorCount=2','--release','8','-classpath',str(jar),'-d',str(tmp_path),*files],check=True,capture_output=True,timeout=30)
    r=subprocess.run([java,'-Xmx64m','-XX:ActiveProcessorCount=2','-classpath',str(tmp_path)+':'+str(jar),'com.aidj.music.'+probe,str(tmp_path)],capture_output=True,text=True,timeout=15)
    assert r.returncode==0,r.stdout+r.stderr
    assert 'PROBE_PASSED' in r.stdout


def test_native_resume_is_idempotent_background_inbox_preserves_pause_and_failures_are_not_pauses(tmp_path):
    fixtures={
        'android/content/Context.java':'''package android.content;public class Context {
            public static final String DOWNLOAD_SERVICE="download";public static java.io.File root;
            public Context getApplicationContext(){return this;}public Object getSystemService(String key){return new android.app.DownloadManager();}
            public java.io.File getExternalFilesDir(String type){return root;}}''',
        'android/app/DownloadManager.java':'''package android.app;public class DownloadManager {
            public static final String COLUMN_STATUS="status";public static final int STATUS_SUCCESSFUL=8;
            public static class Query{public Query setFilterById(long id){return this;}}
            public int remove(long id){return 1;}public android.database.Cursor query(Query q){return null;}}''',
        'android/database/Cursor.java':'''package android.database;public interface Cursor extends AutoCloseable {
            boolean moveToFirst();int getInt(int column);int getColumnIndexOrThrow(String key);void close();}''',
        'android/os/Environment.java':'package android.os;public class Environment{public static final String DIRECTORY_MUSIC="Music";}',
        'android/os/SystemClock.java':'package android.os;public class SystemClock{public static long elapsedRealtime(){return System.nanoTime()/1000000;}}',
        'android/os/Build.java':'package android.os;public class Build{public static class VERSION{public static int SDK_INT=35;}}',
        'com/aidj/music/NativeDownloadsProbe.java':r'''package com.aidj.music;
import android.content.Context;import org.json.*;import java.io.*;import java.util.*;
final class State {
    static final State instance=new State();long generation=0;
    final Map<String,JSONObject> rows=new LinkedHashMap<>();final Map<String,String> meta=new HashMap<>();
    static State get(Context c){return instance;}
    String read(String key,String fallback){return meta.getOrDefault(key,fallback);}
    void write(String key,String value){generation++;meta.put(key,value);}
    JSONObject download(String id){return rows.get(id);}
    JSONArray list(String status){JSONArray out=new JSONArray();for(JSONObject r:rows.values())if(status==null||status.equals(r.optString("status")))out.put(r);return out;}
    boolean hasWork(){return list("active").length()+list("pending").length()+list("retry").length()>0;}
    void queueAll(JSONArray tracks){for(int i=0;i<tracks.length();i++){
        JSONObject t=tracks.getJSONObject(i);String key=t.getString("md5");if(file(key)!=null||rows.containsKey(key)&&!"failed".equals(rows.get(key).optString("status")))continue;
        rows.put(key,new JSONObject().put("track",t).put("status","pending").put("path","").put("requestId",0).put("error",""));generation++;
    }}
    File file(String key){JSONObject r=rows.get(key);if(r==null||!"ready".equals(r.optString("status")))return null;File f=new File(r.optString("path"));return f.isFile()?f:null;}
    JSONArray files(){JSONArray a=new JSONArray();for(String key:rows.keySet())if(file(key)!=null)a.put(rows.get(key).getJSONObject("track"));return a;}
    void update(String key,String status,String path,long requestId,String error){rows.get(key).put("status",status).put("path",path).put("requestId",requestId).put("error",error);generation++;}
    void imported(JSONObject track,File f){rows.put(track.getString("md5"),new JSONObject().put("track",track).put("status","ready").put("path",f.getAbsolutePath()));generation++;}
    void remove(String key){rows.remove(key);generation++;}static boolean verify(File f,JSONObject t){return f.isFile()&&f.length()==t.optLong("bytes");}
}
final class DownloadRunner{static String phase="";static void abortFile(String id){}}
final class DownloadBatch{static int requests=0,stops=0;static void request(Context c,boolean user){requests++;}static void stop(Context c){stops++;}}
final class MainActivity{static boolean isVisible(){return true;}}
final class BuildConfig{static final int VERSION_CODE=16;}
public final class NativeDownloadsProbe {
    static void check(boolean v,String label){if(!v)throw new AssertionError(label);}
    static JSONObject track(char c){return new JSONObject().put("id",String.join("",Collections.nCopies(24,""+c))).put("md5",String.join("",Collections.nCopies(32,""+c))).put("bytes",3).put("title","Song "+c);}
    public static void main(String[] args)throws Exception{
        Context.root=new File(args[0]);Context c=new Context();State db=State.instance;
        JSONObject a=track('a'),b=track('b'),other=track('c');JSONArray pair=new JSONArray().put(a).put(b);
        NativeDownloads.addUser(c,pair);NativeDownloads.setPaused(c,true);
        check(NativeDownloads.paused(c),"manual pause persists");int before=DownloadBatch.requests;
        NativeDownloads.reconcile(c);NativeDownloads.add(c,new JSONArray().put(other));
        check(NativeDownloads.paused(c)&&DownloadBatch.requests==before,"background inbox must not undo a user's pause");
        NativeDownloads.setPaused(c,false);NativeDownloads.setPaused(c,false);
        check(!NativeDownloads.paused(c),"two Resume requests must never pause again");
        check(DownloadBatch.stops==1,"Resume must not call Stop");
        NativeDownloads.markPaused(c,"system","Android stopped it");
        check("system".equals(NativeDownloads.snapshot(c).getString("pauseReason")),"system cause must be distinct from manual pause");
        NativeDownloads.addUser(c,pair);check(!NativeDownloads.paused(c),"new explicit download clears old stop");
        File ready=new File(args[0],"ready.mp3");try(FileOutputStream out=new FileOutputStream(ready)){out.write(new byte[]{1,2,3});}
        db.update(a.getString("md5"),"failed","",0,"");db.imported(b,ready);
        db.update(other.getString("md5"),"failed","",0,"unrelated failure");
        db.write("downloadTargets",new JSONArray().put(a.getString("md5")).put(b.getString("md5")).toString());
        JSONObject state=NativeDownloads.snapshot(c);
        check(!state.getBoolean("paused")&&state.getBoolean("needsRetry"),"failed files are not a manually paused batch");
        check(state.getInt("failedCount")==1&&state.getInt("completed")==1&&state.getInt("total")==2,"failure/counts are scoped to this batch");
        check(state.getString("error").contains("Song a")&&state.getString("error").contains("Повторить"),"legacy blank failure must have a meaningful reason");
        NativeDownloads.setPaused(c,false);check("pending".equals(db.download(a.getString("md5")).getString("status")),"retry requested failure");
        check("failed".equals(db.download(other.getString("md5")).getString("status")),"do not revive unrelated old failures");
        check(db.file(b.getString("md5"))!=null,"ready file remains downloaded");
        JSONObject diag=NativeDownloads.diagnostics(c);check(diag.getInt("versionCode")==16&&diag.getInt("pending")==1,"diagnostics include exact installed version and durable queue");
        System.out.println("PROBE_PASSED native_resume pause_causes inbox failure_scope cache");
    }
}''',
    }
    compile_probe(tmp_path,fixtures,['NativeDownloads.java'],'NativeDownloadsProbe')


def test_job_stop_does_not_pause_a_new_foreground_service_or_turn_network_loss_into_manual_pause(tmp_path):
    fixtures={
        'android/content/Context.java':'package android.content;public class Context{public static final String NOTIFICATION_SERVICE="notification";public Object getSystemService(String key){return new android.app.NotificationManager();}}',
        'android/app/NotificationManager.java':'package android.app;public class NotificationManager{public void cancel(int id){}}',
        'android/net/Network.java':'package android.net;public class Network{}',
        'android/os/Build.java':'package android.os;public class Build{public static class VERSION{public static int SDK_INT=35;}}',
        'android/os/Looper.java':'package android.os;public class Looper{public static Looper getMainLooper(){return new Looper();}}',
        'android/os/Handler.java':'package android.os;public class Handler{public Handler(Looper l){}public void post(Runnable r){r.run();}}',
        'android/app/job/JobParameters.java':'''package android.app.job;public class JobParameters {
            public static final int STOP_REASON_USER=13;public int reason;public JobParameters(int value){reason=value;}
            public int getStopReason(){return reason;}public android.net.Network getNetwork(){return new android.net.Network();}public boolean isUserInitiatedJob(){return false;}}''',
        'android/app/job/JobService.java':'''package android.app.job;public class JobService extends android.content.Context {
            public static final int JOB_END_NOTIFICATION_POLICY_DETACH=0;public void setNotification(JobParameters p,int id,Object n,int policy){}
            public void jobFinished(JobParameters p,boolean again){}public boolean onStartJob(JobParameters p){return false;}public boolean onStopJob(JobParameters p){return false;}public void onDestroy(){}}''',
        'com/aidj/music/JobStopProbe.java':r'''package com.aidj.music;
import android.content.Context;import android.app.job.JobParameters;import java.util.*;
final class State {static final State db=new State();Map<String,String> values=new HashMap<>();static State get(Context c){return db;}void write(String key,String v){values.put(key,v);}}
final class NativeDownloads {static boolean paused=false;static boolean paused(Context c){return paused;}static boolean hasWork(Context c){return true;}
    static void markPaused(Context c,String cause,String error){paused=true;State.get(c).write("reason",cause);}}
final class DownloadBatch {static boolean serviceStarting=false,finishing=false;static final int NOTIFICATION=102;static Object notification(Context c,Object o,boolean b){return null;}static void progress(Context c,boolean b){}static void request(Context c,boolean b){}}
final class DownloadService {static boolean running=false;}
final class NativeInbox {static void request(Context c){}}
final class DownloadRunner {static String phase="";static class Token {boolean stopped=false;android.net.Network network;void cancel(){stopped=true;}}
    interface Listener{void progress();void finished(boolean remaining);}static void start(Context c,Token t,Listener listener){}}
public final class JobStopProbe {
    static void check(boolean value,String label){if(!value)throw new AssertionError(label);}
    public static void main(String[] args){
        DownloadBatchJob job=new DownloadBatchJob();JobParameters user=new JobParameters(13),network=new JobParameters(7);
        job.onStartJob(network);check(job.onStopJob(network)&&!NativeDownloads.paused,"network loss reschedules without a manual pause");
        DownloadBatch.serviceStarting=true;job.onStartJob(user);
        check(!job.onStopJob(user)&&!NativeDownloads.paused,"late stop of an obsolete job cannot pause the newly requested service");
        DownloadBatch.serviceStarting=false;DownloadService.running=true;
        check(!job.onStopJob(user)&&!NativeDownloads.paused,"obsolete job cannot pause a running service");
        DownloadService.running=false;check(!job.onStopJob(user)&&NativeDownloads.paused,"a real OS user stop is respected");
        check("system".equals(State.db.values.get("reason")),"retain the real stop cause");
        System.out.println("PROBE_PASSED job_network_recovery obsolete_stop system_stop");
    }
}''',
    }
    compile_probe(tmp_path,fixtures,['DownloadBatchJob.java'],'JobStopProbe')
