"""Run the actual Android inbox orchestration on the JVM with platform fixtures."""
import hashlib
from pathlib import Path
import subprocess
import tempfile


PROBE=r'''
package com.aidj.music;
import android.content.Context;
import org.json.*;
import java.io.File;
import java.util.*;
final class State {
  static final State instance=new State();final Map<String,String> meta=new HashMap<>();final Set<String> files=new HashSet<>(),downloads=new HashSet<>();
  static State get(Context c){return instance;}String read(String k,String fallback){return meta.getOrDefault(k,fallback);}void write(String k,String v){meta.put(k,v);}
  File file(String md5){return files.contains(md5)?new File("verified-local-audio"):null;}
  JSONObject download(String md5){return downloads.contains(md5)?new JSONObject():null;}
}
final class NativeDownloads {
  static int requests=0;static JSONObject diagnostics(Context c){return new JSONObject();}
  static void add(Context c,JSONArray tracks)throws Exception{for(int i=0;i<tracks.length();i++)if(State.instance.downloads.add(tracks.getJSONObject(i).getString("md5")))requests++;}
}
final class Api {
  static boolean pending=true,failAck=false,connected=true,failPriority=false;static int priorities=0;static JSONArray lastPriority;static int revision=1;static String checksum="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",lastStatus="";
  static String cookie(Context c){return connected?"local-test-session":"";}
  static JSONObject request(Context c,String path,JSONObject body)throws Exception{
    JSONObject result=new JSONObject();result.put("status",200);JSONObject content=new JSONObject();result.put("body",content);
    if(path.equals("/api/device-inbox")){
      content.put("revision","library-revision");JSONArray items=new JSONArray();content.put("items",items);
      if(pending){JSONObject item=new JSONObject();item.put("id","111111111111111111111111");item.put("revision",revision);JSONObject track=new JSONObject();track.put("md5",checksum);item.put("track",track);items.put(item);}
    }else if(path.equals("/api/dj")){priorities++;lastPriority=body.getJSONArray("nextTrackIds");if(failPriority)result.put("status",503);
    }else{
      if(failAck){result.put("status",503);return result;}
      JSONArray acks=body.getJSONArray("items");for(int i=0;i<acks.length();i++){JSONObject ack=acks.getJSONObject(i);if(ack.getInt("revision")==revision){lastStatus=ack.getString("status");pending=false;}}
    }return result;
  }
}
public final class InboxProbe {
  static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
  public static void main(String[] args)throws Exception{
    Context c=new Context();State state=State.instance;
    check(NativeInbox.sync(c),"first sync");check(NativeDownloads.requests==1,"one system download");check(Api.lastStatus.equals("queued"),"pending bytes must never be acknowledged ready");
    check(NativeInbox.sync(c)&&NativeDownloads.requests==1,"accepted requests must not download again");
    state.files.add(Api.checksum);check(NativeInbox.sync(c)&&Api.lastStatus.equals("ready"),"verified completion is acknowledged");
    check(new JSONObject(state.read("deliveryReceipts","{}")).length()==0,"completed receipts cleared after server acknowledgement");
    Api.revision++;Api.pending=true;check(NativeInbox.sync(c)&&NativeDownloads.requests==1&&Api.lastStatus.equals("ready"),"new request skips an already downloaded file");
    Api.checksum="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";Api.revision++;Api.pending=true;Api.failAck=true;
    check(!NativeInbox.sync(c)&&NativeDownloads.requests==2,"lost acknowledgement retains the durable queue");
    Api.failAck=false;check(NativeInbox.sync(c)&&NativeDownloads.requests==2,"retry deduplicates the file");
    state.downloads.remove(Api.checksum);check(NativeInbox.sync(c)&&Api.lastStatus.equals("cancelled"),"explicit local cancellation is respected");
    check(NativeInbox.sync(c)&&NativeDownloads.requests==2,"cancelled requests do not restart on polling");
    JSONObject q=new JSONObject().put("djEnabled",true).put("currentId","e2");JSONArray queueEntries=new JSONArray();for(int i=0;i<10;i++)queueEntries.put(new JSONObject().put("id","e"+i).put("trackId",String.join("",Collections.nCopies(24,String.valueOf(i)))));q.put("entries",queueEntries);state.write("nativeQueue",q.toString());
    check(NativeInbox.sync(c)&&Api.priorities==1&&Api.lastPriority.length()==6&&Api.lastPriority.getString(0).startsWith("2"),"current and next five tracks receive preparation priority");
    check(NativeInbox.sync(c)&&Api.priorities==1,"unchanged background queue does not repeat preparation requests");
    q.put("currentId","e3");state.write("nativeQueue",q.toString());check(NativeInbox.sync(c)&&Api.priorities==2&&Api.lastPriority.getString(0).startsWith("3"),"background song advancement updates priority without a WebView");
    Api.failPriority=true;q.put("currentId","e4");state.write("nativeQueue",q.toString());check(NativeInbox.sync(c),"priority failure cannot break delivery sync");int attempts=Api.priorities;Api.failPriority=false;NativeInbox.sync(c);check(Api.priorities==attempts+1,"priority failure retries on the next sync");
    check(NativeDownloads.requests==2,"DJ preparation does not download extra music to the phone");
    System.out.println("NATIVE_INBOX_POLICY_PASSED durable_queue no_fake_ready no_redownload ack_retry cancellation");
  }
}
'''


def main():
    root=Path(__file__).resolve().parents[1];tools=Path.home()/'.local/share/aidj-codex-music/android-tools'
    jar=tools/'test-json/json.jar'
    assert hashlib.sha256(jar.read_bytes()).hexdigest()=='3cf6cd6892e32e2b4c1c39e0f52f5248a2f5b37646fdfbb79a66b46b618414ed'
    with tempfile.TemporaryDirectory(prefix='aidj-inbox-probe-') as folder:
        folder=Path(folder);(folder/'android/content').mkdir(parents=True)
        context=folder/'android/content/Context.java';context.write_text('package android.content;public class Context {public Context getApplicationContext(){return this;}}')
        source=folder/'InboxProbe.java';source.write_text(PROBE)
        subprocess.run([str(tools/'jdk17/bin/javac'),'--release','8','-classpath',str(jar),'-d',str(folder),str(context),str(source),str(root/'android/src/com/aidj/music/NativeInbox.java')],check=True)
        subprocess.run([str(tools/'jdk17/bin/java'),'-Xmx64m','-XX:ActiveProcessorCount=2','-cp',str(folder)+':'+str(jar),'com.aidj.music.InboxProbe'],check=True)


if __name__=='__main__':main()
