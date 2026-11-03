package com.aidj.music;

import android.app.*;
import android.content.*;
import android.graphics.*;
import android.media.*;
import android.media.session.*;
import android.os.*;
import org.json.*;
import java.io.File;
import java.util.*;

public final class PlaybackService extends Service {
    static PlaybackService instance;
    private static final int NOTIFICATION=100;
    private static final String CHANNEL="aidj-playback";
    private MediaPlayer player;
    private MediaSession session;
    private AudioManager audio;
    private AudioFocusRequest focus;
    private JSONObject queue=new JSONObject();
    private String loaded="",error="";
    private boolean ready=false,wantPlay=false,resumeAfterFocus=false,rangeSeeking=false;
    private long pendingSeek=0;
    private float duckGain=1f;
    private float lastGain=-1f;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private BroadcastReceiver noisy;
    private final Runnable librarySync=new Runnable(){public void run(){NativeInbox.request(PlaybackService.this);handler.postDelayed(this,20000);}};
    private final Runnable rangeTick=new Runnable(){public void run(){if(ready&&!rangeSeeking){applyVolume();if(isPlaying()&&position()>=PlaybackRange.end(current(),duration())-15)advance(1,true);}handler.postDelayed(this,50);}};

    @Override public void onCreate(){
        super.onCreate();instance=this;
        try{queue=new JSONObject(State.get(this).read("nativeQueue","{}"));}catch(Exception ignored){}
        pendingSeek=(long)(queue.optDouble("time")*1000);
        audio=(AudioManager)getSystemService(AUDIO_SERVICE);
        focus=new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN).setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build()).setOnAudioFocusChangeListener(change->{
            if(change==AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK){duckGain=.2f;applyVolume();}
            else if(change<0){resumeAfterFocus=change==AudioManager.AUDIOFOCUS_LOSS_TRANSIENT&&isPlaying();pause();}
            else{duckGain=1f;applyVolume();if(resumeAfterFocus){resumeAfterFocus=false;play();}}
        },handler).build();
        session=new MediaSession(this,"AI DJ");
        session.setCallback(new MediaSession.Callback(){
            @Override public void onPlay(){play();}
            @Override public void onPause(){pause();}
            @Override public void onStop(){stopSelf();}
            @Override public void onSkipToNext(){advance(1,false);}
            @Override public void onSkipToPrevious(){if(position()-PlaybackRange.start(current(),duration())>3000)seek(0);else advance(-1,false);}
            @Override public void onSeekTo(long value){seek(value);}
        });session.setActive(true);
        NotificationManager notifications=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        notifications.createNotificationChannel(new NotificationChannel(CHANNEL,"Музыка",NotificationManager.IMPORTANCE_LOW));
        noisy=new BroadcastReceiver(){public void onReceive(Context c,Intent i){pause();}};
        if(Build.VERSION.SDK_INT>=33)registerReceiver(noisy,new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY),Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(noisy,new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY));
        startForeground(NOTIFICATION,notification());
        handler.post(librarySync);
        handler.post(rangeTick);
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(intent!=null){
            String next=intent.getStringExtra("queue");if(next!=null)try{setQueue(new JSONObject(next));}catch(Exception ignored){}
            String action=intent.getStringExtra("action");
            if("pause".equals(action))pause();else if("next".equals(action))advance(1,false);else if("previous".equals(action)){if(position()-PlaybackRange.start(current(),duration())>3000)seek(0);else advance(-1,false);}
            else if("stop".equals(action))stopSelf();else play();
        }
        return START_NOT_STICKY;
    }
    @Override public IBinder onBind(Intent intent){return null;}
    private JSONArray entries(){JSONArray result=queue.optJSONArray("entries");return result==null?new JSONArray():result;}
    private int index(){String id=queue.optString("currentId");JSONArray list=entries();for(int i=0;i<list.length();i++)if(id.equals(list.optJSONObject(i).optString("id")))return i;return -1;}
    private JSONObject current(){int at=index();if(at<0)return null;String id=entries().optJSONObject(at).optString("trackId");JSONObject tracks=queue.optJSONObject("tracks");return tracks==null?null:tracks.optJSONObject(id);}
    void setQueue(JSONObject value){
        long revision=value.optLong("uiRevision");if(revision<queue.optLong("uiRevision"))return;
        String before=queue.optString("currentId");queue=value;if(!before.equals(queue.optString("currentId")))pendingSeek=0;
        State.get(this).write("nativeQueue",queue.toString());
        String id=queue.optString("currentId");
        if(current()==null){releasePlayer();stopSelf();return;}
        if(!loaded.equals(id)&&player!=null){boolean playing=isPlaying();releasePlayer();if(playing)prepare(true);}
        else if(ready&&(position()<PlaybackRange.start(current(),duration())-100||position()>=PlaybackRange.end(current(),duration())))seek(PlaybackRange.start(current(),duration()));
        applyVolume();
        updateSession();
    }
    boolean isPlaying(){try{return ready&&player!=null&&player.isPlaying();}catch(Exception e){return false;}}
    long position(){try{return ready&&player!=null?player.getCurrentPosition():pendingSeek;}catch(Exception e){return 0;}}
    long duration(){try{return ready&&player!=null?player.getDuration():(long)((current()==null?0:current().optDouble("duration"))*1000);}catch(Exception e){return 0;}}
    void play(){
        wantPlay=true;error="";
        if(current()==null){wantPlay=false;updateSession();return;}
        if(player==null||!loaded.equals(queue.optString("currentId"))){prepare(true);return;}
        if(!ready)return;
        if(rangeSeeking)return;
        if(position()<PlaybackRange.start(current(),duration())-100||position()>=PlaybackRange.end(current(),duration())-15){seek(0);return;}
        if(audio.requestAudioFocus(focus)!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED){error="Не удалось получить аудиофокус.";updateSession();return;}
        try{applyVolume();player.start();updateSession();}catch(Exception e){error="Не удалось включить песню.";updateSession();}
    }
    void pause(){wantPlay=false;try{if(ready&&player!=null)player.pause();}catch(Exception ignored){}savePosition();updateSession();}
    void seek(long value){pendingSeek=PlaybackRange.clamp(current(),duration(),value);if(ready&&player!=null){rangeSeeking=true;player.seekTo(pendingSeek,MediaPlayer.SEEK_CLOSEST);pendingSeek=0;}savePosition();updateSession();}
    private void applyVolume(){
        if(player==null||!ready)return;
        long length=duration();float gain=PlaybackFade.gain(position(),PlaybackRange.start(current(),length),PlaybackRange.end(current(),length),queue.optLong("transitionMs",0))*duckGain;
        if(Math.abs(gain-lastGain)<.001f)return;
        try{player.setVolume(gain,gain);lastGain=gain;}catch(Exception ignored){}
    }
    private void prepare(boolean start){
        JSONObject track=current();if(track==null){releasePlayer();wantPlay=false;updateSession();return;}
        releasePlayer();wantPlay=start;loaded=queue.optString("currentId");error="";
        try{
            player=new MediaPlayer();MediaPlayer loading=player;
            player.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build());
            player.setWakeMode(this,PowerManager.PARTIAL_WAKE_LOCK);
            player.setOnPreparedListener(mp->{if(mp!=loading)return;ready=true;long target=PlaybackRange.clamp(current(),mp.getDuration(),pendingSeek);pendingSeek=0;if(target>0){rangeSeeking=true;mp.seekTo(target,MediaPlayer.SEEK_CLOSEST);}else if(wantPlay)play();else updateSession();});
            player.setOnCompletionListener(mp->{if(mp==loading)advance(1,true);});
            player.setOnSeekCompleteListener(mp->{if(mp==loading){rangeSeeking=false;applyVolume();savePosition();if(wantPlay&&!isPlaying())play();else updateSession();}});
            player.setOnErrorListener((mp,what,extra)->{if(mp==loading){releasePlayer();error="Не удалось открыть песню. Проверь сеть или подключение к боту.";wantPlay=false;updateSession();}return true;});
            File file=State.get(this).file(track.getString("md5"));
            if(file!=null)player.setDataSource(file.getAbsolutePath());
            else{
                String id=track.getString("id");if(!id.matches("[a-f0-9]{24}"))throw new Exception();
                Map<String,String> headers=new HashMap<>();headers.put("Cookie",Api.cookie(this));
                player.setDataSource(this,android.net.Uri.parse(Api.base(this)+"/api/audio/"+id),headers);
            }
            player.prepareAsync();updateSession();
        }catch(Exception e){releasePlayer();error="Не удалось открыть песню. Проверь подключение к боту.";wantPlay=false;updateSession();}
    }
    private void advance(int direction,boolean ended){
        JSONArray list=entries();int at=index();if(list.length()==0)return;
        if(ended&&"one".equals(queue.optString("repeat"))){seek(0);play();return;}
        int next=at+direction;
        if(next>=list.length()){
            if(!"all".equals(queue.optString("repeat"))){pause();seek(0);return;}
            if(queue.optBoolean("shuffle")){
                ArrayList<JSONObject> values=new ArrayList<>();for(int i=0;i<list.length();i++)values.add(list.optJSONObject(i));Collections.shuffle(values);list=new JSONArray(values);try{queue.put("entries",list);}catch(Exception ignored){}
            }next=0;
        }
        if(next<0)next=0;
        try{queue.put("currentId",list.getJSONObject(next).getString("id"));queue.put("time",0);}catch(Exception ignored){}
        pendingSeek=0;State.get(this).write("nativeQueue",queue.toString());prepare(true);
    }
    private void releasePlayer(){if(player!=null){try{player.release();}catch(Exception ignored){}player=null;}ready=false;rangeSeeking=false;lastGain=-1f;loaded="";}
    private void savePosition(){try{queue.put("time",position()/1000.0);State.get(this).write("nativeQueue",queue.toString());}catch(Exception ignored){}}
    private PendingIntent action(String value,int request){
        Intent intent=new Intent(this,PlaybackService.class).putExtra("action",value);
        return PendingIntent.getForegroundService(this,request,intent,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
    }
    private Bitmap artwork(){
        Bitmap bitmap=Bitmap.createBitmap(256,256,Bitmap.Config.ARGB_8888);Canvas canvas=new Canvas(bitmap);canvas.drawColor(Color.rgb(212,255,97));Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);paint.setColor(Color.rgb(20,21,20));paint.setTextAlign(Paint.Align.CENTER);paint.setTextSize(110);paint.setTypeface(Typeface.create("sans-serif",Typeface.BOLD));canvas.drawText("AI DJ",128,167,paint);return bitmap;
    }
    private Notification notification(){
        JSONObject track=current();String title=track==null?"AI DJ":track.optString("title"),artist=track==null?"Моя музыка":track.optString("artist");
        PendingIntent open=PendingIntent.getActivity(this,10,new Intent(this,MainActivity.class),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this,CHANNEL).setSmallIcon(R.drawable.ic_music).setContentTitle(title).setContentText(artist).setLargeIcon(artwork()).setContentIntent(open)
            .setDeleteIntent(action("stop",4))
            .setVisibility(Notification.VISIBILITY_PUBLIC).setOnlyAlertOnce(true).setShowWhen(false).setOngoing(isPlaying()).setCategory(Notification.CATEGORY_TRANSPORT)
            .addAction(new Notification.Action.Builder(android.R.drawable.ic_media_previous,"Предыдущая",action("previous",1)).build())
            .addAction(new Notification.Action.Builder(isPlaying()?android.R.drawable.ic_media_pause:android.R.drawable.ic_media_play,isPlaying()?"Пауза":"Играть",action(isPlaying()?"pause":"play",2)).build())
            .addAction(new Notification.Action.Builder(android.R.drawable.ic_media_next,"Следующая",action("next",3)).build())
            .setStyle(new Notification.MediaStyle().setMediaSession(session.getSessionToken()).setShowActionsInCompactView(0,1,2)).build();
    }
    private void updateSession(){
        if(session==null)return;
        JSONObject track=current();long length=duration();
        session.setMetadata(new MediaMetadata.Builder().putString(MediaMetadata.METADATA_KEY_TITLE,track==null?"AI DJ":track.optString("title"))
            .putString(MediaMetadata.METADATA_KEY_ARTIST,track==null?"":track.optString("artist")).putString(MediaMetadata.METADATA_KEY_ALBUM,"AI DJ")
            .putLong(MediaMetadata.METADATA_KEY_DURATION,length).putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART,artwork()).build());
        long actions=PlaybackState.ACTION_PLAY|PlaybackState.ACTION_PAUSE|PlaybackState.ACTION_PLAY_PAUSE|PlaybackState.ACTION_SKIP_TO_NEXT|PlaybackState.ACTION_SKIP_TO_PREVIOUS|PlaybackState.ACTION_SEEK_TO|PlaybackState.ACTION_STOP;
        PlaybackState.Builder state=new PlaybackState.Builder().setActions(actions).setState(isPlaying()?PlaybackState.STATE_PLAYING:wantPlay?PlaybackState.STATE_BUFFERING:PlaybackState.STATE_PAUSED,position(),isPlaying()?1:0);
        if(!error.isEmpty())state.setErrorMessage(error);session.setPlaybackState(state.build());
        ((NotificationManager)getSystemService(NOTIFICATION_SERVICE)).notify(NOTIFICATION,notification());
    }
    JSONObject snapshot(){
        JSONObject state=new JSONObject();try{state.put("queue",queue);state.put("paused",!isPlaying());state.put("time",position()/1000.0);state.put("duration",duration()/1000.0);state.put("error",error);}catch(Exception ignored){}return state;
    }
    @Override public void onDestroy(){handler.removeCallbacks(librarySync);handler.removeCallbacks(rangeTick);savePosition();releasePlayer();audio.abandonAudioFocusRequest(focus);if(noisy!=null)unregisterReceiver(noisy);if(session!=null)session.release();instance=null;super.onDestroy();}
}
