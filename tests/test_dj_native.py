"""Run the actual PlaybackService against deterministic Android media boundaries.

MediaPlayer's documented setPlaybackParams auto-start is deliberately simulated.
This exercises deck lifecycle and queue changes, rather than matching source text.
"""
from tests.test_native_downloads import compile_probe


def test_dj_real_service_pause_skip_cancel_late_preload_and_cached_sources(tmp_path):
    fixtures={
'android/content/Context.java':'''package android.content;public class Context {
 public static final String AUDIO_SERVICE="audio",NOTIFICATION_SERVICE="notification";public static final int RECEIVER_NOT_EXPORTED=4;
 public Object getSystemService(String s){return s.equals(AUDIO_SERVICE)?new android.media.AudioManager():new android.app.NotificationManager();}
 public void registerReceiver(BroadcastReceiver b,IntentFilter f){}public void registerReceiver(BroadcastReceiver b,IntentFilter f,int x){}public void unregisterReceiver(BroadcastReceiver b){}
}''',
'android/content/Intent.java':'''package android.content;import java.util.*;public class Intent {Map<String,String> m=new HashMap<>();public Intent(Context c,Class<?> k){}public Intent putExtra(String k,String v){m.put(k,v);return this;}public String getStringExtra(String k){return m.get(k);}}''',
'android/content/IntentFilter.java':'package android.content;public class IntentFilter{public IntentFilter(String s){}}',
'android/content/BroadcastReceiver.java':'package android.content;public abstract class BroadcastReceiver{public abstract void onReceive(Context c,Intent i);}',
'android/app/Service.java':'''package android.app;public class Service extends android.content.Context {public static final int START_NOT_STICKY=2;public void onCreate(){}public int onStartCommand(android.content.Intent i,int f,int s){return 2;}public android.os.IBinder onBind(android.content.Intent i){return null;}public void onDestroy(){}public void stopSelf(){}public void startForeground(int n,Notification v){}}''',
'android/os/Handler.java':'package android.os;public class Handler{public Handler(Looper l){}public void post(Runnable r){}public void postDelayed(Runnable r,long t){}public void removeCallbacks(Runnable r){}}',
'android/os/Looper.java':'package android.os;public class Looper{public static Looper getMainLooper(){return new Looper();}}',
'android/os/IBinder.java':'package android.os;public interface IBinder{}',
'android/os/Build.java':'package android.os;public class Build{public static class VERSION{public static int SDK_INT=35;}}',
'android/os/PowerManager.java':'package android.os;public class PowerManager{public static final int PARTIAL_WAKE_LOCK=1;}',
'android/net/Uri.java':'package android.net;public class Uri{public static Uri parse(String s){return new Uri();}}',
'android/media/AudioAttributes.java':'''package android.media;public class AudioAttributes{public static final int USAGE_MEDIA=1,CONTENT_TYPE_MUSIC=2;public static class Builder{public Builder setUsage(int x){return this;}public Builder setContentType(int x){return this;}public AudioAttributes build(){return new AudioAttributes();}}}''',
'android/media/AudioFocusRequest.java':'''package android.media;public class AudioFocusRequest{public static class Builder{public Builder(int n){}public Builder setAudioAttributes(AudioAttributes a){return this;}public Builder setOnAudioFocusChangeListener(AudioManager.OnAudioFocusChangeListener l,android.os.Handler h){return this;}public AudioFocusRequest build(){return new AudioFocusRequest();}}}''',
'android/media/AudioManager.java':'''package android.media;public class AudioManager{public static final int AUDIOFOCUS_GAIN=1,AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK=-3,AUDIOFOCUS_LOSS_TRANSIENT=-2,AUDIOFOCUS_REQUEST_GRANTED=1;public static final String ACTION_AUDIO_BECOMING_NOISY="noisy";public interface OnAudioFocusChangeListener{void onAudioFocusChange(int x);}public int requestAudioFocus(AudioFocusRequest a){return 1;}public void abandonAudioFocusRequest(AudioFocusRequest a){}}''',
'android/media/PlaybackParams.java':'''package android.media;public class PlaybackParams{public static final int AUDIO_FALLBACK_MODE_DEFAULT=0;public float speed=1;public PlaybackParams setSpeed(float s){speed=s;return this;}public PlaybackParams setPitch(float p){return this;}public PlaybackParams setAudioFallbackMode(int m){return this;}}''',
'android/media/MediaPlayer.java':'''package android.media;import java.util.*;public class MediaPlayer {
 public static final int SEEK_CLOSEST=3;public static final ArrayList<MediaPlayer> all=new ArrayList<>();public static int remote=0;
 public boolean playing=false,released=false;public int pos=0,duration=240000;public float gain=1,speed=1;public String path="";
 public interface OnPreparedListener{void onPrepared(MediaPlayer p);}public interface OnCompletionListener{void onCompletion(MediaPlayer p);}public interface OnSeekCompleteListener{void onSeekComplete(MediaPlayer p);}public interface OnErrorListener{boolean onError(MediaPlayer p,int w,int e);}
 OnPreparedListener prepared;OnCompletionListener done;OnSeekCompleteListener seek;OnErrorListener error;
 public MediaPlayer(){all.add(this);}public void setAudioAttributes(AudioAttributes a){}public void setWakeMode(android.content.Context c,int m){}
 public void setOnPreparedListener(OnPreparedListener l){prepared=l;}public void setOnCompletionListener(OnCompletionListener l){done=l;}public void setOnSeekCompleteListener(OnSeekCompleteListener l){seek=l;}public void setOnErrorListener(OnErrorListener l){error=l;}
 public void setDataSource(String s){path=s;}public void setDataSource(android.content.Context c,android.net.Uri u,Map<String,String> h){remote++;}
 public void prepareAsync(){}public void firePrepared(){if(prepared!=null)prepared.onPrepared(this);}public void fireDone(){if(done!=null)done.onCompletion(this);}
 public int getDuration(){return duration;}public int getCurrentPosition(){return pos;}public boolean isPlaying(){return playing&&!released;}public void start(){if(released)throw new IllegalStateException();playing=true;}public void pause(){playing=false;}public void release(){released=true;playing=false;}
 public void seekTo(long p,int m){pos=(int)p;if(seek!=null)seek.onSeekComplete(this);}public void setVolume(float l,float r){gain=l;}public void setPlaybackParams(PlaybackParams p){speed=p.speed;playing=true;}
}''',
'android/media/MediaMetadata.java':'''package android.media;public class MediaMetadata{public static final String METADATA_KEY_TITLE="title",METADATA_KEY_ARTIST="artist",METADATA_KEY_ALBUM="album",METADATA_KEY_DURATION="duration",METADATA_KEY_ALBUM_ART="art";public static class Builder{public Builder putString(String k,String s){return this;}public Builder putLong(String k,long n){return this;}public Builder putBitmap(String k,android.graphics.Bitmap b){return this;}public MediaMetadata build(){return new MediaMetadata();}}}''',
'android/media/session/MediaSession.java':'''package android.media.session;public class MediaSession{public MediaSession(android.content.Context c,String s){}public void setCallback(Callback c){}public void setActive(boolean a){}public Object getSessionToken(){return this;}public void setMetadata(android.media.MediaMetadata m){}public void setPlaybackState(PlaybackState s){}public void release(){}public static class Callback{public void onPlay(){}public void onPause(){}public void onStop(){}public void onSkipToNext(){}public void onSkipToPrevious(){}public void onSeekTo(long t){}}}''',
'android/media/session/PlaybackState.java':'''package android.media.session;public class PlaybackState{public static final int STATE_PLAYING=3,STATE_BUFFERING=6,STATE_PAUSED=2;public static final long ACTION_PLAY=1,ACTION_PAUSE=2,ACTION_PLAY_PAUSE=4,ACTION_SKIP_TO_NEXT=8,ACTION_SKIP_TO_PREVIOUS=16,ACTION_SEEK_TO=32,ACTION_STOP=64;public static class Builder{public Builder setActions(long x){return this;}public Builder setState(int s,long t,float r){return this;}public Builder setErrorMessage(String s){return this;}public PlaybackState build(){return new PlaybackState();}}}''',
'android/app/NotificationManager.java':'package android.app;public class NotificationManager{public static final int IMPORTANCE_LOW=2;public void createNotificationChannel(NotificationChannel c){}public void notify(int id,Notification n){}}',
'android/app/NotificationChannel.java':'package android.app;public class NotificationChannel{public NotificationChannel(String a,String b,int c){}}',
'android/app/PendingIntent.java':'''package android.app;public class PendingIntent{public static final int FLAG_UPDATE_CURRENT=1,FLAG_IMMUTABLE=2;public static PendingIntent getForegroundService(android.content.Context c,int n,android.content.Intent i,int f){return new PendingIntent();}public static PendingIntent getActivity(android.content.Context c,int n,android.content.Intent i,int f){return new PendingIntent();}}''',
'android/app/Notification.java':'''package android.app;public class Notification{public static final int VISIBILITY_PUBLIC=1;public static final String CATEGORY_TRANSPORT="transport";
 public static class Action{public static class Builder{public Builder(int i,String s,PendingIntent p){}public Action build(){return new Action();}}}
 public static class MediaStyle{public MediaStyle setMediaSession(Object t){return this;}public MediaStyle setShowActionsInCompactView(int a,int b,int c){return this;}}
 public static class Builder{public Builder(android.content.Context c,String ch){}public Builder setSmallIcon(int i){return this;}public Builder setContentTitle(String s){return this;}public Builder setContentText(String s){return this;}public Builder setLargeIcon(android.graphics.Bitmap b){return this;}public Builder setContentIntent(PendingIntent p){return this;}public Builder setDeleteIntent(PendingIntent p){return this;}public Builder setVisibility(int n){return this;}public Builder setOnlyAlertOnce(boolean b){return this;}public Builder setShowWhen(boolean b){return this;}public Builder setOngoing(boolean b){return this;}public Builder setCategory(String s){return this;}public Builder addAction(Action a){return this;}public Builder setStyle(MediaStyle s){return this;}public Notification build(){return new Notification();}}}''',
'android/graphics/Bitmap.java':'package android.graphics;public class Bitmap{public static class Config{public static final Config ARGB_8888=new Config();}public static Bitmap createBitmap(int w,int h,Config c){return new Bitmap();}}',
'android/graphics/Canvas.java':'package android.graphics;public class Canvas{public Canvas(Bitmap b){}public void drawColor(int c){}public void drawText(String t,float x,float y,Paint p){}}',
'android/graphics/Color.java':'package android.graphics;public class Color{public static int rgb(int r,int g,int b){return 0;}}',
'android/graphics/Paint.java':'package android.graphics;public class Paint{public static final int ANTI_ALIAS_FLAG=1;public enum Align{CENTER}public Paint(int f){}public void setColor(int c){}public void setTextAlign(Align a){}public void setTextSize(int s){}public void setTypeface(Typeface t){}}',
'android/graphics/Typeface.java':'package android.graphics;public class Typeface{public static final int BOLD=1;public static Typeface create(String s,int w){return new Typeface();}}',
'android/R.java':'package android;public class R{public static class drawable{public static final int ic_media_previous=1,ic_media_pause=2,ic_media_play=3,ic_media_next=4;}}',
'com/aidj/music/R.java':'package com.aidj.music;class R{static class drawable{static final int ic_music=1;}}',
'com/aidj/music/State.java':'''package com.aidj.music;class State{static State s=new State();static State get(android.content.Context c){return s;}String read(String k,String f){return f;}void write(String k,String v){}java.io.File file(String m){return new java.io.File("cached-original.mp3");}}''',
'com/aidj/music/NativeInbox.java':'package com.aidj.music;class NativeInbox{static void request(android.content.Context c){}}',
'com/aidj/music/Api.java':'package com.aidj.music;class Api{static String base(android.content.Context c){return "https://invalid.example";}static String cookie(android.content.Context c){return "";}}',
'com/aidj/music/MainActivity.java':'package com.aidj.music;class MainActivity{}',
'com/aidj/music/DjServiceProbe.java':r'''package com.aidj.music;
import org.json.*;import android.media.MediaPlayer;import java.lang.reflect.*;
public class DjServiceProbe {
 static void check(boolean v,String label){if(!v)throw new AssertionError(label);}
 static void tick(PlaybackService s)throws Exception{Method m=PlaybackService.class.getDeclaredMethod("tick");m.setAccessible(true);m.invoke(s);}
 static JSONObject queue(){JSONObject q=new JSONObject().put("uiRevision",1).put("currentId","e1").put("repeat","off").put("djEnabled",true),tracks=new JSONObject();JSONArray es=new JSONArray();for(int i=1;i<=3;i++){
 String id=String.join("",java.util.Collections.nCopies(24,""+i));tracks.put(id,new JSONObject().put("id",id).put("md5",String.join("",java.util.Collections.nCopies(32,""+i))).put("duration",240).put("title","song "+i));
 es.put(new JSONObject().put("id","e"+i).put("trackId",id).put("dj",new JSONObject().put("start",20).put("end",130).put("bpm",128).put("fade",1.8).put("rate",1)));}return q.put("entries",es).put("tracks",tracks);}
 static PlaybackService service()throws Exception{MediaPlayer.all.clear();PlaybackService s=new PlaybackService();s.onCreate();s.setQueue(queue());s.play();MediaPlayer.all.get(0).firePrepared();return s;}
 public static void main(String[] ignored)throws Exception{
 for(double g:new double[]{0,.1,.3,.49}){
 JSONObject t=new JSONObject().put("duration",240),left=new JSONObject().put("dj",new JSONObject().put("start",20).put("end",130).put("bpm",120).put("fade",1.8).put("grid",g)),right=new JSONObject().put("dj",new JSONObject().put("start",20).put("end",130).put("bpm",120).put("fade",1.8).put("grid",.2));
 long overlap=DjPlan.matchedOverlap(t,left,t,right,1,1);double aphase=(130-g-overlap/1000.0)*2,bphase=(20-.2)*2;
 check(overlap<=1800&&Math.abs((aphase-Math.floor(aphase))-(bphase-Math.floor(bphase)))<.003,"beat phase alignment stays within the vocal gap");
 }
 check(DjPlan.track(new JSONObject().put("duration",240).put("rangeStart",35).put("rangeEnd",90),new JSONObject().put("dj",new JSONObject().put("start",0).put("end",130).put("bpm",0).put("fade",.5)),true).getDouble("rangeStart")==35,"manual fragment still has priority over safe automatic entry");
 PlaybackService s=service();MediaPlayer a=MediaPlayer.all.get(0);check(a.isPlaying()&&a.pos==0,"legacy automatic cue must keep the first complete phrase");tick(s);MediaPlayer b=MediaPlayer.all.get(1);b.firePrepared();check(!b.isPlaying()&&b.pos==0&&b.gain==0,"preload never leaks/autoplays");
 a.pos=129000;tick(s);check(a.isPlaying()&&b.isPlaying(),"two real decks overlap");s.pause();check(!a.isPlaying()&&!b.isPlaying(),"pause stops both decks");a.pos=130000;tick(s);check(s.snapshot().getJSONObject("queue").getString("currentId").equals("e1"),"paused transition does not advance");
 a.pos=129000;s.play();check(a.isPlaying()&&b.isPlaying(),"resume both decks");s.skip(1);check(a.released&&!b.released&&b.isPlaying(),"skip promotes ready deck without reload");check(s.snapshot().getJSONObject("queue").getString("currentId").equals("e2"),"individual song queue advances");s.onDestroy();check(b.released,"destroy releases active deck");
 s=service();a=MediaPlayer.all.get(0);tick(s);b=MediaPlayer.all.get(1);b.firePrepared();a.pos=129000;tick(s);s.seek(60000);check(b.released&&a.pos==60000,"seek cancels overlap and keeps original");s.onDestroy();
 s=service();a=MediaPlayer.all.get(0);tick(s);b=MediaPlayer.all.get(1);JSONObject q=queue().put("uiRevision",2);q.getJSONArray("entries").remove(1);s.setQueue(q);check(b.released,"queue edit releases stale preload");b.firePrepared();check(!b.isPlaying(),"stale callbacks cannot restart released deck");s.onDestroy();
 s=service();a=MediaPlayer.all.get(0);tick(s);a.pos=129000;tick(s);JSONObject e=s.snapshot().getJSONObject("queue").getJSONArray("entries").getJSONObject(0);check(e.optBoolean("djFull")&&a.isPlaying(),"late preload extends source instead of abrupt cut");check(a.pos==129000,"extension never seeks backwards");s.onDestroy();
 s=service();a=MediaPlayer.all.get(0);tick(s);b=MediaPlayer.all.get(1);b.firePrepared();a.pos=129000;tick(s);q=queue().put("uiRevision",3);q.getJSONArray("entries").getJSONObject(0).put("djFull",true);s.setQueue(q);check(b.released&&a.isPlaying()&&a.pos==129000,"play full cancels overlap without restarting");s.onDestroy();
 s=service();a=MediaPlayer.all.get(0);tick(s);b=MediaPlayer.all.get(1);b.firePrepared();a.pos=129000;tick(s);a.pos=130000;tick(s);check(a.released&&b.isPlaying()&&s.snapshot().getJSONObject("queue").getString("currentId").equals("e2"),"automatic transition preserves next deck position");a.fireDone();check(s.snapshot().getJSONObject("queue").getString("currentId").equals("e2"),"stale completion cannot double-advance");s.onDestroy();
 s=service();q=queue().put("uiRevision",4);q.getJSONArray("entries").getJSONObject(0).getJSONObject("dj").put("fade",.5).put("mix",3.2);s.setQueue(q);a=MediaPlayer.all.get(0);tick(s);b=MediaPlayer.all.get(1);b.firePrepared();a.pos=127500;tick(s);a.pos=129200;tick(s);check(Math.abs(a.gain-.7)<.001&&b.gain>0,"outgoing word remains at full gain during early incoming fade");a.pos=129800;tick(s);check(a.gain<.7&&a.gain+b.gain<=.991,"late outgoing fade keeps peak headroom");s.onDestroy();
 for(int i=0;i<=100;i++){double progress=i/100.0;float out=DjPlan.gain(progress,false,.2),in=DjPlan.gain(progress,true,.2);check(out>=0&&in>=0&&out+in<=.991,"asymmetric mix remains bounded");if(progress<.8)check(Math.abs(out-.7)<.001,"no early vocal fade");}
 check(MediaPlayer.remote==0,"all decks reuse downloaded originals without HTTP");
 System.out.println("PROBE_PASSED overlap pause resume skip seek cancel stale full fallback offline");
 }
}''',
    }
    compile_probe(tmp_path,fixtures,['PlaybackService.java','PlaybackRange.java','PlaybackFade.java','DjPlan.java'],'DjServiceProbe')


def test_dj_priority_follows_native_background_queue_without_redownloading_files(tmp_path):
    from tests.native_inbox_probe import PROBE
    fixtures={'android/content/Context.java':'package android.content;public class Context{public Context getApplicationContext(){return this;}}',
              'com/aidj/music/InboxProbe.java':PROBE.replace('NATIVE_INBOX_POLICY_PASSED','PROBE_PASSED')}
    compile_probe(tmp_path,fixtures,['NativeInbox.java'],'InboxProbe')
