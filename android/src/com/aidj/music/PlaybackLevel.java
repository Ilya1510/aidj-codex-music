package com.aidj.music;

import android.media.MediaPlayer;
import android.media.audiofx.LoudnessEnhancer;
import org.json.JSONObject;

/** One deck's measured level correction; never changes a source or its fade curve. */
final class PlaybackLevel {
    private final MediaPlayer player;
    private LoudnessEnhancer effect;
    private String specification="";
    private float attenuation=1f;

    PlaybackLevel(MediaPlayer player,JSONObject track){this.player=player;update(track);}
    static double correction(JSONObject track){
        JSONObject m=track==null?null:track.optJSONObject("loudness");
        if(m==null||!"ebu-r128-v1".equals(m.optString("method"))||!(m.opt("integratedLufs") instanceof Number)||!(m.opt("truePeakDbtp") instanceof Number))return 0;
        double loudness=m.optDouble("integratedLufs"),peak=m.optDouble("truePeakDbtp");
        if(!Double.isFinite(loudness)||!Double.isFinite(peak)||loudness< -70||loudness>0||peak< -60||peak>6)return 0;
        double delta= -16-loudness,soft=Math.abs(delta)<1.5?0:Math.max(-6,Math.min(6,delta));
        return Math.min(soft,-1-peak);
    }
    boolean update(JSONObject track){
        JSONObject m=track==null?null:track.optJSONObject("loudness");
        String next=m==null?"":m.toString();
        if(next.equals(specification))return false;
        specification=next;releaseEffect();double correction=correction(track);attenuation=correction<0?(float)Math.pow(10,correction/20):1f;
        if(correction>0){
            LoudnessEnhancer created=null;
            try{
                int session=player.getAudioSessionId();if(session<=0)return true;
                created=new LoudnessEnhancer(session);created.setTargetGain((int)Math.floor(correction*100));
                if(created.setEnabled(true)!=0)throw new IllegalStateException("LEVEL_EFFECT_UNAVAILABLE");effect=created;
            }catch(Exception ignored){if(created!=null)try{created.release();}catch(Exception ignoredRelease){}effect=null;}
        }
        return true;
    }
    float gain(float envelope){return Float.isNaN(envelope)?0:Math.max(0,Math.min(1,envelope*attenuation));}
    private void releaseEffect(){if(effect!=null){try{effect.release();}catch(Exception ignored){}effect=null;}}
    void release(){releaseEffect();attenuation=1f;specification="";}
}
