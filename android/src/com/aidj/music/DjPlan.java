package com.aidj.music;
import org.json.*;

/** Queue-local interpretation. Never rewrites an original or a manual excerpt. */
final class DjPlan {
    static boolean manual(JSONObject t){return t!=null&&(t.optDouble("rangeStart",0)>.001||t.optDouble("rangeEnd",t.optDouble("duration"))<t.optDouble("duration")-.1);}
    static JSONObject cue(JSONObject t,JSONObject entry,boolean enabled){
        if(!enabled||t==null||entry==null||entry.optBoolean("djFull")||manual(t))return null;
        JSONObject c=entry.optJSONObject("dj");if(c==null)return null;
        double start=c.optDouble("start",-1),end=c.optDouble("end",-1),bpm=c.optDouble("bpm",0),fade=c.optDouble("fade",0);
        if(!Double.isFinite(start)||!Double.isFinite(end)||start<0||end-start<30||end>t.optDouble("duration")+.1||!Double.isFinite(bpm)||bpm<60||bpm>210||!Double.isFinite(fade)||fade<.3||fade>8)return null;
        return c;
    }
    static JSONObject track(JSONObject t,JSONObject entry,boolean enabled){
        JSONObject c=cue(t,entry,enabled);if(c==null)return t;
        try{return new JSONObject(t.toString()).put("rangeStart",c.getDouble("start")).put("rangeEnd",c.getDouble("end"));}catch(Exception e){return t;}
    }
    static float rate(JSONObject t,JSONObject entry,boolean enabled){
        JSONObject c=cue(t,entry,enabled);double rate=c==null?1:c.optDouble("rate",1);
        return Double.isFinite(rate)&&rate>=.9699&&rate<=1.0301?(float)rate:1f;
    }
    static long overlap(JSONObject t,JSONObject entry,boolean enabled,float rate){
        if(!enabled)return 0;
        JSONObject c=cue(t,entry,true);
        // Fade fits entirely after the outgoing vocal phrase. Unknown tracks use their original ending.
        double seconds=c==null?1.2:Math.min(c.optDouble("fade",1.2)/rate,4*60/(c.optDouble("bpm",120)*rate));
        if(c!=null){double beat=60/(c.optDouble("bpm",120)*rate);int beats=(int)Math.floor((seconds+.0001)/beat);if(beats>=1)seconds=beats*beat;}
        return (long)(Math.max(.15,Math.min(4,seconds))*1000);
    }
    static long matchedOverlap(JSONObject out,JSONObject entry,JSONObject next,JSONObject nextEntry,float rate,float nextRate){
        long window=overlap(out,entry,true,rate);JSONObject left=cue(out,entry,true),right=cue(next,nextEntry,true);
        if(left==null||right==null||!left.has("grid")||!right.has("grid"))return window;
        double bpm=left.optDouble("bpm")*rate,other=right.optDouble("bpm")*nextRate;
        if(Math.abs(bpm-other)>.1)return window;
        double a=(left.optDouble("end")-left.optDouble("grid"))*left.optDouble("bpm")/60;
        double b=(right.optDouble("start")-right.optDouble("grid"))*right.optDouble("bpm")/60;
        double phase=(a-Math.floor(a))-(b-Math.floor(b)),beat=60/bpm;
        double count=Math.floor(window/1000.0/beat-phase)+phase;
        return count>=.5?(long)(count*beat*1000):window;
    }
    static float gain(double progress,boolean incoming){
        double p=Math.max(0,Math.min(1,progress));
        // Equal-power curves with headroom for a pair of correlated/loud masters.
        return (float)(.70*(incoming?Math.sin(p*Math.PI/2):Math.cos(p*Math.PI/2)));
    }
}
