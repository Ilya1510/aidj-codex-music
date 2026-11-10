package com.aidj.music;
import org.json.*;

/** Queue-local interpretation. Never rewrites an original or a manual excerpt. */
final class DjPlan {
    static boolean manual(JSONObject t){return t!=null&&(t.optDouble("rangeStart",0)>.001||t.optDouble("rangeEnd",t.optDouble("duration"))<t.optDouble("duration")-.1);}
    static double lower(JSONObject t){double total=t==null?0:t.optDouble("duration",0),start=t==null?0:t.optDouble("rangeStart",0);return Double.isFinite(total)&&Double.isFinite(start)?Math.max(0,Math.min(total,start)):0;}
    static double upper(JSONObject t){double total=t==null?0:t.optDouble("duration",0),end=t==null?0:t.optDouble("rangeEnd",total);return Double.isFinite(total)&&Double.isFinite(end)?Math.max(lower(t),Math.min(total,end)):lower(t);}
    static JSONObject fixed(JSONObject t,JSONObject entry){
        JSONObject c=entry==null?null:entry.optJSONObject("fixed");if(t==null||c==null||c.has("sourceMd5")&&!c.optString("sourceMd5").equals(t.optString("md5")))return null;
        for(String k:new String[]{"start","end","fadeIn","fadeOut","transitionSeconds","rate"})if(!(c.opt(k) instanceof Number)||!Double.isFinite(c.optDouble(k)))return null;
        double start=c.optDouble("start"),end=c.optDouble("end"),span=end-start,limit=Math.min(8,span/2),a=c.optDouble("fadeIn"),b=c.optDouble("fadeOut"),overlap=c.optDouble("transitionSeconds"),rate=c.optDouble("rate");String kind=c.optString("transitionKind");
        if(start<lower(t)-.001||end>upper(t)+.001||span<.2||a<0||a>limit||b<0||b>limit||overlap<0||overlap>limit||rate<.97||rate>1.03||!("clean-cut".equals(kind)||"crossfade".equals(kind))||"clean-cut".equals(kind)&&overlap!=0)return null;
        return c;
    }
    static boolean endsAtSourceEnd(JSONObject source,JSONObject entry){
        JSONObject c=fixed(source,entry);if(c==null)return false;
        if(!(source.opt("duration") instanceof Number))return false;
        double duration=source.optDouble("duration");
        // A hand-trimmed end remains an internal cut even if very close to EOF.
        return Double.isFinite(duration)&&duration>0&&upper(source)>=duration&&Math.abs(c.optDouble("end")-duration)<=.001;
    }
    /** Each deck keeps its authored fades before, during, and after a blend. */
    static double fixedEnvelope(JSONObject source,JSONObject entry,long position,long duration,float rate){
        JSONObject c=fixed(source,entry);if(c==null)return 1;
        double start=c.optDouble("start")*1000,end=endsAtSourceEnd(source,entry)&&duration>0?duration:c.optDouble("end")*1000;
        double fadeIn=c.optDouble("fadeIn")*1000*rate,fadeOut=c.optDouble("fadeOut")*1000*rate;
        double intro=fadeIn>0?Math.max(0,Math.min(1,(position-start)/fadeIn)):1;
        double tail=fadeOut>0?Math.max(0,Math.min(1,(end-position)/fadeOut)):1;
        return intro*tail;
    }
    static boolean timed(JSONObject track,JSONObject entry){
        if(track==null||!(track.opt("duration") instanceof Number)||entry==null||!entry.optBoolean("remix")||entry.optBoolean("djFull"))return false;
        JSONObject c=entry.optJSONObject("dj");if(c==null||!Boolean.TRUE.equals(c.opt("timedFallback"))||!"timed".equals(c.optString("quality"))||!Boolean.TRUE.equals(c.opt("needsReview"))||!Boolean.FALSE.equals(c.opt("entryVerified"))||!Boolean.FALSE.equals(c.opt("wordGuarded"))||c.optBoolean("acoustic"))return false;
        for(String key:new String[]{"start","end","targetSeconds","sourceDuration","bpm","fade","mix"})if(!(c.opt(key) instanceof Number)||!Double.isFinite(c.optDouble(key)))return false;
        double start=c.optDouble("start"),end=c.optDouble("end"),target=c.optDouble("targetSeconds"),total=c.optDouble("sourceDuration"),actual=track.optDouble("duration"),span=end-start,bpm=c.optDouble("bpm"),fade=c.optDouble("fade"),mix=c.optDouble("mix");
        if(!Double.isFinite(actual)||Math.abs(total-actual)>.001||target<10||target>120||total<8||start<0||end>total+.001||span<8||span>123||(bpm!=0&&(bpm<60||bpm>210))||fade<.8||fade>1.2||mix<3.2||mix>8)return false;
        double lo=lower(track),hi=upper(track);if(start<lo-.001||end>hi+.001)return false;
        if(manual(track)||c.has("sourceRangeStart")||c.has("sourceRangeEnd")){
            if(!(c.opt("sourceRangeStart") instanceof Number)||!(c.opt("sourceRangeEnd") instanceof Number))return false;
            double savedLo=c.optDouble("sourceRangeStart"),savedHi=c.optDouble("sourceRangeEnd");
            if(!Double.isFinite(savedLo)||!Double.isFinite(savedHi)||savedLo<0||savedHi>total||savedHi<=savedLo||Math.abs(savedLo-lo)>.001||Math.abs(savedHi-hi)>.001)return false;
        }
        return hi-lo<target?Math.abs(start-lo)<.001&&Math.abs(end-hi)<.001:Math.abs(span-target)<=3;
    }
    static boolean acoustic(JSONObject cue){return cue!=null&&cue.optBoolean("acoustic")&&cue.optBoolean("needsReview")&&"acoustic".equals(cue.optString("quality"));}
    static boolean guarded(JSONObject cue){return cue!=null&&cue.optBoolean("wordGuarded")&&cue.optBoolean("entryVerified");}
    static double minimumFade(JSONObject cue,boolean remix){return guarded(cue)?(remix?.15:.25):.5;}
    static JSONObject cue(JSONObject t,JSONObject entry,boolean enabled){
        if(!enabled||t==null||entry==null||entry.optBoolean("djFull")||manual(t)&&!entry.optBoolean("remix"))return null;
        JSONObject c=entry.optJSONObject("dj");if(c==null)return null;
        boolean timed=timed(t,entry);
        if((c.has("timedFallback")&&!Boolean.FALSE.equals(c.opt("timedFallback"))||"timed".equals(c.optString("quality")))&&!timed)return null;
        double start=c.optDouble("start",-1),end=c.optDouble("end",-1),bpm=c.optDouble("bpm",0),fade=c.optDouble("fade",0);
        if(!Double.isFinite(start)||!Double.isFinite(end)||start<0||end-start<(entry.optBoolean("remix")&&(c.optBoolean("entryVerified")||acoustic(c)||timed)?8:30)||end>t.optDouble("duration")+.1||!Double.isFinite(bpm)||(bpm!=0&&bpm<60)||bpm>210||!Double.isFinite(fade)||fade<minimumFade(c,entry.optBoolean("remix"))||fade>8)return null;
        if(entry.optBoolean("remix")&&(end-start>123||start<lower(t)-.001||end>upper(t)+.001))return null;
        if(guarded(c)&&c.has("mix")){double mix=c.optDouble("mix",Double.NaN);if(!Double.isFinite(mix)||mix<Math.max(1.5,fade)||mix>8)return null;}
        return c;
    }
    static JSONObject track(JSONObject t,JSONObject entry,boolean enabled){
        JSONObject f=enabled?fixed(t,entry):null;if(f!=null)try{return new JSONObject(t.toString()).put("rangeStart",f.getDouble("start")).put("rangeEnd",f.getDouble("end"));}catch(Exception ignored){}
        JSONObject c=cue(t,entry,enabled);if(c==null)return t;
        try{return new JSONObject(t.toString()).put("rangeStart",Math.max(lower(t),c.optBoolean("entryVerified")||acoustic(c)||timed(t,entry)?c.optDouble("start"):0)).put("rangeEnd",Math.min(upper(t),c.getDouble("end")));}catch(Exception e){return t;}
    }
    static long previewTail(JSONObject track,long duration,float rate){return Math.max(PlaybackRange.start(track,duration),PlaybackRange.end(track,duration)-(long)(10000*rate));}
    static float rate(JSONObject t,JSONObject entry,boolean enabled){
        JSONObject f=enabled?fixed(t,entry):null;if(f!=null)return (float)f.optDouble("rate",1);
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
        JSONObject f=fixed(out,entry);if(f!=null)return (long)(f.optDouble("transitionSeconds")*1000);
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
    static long safeFade(JSONObject t,JSONObject entry){JSONObject f=fixed(t,entry);if(f!=null)return (long)(f.optDouble("fadeOut")*1000);JSONObject c=cue(t,entry,true);return c==null?1200:(long)(c.optDouble("fade",1.2)*1000);}
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
