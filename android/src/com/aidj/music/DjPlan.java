package com.aidj.music;
import org.json.*;

/** Queue-local interpretation. Never rewrites an original or a manual excerpt. */
final class DjPlan {
    static boolean manual(JSONObject t){return t!=null&&(t.optDouble("rangeStart",0)>.001||t.optDouble("rangeEnd",t.optDouble("duration"))<t.optDouble("duration")-.1);}
    static boolean timed(JSONObject track,JSONObject entry){
        if(track==null||!(track.opt("duration") instanceof Number)||entry==null||!entry.optBoolean("remix")||entry.optBoolean("djFull")||manual(track))return false;
        JSONObject c=entry.optJSONObject("dj");if(c==null||!Boolean.TRUE.equals(c.opt("timedFallback"))||!"timed".equals(c.optString("quality"))||!Boolean.TRUE.equals(c.opt("needsReview"))||!Boolean.FALSE.equals(c.opt("entryVerified"))||!Boolean.FALSE.equals(c.opt("wordGuarded"))||c.optBoolean("acoustic"))return false;
        for(String key:new String[]{"start","end","targetSeconds","sourceDuration","bpm","fade","mix"})if(!(c.opt(key) instanceof Number)||!Double.isFinite(c.optDouble(key)))return false;
        double start=c.optDouble("start"),end=c.optDouble("end"),target=c.optDouble("targetSeconds"),total=c.optDouble("sourceDuration"),actual=track.optDouble("duration"),span=end-start,bpm=c.optDouble("bpm"),fade=c.optDouble("fade"),mix=c.optDouble("mix");
        if(!Double.isFinite(actual)||Math.abs(total-actual)>.001||target<10||target>120||total<8||start<0||end>total+.001||span<8||span>123||(bpm!=0&&(bpm<60||bpm>210))||fade<.8||fade>1.2||mix<3.2||mix>8)return false;
        return total<target?start==0&&Math.abs(end-total)<.001:Math.abs(span-target)<=3;
    }
    static boolean acoustic(JSONObject cue){return cue!=null&&cue.optBoolean("acoustic")&&cue.optBoolean("needsReview")&&"acoustic".equals(cue.optString("quality"));}
    static boolean guarded(JSONObject cue){return cue!=null&&cue.optBoolean("wordGuarded")&&cue.optBoolean("entryVerified");}
    static double minimumFade(JSONObject cue,boolean remix){return guarded(cue)?(remix?.15:.25):.5;}
    static JSONObject cue(JSONObject t,JSONObject entry,boolean enabled){
        if(!enabled||t==null||entry==null||entry.optBoolean("djFull")||manual(t))return null;
        JSONObject c=entry.optJSONObject("dj");if(c==null)return null;
        boolean timed=timed(t,entry);
        if((c.has("timedFallback")&&!Boolean.FALSE.equals(c.opt("timedFallback"))||"timed".equals(c.optString("quality")))&&!timed)return null;
        double start=c.optDouble("start",-1),end=c.optDouble("end",-1),bpm=c.optDouble("bpm",0),fade=c.optDouble("fade",0);
        if(!Double.isFinite(start)||!Double.isFinite(end)||start<0||end-start<(entry.optBoolean("remix")&&(c.optBoolean("entryVerified")||acoustic(c)||timed)?8:30)||end>t.optDouble("duration")+.1||!Double.isFinite(bpm)||(bpm!=0&&bpm<60)||bpm>210||!Double.isFinite(fade)||fade<minimumFade(c,entry.optBoolean("remix"))||fade>8)return null;
        if(entry.optBoolean("remix")&&end-start>123)return null;
        if(guarded(c)&&c.has("mix")){double mix=c.optDouble("mix",Double.NaN);if(!Double.isFinite(mix)||mix<Math.max(1.5,fade)||mix>8)return null;}
        return c;
    }
    static JSONObject track(JSONObject t,JSONObject entry,boolean enabled){
        JSONObject c=cue(t,entry,enabled);if(c==null)return t;
        try{return new JSONObject(t.toString()).put("rangeStart",c.optBoolean("entryVerified")||acoustic(c)||timed(t,entry)?c.optDouble("start"):0).put("rangeEnd",c.getDouble("end"));}catch(Exception e){return t;}
    }
    static long previewTail(JSONObject track,long duration,float rate){return Math.max(PlaybackRange.start(track,duration),PlaybackRange.end(track,duration)-(long)(10000*rate));}
    static float rate(JSONObject t,JSONObject entry,boolean enabled){
        JSONObject c=cue(t,entry,enabled);if(c!=null&&(c.optDouble("bpm")==0||timed(t,entry)))return 1f;double rate=c==null?1:c.optDouble("rate",1);
        return Double.isFinite(rate)&&rate>=.9699&&rate<=1.0301?(float)rate:1f;
    }
    static long overlap(JSONObject t,JSONObject entry,boolean enabled,float rate){
        if(!enabled)return 0;
        JSONObject c=cue(t,entry,true);
        // Fade fits entirely after the outgoing vocal phrase. Unknown tracks use their original ending.
        double bpm=c==null?0:c.optDouble("bpm",0);
        double seconds=c==null?1.2:c.has("mix")?Math.max(c.optDouble("fade",1.2),c.optDouble("mix",3.2))/rate:c.optDouble("fade",1.2)/rate;
        if(guarded(c)){double mix=c.optDouble("mix",3.2);if(!Double.isFinite(mix))mix=3.2;seconds=Math.max(c.optDouble("fade"),Math.max(1.5,mix))/rate;}
        if(c!=null&&bpm>0){double beat=60/(bpm*rate);seconds=Math.min(seconds,8*beat);int beats=(int)Math.floor((seconds+.0001)/beat);if(beats>=1)seconds=beats*beat;}
        return (long)(Math.max(guarded(c)?1.5:.15,Math.min(4,seconds))*1000);
    }
    static long matchedOverlap(JSONObject out,JSONObject entry,JSONObject next,JSONObject nextEntry,float rate,float nextRate){
        long window=overlap(out,entry,true,rate);JSONObject left=cue(out,entry,true),right=cue(next,nextEntry,true);
        if(left==null||right==null||!left.has("grid")||!right.has("grid"))return window;
        double bpm=left.optDouble("bpm")*rate,other=right.optDouble("bpm")*nextRate;
        if(bpm<=0||other<=0||Math.abs(bpm-other)>.1)return window;
        double a=(left.optDouble("end")-left.optDouble("grid"))*left.optDouble("bpm")/60;
        double b=((right.optBoolean("entryVerified")||acoustic(right)||timed(next,nextEntry)?right.optDouble("start"):0)-right.optDouble("grid"))*right.optDouble("bpm")/60;
        double phase=(a-Math.floor(a))-(b-Math.floor(b)),beat=60/bpm;
        double count=Math.floor(window/1000.0/beat-phase)+phase;
        return count>=.5&&(!guarded(left)||count*beat>=1.5)?(long)(count*beat*1000):window;
    }
    static long safeFade(JSONObject t,JSONObject entry){JSONObject c=cue(t,entry,true);return c==null?1200:(long)(c.optDouble("fade",1.2)*1000);}
    static float gain(double progress,boolean incoming){return gain(progress,incoming,1);}
    static float gain(double progress,boolean incoming,double fadeFraction){
        double p=Math.max(0,Math.min(1,progress)),fraction=Math.max(.001,Math.min(1,fadeFraction)),hold=1-fraction;
        if(hold<.001)return (float)(.70*(incoming?Math.sin(p*Math.PI/2):Math.cos(p*Math.PI/2)));
        // The next song enters quietly; the outgoing vocal stays intact until
        // the known final-word boundary. Only then does its own short fade begin.
        if(p<hold)return incoming?(float)(.25*Math.sin(p/hold*Math.PI/2)):.70f;
        double q=Math.max(0,Math.min(1,(p-hold)/fraction)),out=.70*Math.cos(q*Math.PI/2);
        double in=Math.sqrt(.25*.25*Math.pow(Math.cos(q*Math.PI/2),2)+.70*.70*Math.pow(Math.sin(q*Math.PI/2),2));
        return (float)(incoming?Math.min(in,.99-out):out);
    }
}
