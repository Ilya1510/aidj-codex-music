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
    private MediaPlayer player,incoming;
    private PlaybackLevel playerLevel,incomingLevel;
    private boolean incomingReady=false,mixing=false,waitingNext=false;
    private String incomingId="",incomingSpec="",failedIncoming="";
    private JSONObject playingTrack;
    private float speed=1f,incomingSpeed=1f,mixOutStart=.7f;
    private long mixStart=0,mixSpan=1,mixFade=1;
    private int nextGeneration=0;
    private MediaSession session;
    private AudioManager audio;
    private AudioFocusRequest focus;
    private JSONObject queue=new JSONObject();
    private long playbackEpoch=0;
    private String loaded="",error="";
    private boolean ready=false,wantPlay=false,resumeAfterFocus=false,rangeSeeking=false;
    private long pendingSeek=0;
    private float duckGain=1f;
    private float lastGain=-1f;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private BroadcastReceiver noisy;
    private final Runnable librarySync=new Runnable(){public void run(){NativeInbox.request(PlaybackService.this);handler.postDelayed(this,20000);}};
    private final Runnable rangeTick=new Runnable(){public void run(){if(ready&&!rangeSeeking){tick();}handler.postDelayed(this,50);}};

    @Override public void onCreate(){
        super.onCreate();instance=this;
        try{queue=new JSONObject(State.get(this).read("nativeQueue","{}"));}catch(Exception ignored){}
        playbackEpoch=Math.max(0,queue.optLong("playbackEpoch",0));stampEpoch();
        pendingSeek=(long)(queue.optDouble("time")*1000);
        audio=(AudioManager)getSystemService(AUDIO_SERVICE);
        focus=new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN).setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build()).setOnAudioFocusChangeListener(change->{
            if(change==AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK){duckGain=.2f;applyVolume();}
            else if(change<0){resumeAfterFocus=change==AudioManager.AUDIOFOCUS_LOSS_TRANSIENT&&wantPlay&&isPlaying();pause();}
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
            String next=intent.getBooleanExtra("reloadQueue",false)?State.get(this).read("nativeQueue","{}"):intent.getStringExtra("queue");if(next!=null)try{setQueue(new JSONObject(next));}catch(Exception ignored){}
            String action=intent.getStringExtra("action");
            if("pause".equals(action))pause();else if("next".equals(action))advance(1,false);else if("previous".equals(action)){if(position()-PlaybackRange.start(current(),duration())>3000)seek(0);else advance(-1,false);}
            else if("stop".equals(action))stopSelf();else play();
        }
        return START_NOT_STICKY;
    }
    @Override public IBinder onBind(Intent intent){return null;}
    private JSONArray entries(){JSONArray result=queue.optJSONArray("entries");return result==null?new JSONArray():result;}
    private int index(){String id=queue.optString("currentId");JSONArray list=entries();for(int i=0;i<list.length();i++)if(id.equals(list.optJSONObject(i).optString("id")))return i;return -1;}
    private JSONObject entry(){int at=index();return at<0?null:entries().optJSONObject(at);}
    private JSONObject source(JSONObject e){JSONObject tracks=queue.optJSONObject("tracks");return e==null||tracks==null?null:tracks.optJSONObject(e.optString("trackId"));}
    private boolean dj(){return queue.optBoolean("djEnabled")||queue.optJSONObject("fixedSet")!=null;}
    private boolean remix(){return queue.optJSONObject("fixedSet")==null&&RemixPlan.enabled(queue);}
    private boolean audition(){return queue.optJSONObject("fixedSet")==null&&dj()&&!remix()&&queue.optBoolean("djAudition")&&entry()!=null&&!entry().optBoolean("djFull");}
    private JSONObject current(){return playingTrack!=null&&loaded.equals(queue.optString("currentId"))?playingTrack:DjPlan.track(source(entry()),entry(),dj());}
    private JSONObject nextEntry(){if(queue.optJSONObject("fixedSet")==null&&!remix()&&"one".equals(queue.optString("repeat")))return null;int at=index()+1;if(at>=entries().length())at=queue.optJSONObject("fixedSet")==null&&!remix()&&"all".equals(queue.optString("repeat"))?0:-1;return at<0?null:entries().optJSONObject(at);}
    private String nextSpec(){JSONObject e=nextEntry(),t=source(e);return e==null||t==null?"":e.optString("id")+"|"+e.optBoolean("djFull")+"|"+e.optBoolean("remix")+"|"+String.valueOf(e.optJSONObject("dj"))+"|"+String.valueOf(e.optJSONObject("fixed"))+"|"+t.optString("md5")+"|"+t.optDouble("rangeStart",0)+"|"+t.optDouble("rangeEnd",t.optDouble("duration"))+"|"+dj();}
    private void stampEpoch(){try{queue.put("playbackEpoch",playbackEpoch);}catch(Exception ignored){}}
    private void advanceEpoch(){if(playbackEpoch<Long.MAX_VALUE)playbackEpoch++;stampEpoch();}
    private boolean matchesEpoch(JSONObject value){Object raw=value.opt("playbackEpoch");if(!(raw instanceof Number))return false;double number=((Number)raw).doubleValue();return Double.isFinite(number)&&number>=0&&number==Math.rint(number)&&((Number)raw).longValue()==playbackEpoch;}
    void setQueue(JSONObject value){applyQueue(value,false);}
    boolean applyQueue(JSONObject value,boolean explicitSelection){return applyQueue(value,explicitSelection,false);}
    private boolean applyQueue(JSONObject value,boolean explicitSelection,boolean metadataOnly){
        if(value==null||value.optInt("queueProtocol",0)>=1&&!explicitSelection&&!matchesEpoch(value))return false;
        if(value.optJSONObject("fixedSet")!=null){
            JSONArray frozen=value.optJSONArray("entries");JSONObject sources=value.optJSONObject("tracks");if(frozen==null||sources==null)return false;
            for(int i=0;i<frozen.length();i++){JSONObject e=frozen.optJSONObject(i);if(e==null||DjPlan.fixed(sources.optJSONObject(e.optString("trackId")),e)==null)return false;}
        }
        long revision=value.optLong("uiRevision");if(revision<queue.optLong("uiRevision"))return false;
        String before=queue.optString("currentId");boolean wasDj=dj(),wasAudition=queue.optBoolean("djAudition");long oldStart=PlaybackRange.start(current(),duration()),oldEnd=PlaybackRange.end(current(),duration());float oldSpeed=speed;String keepNext=incoming==null?"":incomingId;queue=value;if(!metadataOnly&&queue.optJSONObject("fixedSet")==null)RemixPlan.arrange(queue,before.equals(queue.optString("currentId"))?keepNext:"");if(!before.equals(queue.optString("currentId")))pendingSeek=0;
        JSONObject selectedSource=source(entry()),selectedTrack=DjPlan.track(selectedSource,entry(),dj());long selectedDuration=before.equals(queue.optString("currentId"))&&ready?duration():(long)((selectedSource==null?0:selectedSource.optDouble("duration"))*1000);
        if(!before.equals(queue.optString("currentId"))||oldStart!=PlaybackRange.start(selectedTrack,selectedDuration)||oldEnd!=PlaybackRange.end(selectedTrack,selectedDuration)||oldSpeed!=DjPlan.rate(selectedSource,entry(),dj()))advanceEpoch();else stampEpoch();
        State.get(this).write("nativeQueue",queue.toString());
        String id=queue.optString("currentId");
        if(current()==null){releasePlayer();stopSelf();return true;}
        if(!loaded.equals(id)&&player!=null){boolean playing=wantPlay;releasePlayer();if(playing)prepare(true);}
        else if(ready){
            playingTrack=DjPlan.track(source(entry()),entry(),dj());speed=DjPlan.rate(source(entry()),entry(),dj());if(oldSpeed!=speed)setSpeed(player,speed);
            if(!waitingNext&&(position()<PlaybackRange.start(current(),duration())-100||position()>=PlaybackRange.end(current(),duration())))seek(PlaybackRange.start(current(),duration()));
        }
        if(wasDj!=dj()||!incomingSpec.equals(nextSpec())||oldStart!=PlaybackRange.start(current(),duration())||oldEnd!=PlaybackRange.end(current(),duration())||oldSpeed!=speed)cancelIncoming();
        if(playerLevel!=null&&playerLevel.update(source(entry())))lastGain=-1;
        if(incomingLevel!=null&&incomingId.equals(nextEntry()==null?"":nextEntry().optString("id")))incomingLevel.update(source(nextEntry()));
        if(!wasAudition&&audition()&&ready)seek(DjPlan.previewTail(current(),duration(),speed));
        applyVolume();
        updateSession();
        return true;
    }
    private static JSONObject shallow(JSONObject original){JSONObject result=new JSONObject();if(original!=null){java.util.Iterator<String> keys=original.keys();while(keys.hasNext()){String key=keys.next();try{result.put(key,original.opt(key));}catch(Exception ignored){}}}return result;}
    boolean refreshMetadata(JSONObject patch){
        if(patch==null)return false;
        JSONObject oldTracks=queue.optJSONObject("tracks"),newTracks=shallow(oldTracks),patchTracks=patch.optJSONObject("tracks");
        HashSet<String> liveIds=new HashSet<>();for(int i=0;i<entries().length();i++)liveIds.add(entries().optJSONObject(i).optString("trackId"));
        boolean changed=false;
        if(patchTracks!=null)for(String id:liveIds){JSONObject fresh=patchTracks.optJSONObject(id);if(fresh!=null&&oldTracks!=null&&oldTracks.has(id)&&!String.valueOf(oldTracks.opt(id)).equals(fresh.toString())){try{newTracks.put(id,fresh);changed=true;}catch(Exception ignored){}}}
        HashMap<String,JSONObject> metadata=new HashMap<>();JSONArray updates=patch.optJSONArray("entries");if(updates!=null)for(int i=0;i<updates.length();i++){JSONObject e=updates.optJSONObject(i);if(e!=null&&liveIds.contains(e.optString("trackId")))metadata.put(e.optString("trackId"),e);}
        JSONArray mergedEntries=new JSONArray();String[] fields={"remixAnchors","remixAcousticAnchors","remixPool","remixCues"};
        for(int i=0;i<entries().length();i++){
            JSONObject old=entries().optJSONObject(i),fresh=metadata.get(old.optString("trackId"));if(fresh==null){mergedEntries.put(old);continue;}JSONObject merged=shallow(old);
            for(String field:fields){Object value=fresh.opt(field);if(value==JSONObject.NULL)value=null;if(!String.valueOf(old.opt(field)).equals(String.valueOf(value)))changed=true;try{if(value==null)merged.remove(field);else merged.put(field,value);}catch(Exception ignored){}}
            mergedEntries.put(merged);
        }
        if(!changed)return true;JSONObject merged=shallow(queue);
        try{merged.put("tracks",newTracks).put("entries",mergedEntries).put("playbackEpoch",playbackEpoch);}catch(Exception ignored){return false;}
        return applyQueue(merged,false,true);
    }
    boolean isPlaying(){try{return ready&&player!=null&&player.isPlaying();}catch(Exception e){return false;}}
    long position(){try{return ready&&player!=null?player.getCurrentPosition():pendingSeek;}catch(Exception e){return 0;}}
    long duration(){try{return ready&&player!=null?player.getDuration():(long)((current()==null?0:current().optDouble("duration"))*1000);}catch(Exception e){return 0;}}
    void play(){
        wantPlay=true;error="";
        if(current()==null){wantPlay=false;updateSession();return;}
        if(player==null||!loaded.equals(queue.optString("currentId"))){prepare(true);return;}
        if(!ready)return;
        if(waitingNext){if(incomingReady){promote();play();}else{if(incoming==null){failedIncoming="";preload();}updateSession();}return;}
        if(rangeSeeking)return;
        long restartAt=DjPlan.endsAtSourceEnd(source(entry()),entry())?duration():PlaybackRange.end(current(),duration())-15;
        if(position()<PlaybackRange.start(current(),duration())-100||position()>=restartAt){seek(0);return;}
        if(audio.requestAudioFocus(focus)!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED){wantPlay=false;error="Не удалось получить аудиофокус.";updateSession();return;}
        try{applyVolume();player.start();if(mixing&&incomingReady)incoming.start();updateSession();}catch(Exception e){wantPlay=false;error="Не удалось включить песню.";updateSession();}
    }
    void pause(){
        wantPlay=false;
        try{if(player!=null)player.setVolume(0,0);}catch(Exception ignored){}
        try{if(incoming!=null)incoming.setVolume(0,0);}catch(Exception ignored){}
        lastGain=0;
        try{if(ready&&player!=null)player.pause();}catch(Exception ignored){}
        try{if(incomingReady&&incoming!=null)incoming.pause();}catch(Exception ignored){}
        savePosition();updateSession();
    }
    void seek(long value){cancelIncoming();pendingSeek=PlaybackRange.clamp(current(),duration(),value);if(ready&&player!=null){rangeSeeking=true;player.seekTo(pendingSeek,MediaPlayer.SEEK_CLOSEST);pendingSeek=0;}savePosition();updateSession();}
    private void applyVolume(){
        if(!wantPlay){
            if(lastGain!=0){
                try{if(player!=null)player.setVolume(0,0);}catch(Exception ignored){}
                try{if(incoming!=null)incoming.setVolume(0,0);}catch(Exception ignored){}
                lastGain=0;
            }
            return;
        }
        if(player==null||!ready)return;
        long length=duration();float gain=PlaybackFade.gain(position(),PlaybackRange.start(current(),length),PlaybackRange.end(current(),length),queue.optLong("transitionMs",0))*duckGain;
        JSONObject fixed=DjPlan.fixed(source(entry()),entry());
        if(dj()&&fixed!=null){
            double progress=mixing?Math.max(0,Math.min(1,(double)(position()-mixStart)/mixSpan)):0;
            double inSeconds=fixed.optDouble("fadeIn"),outSeconds=fixed.optDouble("fadeOut"),start=PlaybackRange.start(current(),length),end=PlaybackRange.end(current(),length);
            double intro=inSeconds>0?Math.max(0,Math.min(1,(position()-start)/(inSeconds*1000*speed))):1;
            double tail=outSeconds>0?Math.max(0,Math.min(1,(end-position())/(outSeconds*1000*speed))):1;
            gain=(float)(.70*intro*(mixing?Math.cos(progress*Math.PI/2):tail))*duckGain;
            if(mixing&&incomingReady&&incoming!=null){float g=(float)(.70*Math.sin(progress*Math.PI/2))*duckGain;try{float actual=incomingLevel==null?g:incomingLevel.gain(g);incoming.setVolume(actual,actual);}catch(Exception ignored){}}
        }else if(dj()){
            double progress=mixing?(double)(position()-mixStart)/mixSpan:0;
            gain=(mixing?DjPlan.gain(progress,false,(double)mixFade/mixSpan)*Math.min(1,mixOutStart/.70f):.70f)*duckGain;
            if(!mixing&&(nextEntry()==null||!incomingReady)){long window=DjPlan.safeFade(source(entry()),entry());gain*=Math.min(1f,Math.max(0f,(PlaybackRange.end(current(),length)-position())/(window*speed)));}
            if(mixing&&incomingReady&&incoming!=null){float g=DjPlan.gain(progress,true,(double)mixFade/mixSpan)*duckGain;try{float actual=incomingLevel==null?g:incomingLevel.gain(g);incoming.setVolume(actual,actual);}catch(Exception ignored){}}
        }
        if(Math.abs(gain-lastGain)<.001f)return;
        try{float actual=playerLevel==null?gain:playerLevel.gain(gain);player.setVolume(actual,actual);lastGain=gain;}catch(Exception ignored){}
    }
    private void prepare(boolean start){
        if(queue.optJSONObject("fixedSet")!=null&&DjPlan.fixed(source(entry()),entry())==null){releasePlayer();wantPlay=false;error="Границы сета изменились. Обнови библиотеку и запусти DJ 1 заново.";updateSession();return;}
        JSONObject track=current();if(track==null){releasePlayer();wantPlay=false;updateSession();return;}
        releasePlayer();wantPlay=start;loaded=queue.optString("currentId");playingTrack=track;speed=DjPlan.rate(source(entry()),entry(),dj());error="";
        try{
            player=new MediaPlayer();playerLevel=new PlaybackLevel(player,source(entry()));MediaPlayer loading=player;
            player.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build());
            player.setWakeMode(this,PowerManager.PARTIAL_WAKE_LOCK);
            player.setOnPreparedListener(mp->{if(player!=loading)return;ready=true;setSpeed(mp,speed);long target=PlaybackRange.clamp(current(),mp.getDuration(),pendingSeek);if(audition()&&pendingSeek==0)target=DjPlan.previewTail(current(),mp.getDuration(),speed);pendingSeek=0;if(target>0){rangeSeeking=true;mp.seekTo(target,MediaPlayer.SEEK_CLOSEST);}else if(wantPlay)play();else updateSession();});
            player.setOnCompletionListener(mp->{if(player==loading)completed();});
            player.setOnSeekCompleteListener(mp->{if(player==loading){rangeSeeking=false;applyVolume();savePosition();if(wantPlay&&!isPlaying())play();else updateSession();}});
            player.setOnErrorListener((mp,what,extra)->{if(player==loading){releasePlayer();error="Не удалось открыть песню. Проверь сеть или подключение к боту.";wantPlay=false;updateSession();}return true;});
            dataSource(player,track);
            player.prepareAsync();updateSession();
        }catch(Exception e){releasePlayer();error="Не удалось открыть песню. Проверь подключение к боту.";wantPlay=false;updateSession();}
    }
    private void dataSource(MediaPlayer deck,JSONObject track)throws Exception{
        File file=State.get(this).file(track.getString("md5"));
        if(file!=null)deck.setDataSource(file.getAbsolutePath());
        else{String id=track.getString("id");if(!id.matches("[a-f0-9]{24}"))throw new Exception();Map<String,String> headers=new HashMap<>();headers.put("Cookie",Api.cookie(this));deck.setDataSource(this,android.net.Uri.parse(Api.base(this)+"/api/audio/"+id),headers);}
    }
    private void setSpeed(MediaPlayer deck,float value){
        if(deck==null)return;boolean playing=false;
        try{
            playing=deck.isPlaying();
            // Parameter assignment may start a paused player before pause() can
            // run. Silence it first, including its first prepared callback.
            deck.setVolume(0,0);
            deck.setPlaybackParams(new PlaybackParams().setSpeed(value).setPitch(1f).setAudioFallbackMode(PlaybackParams.AUDIO_FALLBACK_MODE_DEFAULT));
        }catch(Exception ignored){}
        try{if(!wantPlay||!playing||deck==incoming)deck.pause();}catch(Exception ignored){}
        // applyVolume() must restore the real current-deck gain even when its
        // envelope has not changed. Preloaded incoming audio stays muted.
        if(deck==player)lastGain=-1;
    }
    void skip(int direction){if(direction<0&&position()-PlaybackRange.start(current(),duration())>3000)seek(0);else advance(direction,false);}
    private void cancelIncoming(){
        nextGeneration++;mixing=false;waitingNext=false;incomingReady=false;incomingId="";incomingSpec="";
        if(incomingLevel!=null){incomingLevel.release();incomingLevel=null;}
        if(incoming!=null){try{incoming.release();}catch(Exception ignored){}incoming=null;}
        lastGain=-1;
    }
    private void preload(){
        if(!dj())return;String spec=nextSpec();if(spec.isEmpty()||spec.equals(failedIncoming))return;
        if(incoming!=null&&spec.equals(incomingSpec))return;
        boolean waiting=waitingNext;cancelIncoming();waitingNext=waiting;incomingSpec=spec;JSONObject e=nextEntry(),t=source(e);if(t==null)return;
        incomingId=e.optString("id");incomingSpeed=DjPlan.rate(t,e,true);final int token=nextGeneration;
        try{
            MediaPlayer deck=new MediaPlayer();incoming=deck;incomingLevel=new PlaybackLevel(deck,t);
            deck.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build());deck.setWakeMode(this,PowerManager.PARTIAL_WAKE_LOCK);deck.setVolume(0,0);
            deck.setOnErrorListener((mp,what,extra)->{if(incoming==mp&&token==nextGeneration)incomingFailed(spec);return true;});
            deck.setOnPreparedListener(mp->{if(incoming!=mp||token!=nextGeneration)return;setSpeed(mp,incomingSpeed);long start=PlaybackRange.start(DjPlan.track(t,e,true),mp.getDuration());if(start>0)mp.seekTo(start,MediaPlayer.SEEK_CLOSEST);else incomingPrepared();});
            deck.setOnSeekCompleteListener(mp->{if(incoming==mp&&token==nextGeneration)incomingPrepared();});
            dataSource(deck,t);deck.prepareAsync();
        }catch(Exception ignored){incomingFailed(spec);}
    }
    private void incomingFailed(String spec){
        boolean waiting=waitingNext;failedIncoming=spec;cancelIncoming();
        if(waiting){waitingNext=true;wantPlay=false;error="Не удалось подготовить следующую песню. Нажми воспроизвести для повтора.";savePosition();updateSession();}
    }
    private void waitForNext(){
        applyVolume();try{if(player!=null)player.setVolume(0,0);}catch(Exception ignored){}lastGain=0;try{if(ready&&player!=null)player.pause();}catch(Exception ignored){}
        waitingNext=true;
        if(incoming==null&&!failedIncoming.isEmpty()){wantPlay=false;error="Не удалось подготовить следующую песню. Нажми воспроизвести для повтора.";}
        else if(incoming==null)preload();
        savePosition();updateSession();
    }
    private void previewSeek(){
        // The target deck is unchanged: retain an already prepared successor.
        long target=DjPlan.previewTail(current(),duration(),speed);rangeSeeking=true;player.seekTo(target,MediaPlayer.SEEK_CLOSEST);savePosition();
    }
    private void completed(){
        if(!wantPlay){savePosition();updateSession();return;}
        if(dj()&&nextEntry()!=null){if(incomingReady){promote();if(wantPlay)play();}else waitForNext();return;}
        advance(1,true);
    }
    private void incomingPrepared(){incomingReady=true;if(waitingNext&&wantPlay){promote();play();}}
    private void tick(){
        if(!wantPlay){applyVolume();return;}
        if(waitingNext){if(incomingReady&&wantPlay){promote();play();}return;}
        boolean sourceEnding=dj()&&DjPlan.endsAtSourceEnd(source(entry()),entry());
        if(dj()&&isPlaying()){
            long end=PlaybackRange.end(current(),duration());
            if(audition()&&entry().optBoolean("auditionIntro")&&position()>=PlaybackRange.start(current(),duration())+(long)(10000*speed)&&end-position()>15000*speed){
                try{entry().put("auditionIntro",false);}catch(Exception ignored){}previewSeek();return;
            }
            preload();long remaining=end-position();
            long window=DjPlan.matchedOverlap(source(entry()),entry(),source(nextEntry()),nextEntry(),speed,incomingSpeed);
            if(!mixing&&incomingReady&&remaining>30&&remaining<=window*speed){
                try{mixStart=position();mixSpan=Math.max(1,remaining);mixFade=Math.min(mixSpan,DjPlan.safeFade(source(entry()),entry()));mixOutStart=lastGain<0?.70f:Math.max(0,lastGain/duckGain);incoming.setVolume(0,0);incoming.start();mixing=true;lastGain=-1;}catch(Exception ignored){failedIncoming=incomingSpec;cancelIncoming();}
            }
            if(!sourceEnding&&mixing&&remaining<=15){promote();return;}
            if(!sourceEnding&&!mixing&&remaining<=15&&nextEntry()!=null){
                if(incomingReady){promote();if(wantPlay)play();}else waitForNext();return;
            }
        }
        // A stored-file ending completes through MediaPlayer.OnCompletion.
        // Its final recorded samples must survive the50ms timer/15ms margin.
        applyVolume();if(!sourceEnding&&isPlaying()&&position()>=PlaybackRange.end(current(),duration())-15)advance(1,true);
    }
    private void promote(){promote(false);}
    private void promote(boolean skipped){
        if(incoming==null||!incomingReady)return;
        String id=incomingId,finished=queue.optString("currentId");MediaPlayer next=incoming;float rate=incomingSpeed;PlaybackLevel nextLevel=incomingLevel;
        incomingLevel=null;incoming=null;cancelIncoming();if(playerLevel!=null){playerLevel.release();playerLevel=null;}if(player!=null)try{player.release();}catch(Exception ignored){}
        if(queue.optJSONObject("fixedSet")==null)RemixPlan.finish(queue,finished,id,skipped);
        try{queue.put("currentId",id);queue.put("time",next.getCurrentPosition()/1000.0);}catch(Exception ignored){}
        advanceEpoch();
        try{JSONObject e=entry();if(e!=null)e.put("auditionIntro",queue.optBoolean("djAudition"));}catch(Exception ignored){}
        player=next;playerLevel=nextLevel;loaded=id;playingTrack=DjPlan.track(source(entry()),entry(),dj());speed=rate;ready=true;rangeSeeking=false;pendingSeek=0;failedIncoming="";lastGain=-1;
        next.setOnPreparedListener(null);next.setOnSeekCompleteListener(mp->{if(player==mp){rangeSeeking=false;applyVolume();if(wantPlay&&!isPlaying())play();}});
        next.setOnCompletionListener(mp->{if(player==mp)completed();});
        next.setOnErrorListener((mp,what,extra)->{if(player==mp){releasePlayer();wantPlay=false;error="Не удалось воспроизвести песню.";updateSession();}return true;});
        savePosition();applyVolume();updateSession();
    }
    private void advance(int direction,boolean ended){
        JSONArray list=entries();int at=index();if(list.length()==0)return;
        if(direction==1&&(remix()||!"one".equals(queue.optString("repeat")))&&incomingReady&&incomingId.equals(nextEntry()==null?"":nextEntry().optString("id"))){promote(!ended);play();return;}
        if(remix()){
            if(direction<0){seek(0);play();return;}
            String finished=queue.optString("currentId");RemixPlan.finish(queue,finished,"",!ended);advanceEpoch();pendingSeek=0;
            if(entries().length()==0){pause();releasePlayer();savePosition();updateSession();return;}
            State.get(this).write("nativeQueue",queue.toString());prepare(true);return;
        }
        if(queue.optJSONObject("fixedSet")==null&&ended&&"one".equals(queue.optString("repeat"))){if(audition())try{entry().put("auditionIntro",true);}catch(Exception ignored){}seek(0);play();return;}
        int next=at+direction;
        if(next>=list.length()){
            if(queue.optJSONObject("fixedSet")!=null||!"all".equals(queue.optString("repeat"))){pause();seek(0);return;}
            if(queue.optBoolean("shuffle")){
                ArrayList<JSONObject> values=new ArrayList<>();for(int i=0;i<list.length();i++)values.add(list.optJSONObject(i));Collections.shuffle(values);list=new JSONArray(values);try{queue.put("entries",list);}catch(Exception ignored){}
            }next=0;
        }
        if(next<0)next=0;
        try{queue.put("currentId",list.getJSONObject(next).getString("id"));queue.put("time",0);}catch(Exception ignored){}
        advanceEpoch();
        pendingSeek=0;State.get(this).write("nativeQueue",queue.toString());prepare(true);
    }
    private void releasePlayer(){cancelIncoming();if(playerLevel!=null){playerLevel.release();playerLevel=null;}playingTrack=null;if(player!=null){try{player.release();}catch(Exception ignored){}player=null;}ready=false;rangeSeeking=false;lastGain=-1f;loaded="";}
    private void savePosition(){stampEpoch();try{queue.put("time",position()/1000.0);State.get(this).write("nativeQueue",queue.toString());}catch(Exception ignored){}}
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
            .setVisibility(Notification.VISIBILITY_PUBLIC).setOnlyAlertOnce(true).setShowWhen(false).setOngoing(wantPlay&&isPlaying()).setCategory(Notification.CATEGORY_TRANSPORT)
            .addAction(new Notification.Action.Builder(android.R.drawable.ic_media_previous,"Предыдущая",action("previous",1)).build())
            .addAction(new Notification.Action.Builder(wantPlay&&isPlaying()?android.R.drawable.ic_media_pause:android.R.drawable.ic_media_play,wantPlay&&isPlaying()?"Пауза":"Играть",action(wantPlay&&isPlaying()?"pause":"play",2)).build())
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
        PlaybackState.Builder state=new PlaybackState.Builder().setActions(actions).setState(!wantPlay?PlaybackState.STATE_PAUSED:isPlaying()?PlaybackState.STATE_PLAYING:PlaybackState.STATE_BUFFERING,position(),wantPlay&&isPlaying()?speed:0);
        if(!error.isEmpty())state.setErrorMessage(error);session.setPlaybackState(state.build());
        ((NotificationManager)getSystemService(NOTIFICATION_SERVICE)).notify(NOTIFICATION,notification());
    }
    static JSONObject lightQueue(JSONObject original){
        JSONObject result=new JSONObject();
        try{
            java.util.Iterator<String> keys=original.keys();while(keys.hasNext()){String key=keys.next();if(!"entries".equals(key))result.put(key,original.opt(key));}
            JSONArray all=original.optJSONArray("entries"),visibleEntries=new JSONArray();
            if(all!=null)for(int i=0;i<all.length();i++){
                JSONObject source=all.optJSONObject(i);if(source==null)continue;JSONObject e=new JSONObject();keys=source.keys();
                while(keys.hasNext()){String key=keys.next();if(!"remixAnchors".equals(key)&&!"remixAcousticAnchors".equals(key)&&!"remixPool".equals(key)&&!"remixCues".equals(key))e.put(key,source.opt(key));}
                visibleEntries.put(e);
            }
            result.put("entries",visibleEntries);result.put("remixMetadataOmitted",true);
        }catch(Exception ignored){}
        return result;
    }
    JSONObject snapshot(){return snapshot(false);}
    JSONObject snapshot(boolean light){
        stampEpoch();JSONObject visibleQueue=light?lightQueue(queue):queue;
        JSONObject state=new JSONObject();try{state.put("queue",visibleQueue);state.put("paused",!wantPlay);state.put("playing",wantPlay&&isPlaying());state.put("buffering",wantPlay&&!isPlaying());state.put("time",position()/1000.0);state.put("duration",duration()/1000.0);state.put("error",error);state.put("waitingNext",waitingNext);}catch(Exception ignored){}return state;
    }
    @Override public void onDestroy(){handler.removeCallbacks(librarySync);handler.removeCallbacks(rangeTick);savePosition();releasePlayer();audio.abandonAudioFocusRequest(focus);if(noisy!=null)unregisterReceiver(noisy);if(session!=null)session.release();instance=null;super.onDestroy();}
}
