package com.aidj.music;
import org.json.*;
import java.util.*;

/** A small moving pool of original tracks, not an expanded/fixed audio montage. */
final class RemixPlan {
    static boolean enabled(JSONObject q){return q.optBoolean("djEnabled")&&q.optBoolean("djRemix");}
    private static JSONObject source(JSONObject q,JSONObject e){JSONObject ts=q.optJSONObject("tracks");return ts==null||e==null?null:ts.optJSONObject(e.optString("trackId"));}
    private static ArrayList<JSONObject> list(JSONObject q){ArrayList<JSONObject> out=new ArrayList<>();JSONArray a=q.optJSONArray("entries");if(a!=null)for(int i=0;i<a.length();i++){JSONObject e=a.optJSONObject(i);if(e!=null)out.add(e);}return out;}
    static double desiredDuration(JSONObject q){double value=q.optDouble("remixDurationSeconds",12);return Double.isFinite(value)?Math.max(10,Math.min(120,value)):12;}
    private static double energy(double value){return Double.isFinite(value)?Math.max(0,Math.min(1,value)):.5;}
    private static ArrayList<JSONObject> anchorCues(JSONObject q,JSONObject e,JSONObject t,boolean acoustic){
        ArrayList<JSONObject> out=new ArrayList<>();JSONObject anchors=e.optJSONObject(acoustic?"remixAcousticAnchors":"remixAnchors");
        if(anchors==null||anchors.optInt("version")!=1||(acoustic?!anchors.optBoolean("needsReview")||!"acoustic".equals(anchors.optString("quality"))||anchors.optBoolean("wordGuarded"):!anchors.optBoolean("wordGuarded")))return out;
        double scale=anchors.optBoolean("packed")?1000:1,bpm=anchors.optDouble("bpm",Double.NaN),total=t.optDouble("duration"),target=desiredDuration(q);
        if(!Double.isFinite(bpm)||(bpm!=0&&(bpm<60||bpm>210)))return out;
        JSONArray entries=anchors.optJSONArray("entries"),rawExits=anchors.optJSONArray("exits");if(entries==null||rawExits==null)return out;
        boolean flat=anchors.optBoolean("flat");ArrayList<double[]> exits=new ArrayList<>();
        for(int i=0;i<Math.min(256,flat?rawExits.length()/3:rawExits.length());i++){
            JSONArray x=flat?rawExits:rawExits.optJSONArray(i);if(x==null)continue;int offset=flat?i*3:0;
            double at=x.optDouble(offset,Double.NaN)/scale,fade=x.optDouble(offset+1,Double.NaN)/scale;
            if(Double.isFinite(at)&&Double.isFinite(fade)&&at>=0&&at<=total+.1&&fade>=(acoustic?.5:.15)&&fade<=2)exits.add(new double[]{at,fade,energy(x.optDouble(offset+2,.5*scale)/scale)});
        }
        Collections.sort(exits,(a,b)->Double.compare(a[0],b[0]));Set<String> seen=new HashSet<>();
        for(int i=0;i<Math.min(256,flat?entries.length()/2:entries.length());i++){
            JSONArray x=flat?entries:entries.optJSONArray(i);if(x==null)continue;int offset=flat?i*2:0;double start=x.optDouble(offset,Double.NaN)/scale;if(!Double.isFinite(start)||start<0||start>total-8)continue;
            int lo=0,hi=exits.size();while(lo<hi){int mid=(lo+hi)/2;if(exits.get(mid)[0]<start+target)lo=mid+1;else hi=mid;}
            for(int j=Math.max(0,lo-1);j<Math.min(exits.size(),lo+1);j++){
                double[] end=exits.get(j);double span=end[0]-start;if(span<8||span>123||Math.abs(span-target)>3)continue;
                try{
                    JSONObject cue=new JSONObject().put("start",start).put("end",end[0]).put("bpm",bpm).put("fade",end[1]).put("mix",Math.max(3.2,end[1])).put("energy",(energy(x.optDouble(offset+1,.5*scale)/scale)+end[2])/2).put("entryVerified",!acoustic).put("wordGuarded",!acoustic);
                    if(acoustic)cue.put("acoustic",true).put("quality","acoustic").put("needsReview",true);
                    double grid=anchors.optDouble("grid",Double.NaN);if(bpm>0&&Double.isFinite(grid)&&grid>=0&&grid<60/bpm)cue.put("grid",grid);
                    if(seen.add(signature(cue)))out.add(cue);
                }catch(Exception ignored){}
            }
        }
        return out;
    }
    private static ArrayList<JSONObject> cues(JSONObject q,JSONObject e){
        ArrayList<JSONObject> out=new ArrayList<>();JSONObject t=source(q,e);if(t==null||DjPlan.manual(t)||e.optBoolean("djFull"))return out;
        ArrayList<JSONObject> dynamic=anchorCues(q,e,t,false);dynamic.addAll(anchorCues(q,e,t,true));if(!dynamic.isEmpty())return dynamic;
        JSONArray compact=e.optJSONArray("remixPool"),a=compact==null?e.optJSONArray("remixCues"):compact;if(a==null)return out;Set<String> seen=new HashSet<>();
        for(int i=0;i<Math.min(a.length(),32);i++){
            JSONObject c;
            if(compact!=null){
                JSONArray values=a.optJSONArray(i);if(values==null||values.length()<9)continue;
                try{
                    c=new JSONObject().put("start",values.optDouble(0,Double.NaN)).put("end",values.optDouble(1,Double.NaN)).put("bpm",values.optDouble(2,Double.NaN)).put("fade",values.optDouble(3,Double.NaN)).put("mix",values.optDouble(4,Double.NaN)).put("energy",values.optDouble(6,.5)).put("wordGuarded",values.optInt(7,0)==1).put("entryVerified",values.optInt(8,0)==1);
                    if(values.optInt(9,0)==1&&values.optInt(10,0)==1)c.put("acoustic",true).put("needsReview",true).put("quality","acoustic");
                    double grid=values.optDouble(5,Double.NaN);if(Double.isFinite(grid))c.put("grid",grid);
                }catch(Exception ignored){continue;}
            }else c=a.optJSONObject(i);
            if(c==null||!c.optBoolean("entryVerified")&&!DjPlan.acoustic(c))continue;
            double start=c.optDouble("start",-1),end=c.optDouble("end",-1),span=end-start,bpm=c.optDouble("bpm",0),fade=c.optDouble("fade",0);
            if(!Double.isFinite(start)||!Double.isFinite(end)||start<0||Math.abs(span-desiredDuration(q))>3||span<8||span>30||end>t.optDouble("duration")+.1||!Double.isFinite(bpm)||(bpm!=0&&(bpm<60||bpm>210))||!Double.isFinite(fade)||fade<DjPlan.minimumFade(c,true)||fade>8)continue;
            if(DjPlan.guarded(c)&&c.has("mix")){double mix=c.optDouble("mix",Double.NaN);if(!Double.isFinite(mix)||mix<Math.max(1.5,fade)||mix>8)continue;}
            String key=signature(c);if(seen.add(key))out.add(c);
        }
        return out;
    }
    private static String signature(JSONObject cue){return Math.round(cue.optDouble("start",0)*1000)+":"+Math.round(cue.optDouble("end",0)*1000);}
    private static LinkedHashSet<String> used(JSONObject entry,ArrayList<JSONObject> options){
        LinkedHashSet<String> out=new LinkedHashSet<>();JSONArray saved=entry.optJSONArray("remixUsed");
        if(saved!=null){for(int i=0;i<Math.min(3,saved.length());i++){String key=saved.optString(i,"");if(key.matches("[0-9]{1,9}:[0-9]{1,9}"))out.add(key);}}
        else for(int i=0;i<Math.min(Math.min(3,Math.max(0,entry.optInt("remixVisits",0))),options.size());i++)out.add(signature(options.get(i)));
        return out;
    }
    private static double overlap(JSONObject cue,Set<String> used){
        double start=cue.optDouble("start"),end=cue.optDouble("end"),score=0;
        for(String key:used){String[] limits=key.split(":");if(limits.length!=2)continue;try{double lo=Long.parseLong(limits[0])/1000.,hi=Long.parseLong(limits[1])/1000.;score=Math.max(score,Math.max(0,Math.min(end,hi)-Math.max(start,lo))/Math.max(.001,Math.min(end-start,hi-lo)));}catch(Exception ignored){}}
        return score;
    }
    private static JSONObject available(JSONObject q,JSONObject entry,double target,boolean preserve){
        if(entry.optInt("remixVisits",0)>=3)return null;ArrayList<JSONObject> options=cues(q,entry);Set<String> used=used(entry,options);
        // A metadata refresh must not change an already preloaded successor.
        JSONObject old=entry.optJSONObject("dj");
        if(preserve&&entry.optBoolean("remix")&&old!=null&&(old.optBoolean("entryVerified")||DjPlan.acoustic(old))&&old.optDouble("end")-old.optDouble("start")<=123&&DjPlan.cue(source(q,entry),entry,true)!=null&&!used.contains(signature(old)))return old;
        JSONObject best=null;double bestScore=Double.POSITIVE_INFINITY,desired=desiredDuration(q);boolean nearby=false,wordFit=false;
        for(JSONObject cue:options)if(!used.contains(signature(cue))&&Math.abs(cue.optDouble("end")-cue.optDouble("start")-desired)<=3){nearby=true;if(!DjPlan.acoustic(cue))wordFit=true;}
        for(JSONObject cue:options){
            String key=signature(cue);if(used.contains(key)||wordFit&&DjPlan.acoustic(cue))continue;
            double span=cue.optDouble("end")-cue.optDouble("start"),bpm=cue.optDouble("bpm",0),energy=cue.optDouble("energy",.5);if(!Double.isFinite(energy))energy=.5;
            if(nearby&&Math.abs(span-desired)>3)continue;
            double durationCost=Math.max(0,Math.abs(span-desired)-3)/3;
            double tempoCost=target>0&&bpm>0?Math.max(0,Math.abs(Math.log(bpm/target))-.03)*2:0;
            String seed=entry.optString("id")+"|"+entry.optInt("remixVisits",0)+"|"+desired+"|"+key;
            int hash=seed.hashCode();hash=(hash^(hash>>>16))*0x45d9f3b;hash=(hash^(hash>>>16))*0x45d9f3b;hash^=hash>>>16;
            double jitter=(hash&0x7fffffff)/2147483647.0*.12;
            double score=overlap(cue,used)*8+durationCost+(1-Math.max(0,Math.min(1,energy)))*.4+tempoCost+jitter;
            if(score<bestScore){bestScore=score;best=cue;}
        }
        return best;
    }
    private static JSONObject assign(JSONObject q,JSONObject e,double target,boolean preserve){
        JSONObject candidate=available(q,e,target,preserve);
        try{
            if(candidate==null){e.remove("remix");e.remove("dj");if(!e.optBoolean("djFull")&&!DjPlan.manual(source(q,e)))e.put("remixNeedsReview",true).put("remixFallback","full-source");return null;}
            if(preserve&&e.optBoolean("remix")&&candidate==e.optJSONObject("dj"))return candidate;
            JSONObject cue=new JSONObject(candidate.toString());double bpm=cue.optDouble("bpm",0),rate=bpm>0&&target>0?Math.max(.97,Math.min(1.03,target/bpm)):1;
            cue.put("rate",rate);e.put("dj",cue);e.put("remix",true);e.put("remixNeedsReview",DjPlan.acoustic(cue));e.remove("remixFallback");return cue;
        }catch(Exception ignored){return null;}
    }
    static void arrange(JSONObject q){arrange(q,"");}
    static void arrange(JSONObject q,String pinnedNext){
        if(!enabled(q))return;ArrayList<JSONObject> values=list(q);if(values.isEmpty())return;
        String current=q.optString("currentId");int at=-1;for(int i=0;i<values.size();i++)if(values.get(i).optString("id").equals(current)){at=i;break;}
        if(at<0)at=0;
        // Entries before a selected current song have already been played/skipped.
        if(at>0)values=new ArrayList<>(values.subList(at,values.size()));
        JSONObject first=values.get(0);
        JSONObject left=first.optJSONObject("dj");
        if(first.optBoolean("djFull")||DjPlan.manual(source(q,first)))left=null;
        else if(!first.optBoolean("remix")||left==null||used(first,cues(q,first)).contains(signature(left)))left=assign(q,first,0,true);
        double target=left==null?0:left.optDouble("bpm",0)*left.optDouble("rate",1);
        int best=1;double score=Double.POSITIVE_INFINITY;
        for(int i=1;i<Math.min(values.size(),10);i++){
            JSONObject e=values.get(i),option=available(q,e,target,e.optString("id").equals(pinnedNext));
            boolean usable=option!=null;double bpm=usable?option.optDouble("bpm",0):0;
            double cost=(i-1)*.20+(usable?0:3)+(target>0&&bpm>0?Math.min(2,Math.abs(Math.log(bpm/target))*5):.4);
            if(e.optString("trackId").equals(first.optString("trackId")))cost+=3;
            if(cost<score){score=cost;best=i;}
        }
        if(values.size()>1&&values.get(1).optString("id").equals(pinnedNext))best=1;
        if(values.size()>1){JSONObject chosen=values.remove(best);assign(q,chosen,target,chosen.optString("id").equals(pinnedNext));values.add(1,chosen);}
        try{q.put("entries",new JSONArray(values));q.put("currentId",first.optString("id"));}catch(Exception ignored){}
    }
    static void finish(JSONObject q,String finishedId,String promotedId,boolean skipped){
        if(!enabled(q))return;ArrayList<JSONObject> values=list(q);JSONObject finished=null;
        for(int i=0;i<values.size();i++)if(finishedId.equals(values.get(i).optString("id"))){finished=values.remove(i);break;}
        if(finished!=null&&!skipped&&finished.optBoolean("remix")&&!finished.optBoolean("djFull")){
            int visits=finished.optInt("remixVisits",0)+1;LinkedHashSet<String> heard=used(finished,cues(q,finished));JSONObject played=finished.optJSONObject("dj");if(played!=null)heard.add(signature(played));
            try{finished.put("remixUsed",new JSONArray(new ArrayList<>(heard).subList(0,Math.min(3,heard.size()))));finished.put("remixVisits",visits);finished.remove("dj");finished.remove("remix");}catch(Exception ignored){}
            if(visits<3&&available(q,finished,0,false)!=null)values.add(Math.min(3,values.size()),finished);
        }
        try{
            q.put("entries",new JSONArray(values));q.put("currentId",values.isEmpty()?"":promotedId.isEmpty()?values.get(0).optString("id"):promotedId);
            q.put("time",0);
        }catch(Exception ignored){}
        arrange(q);
    }
}
