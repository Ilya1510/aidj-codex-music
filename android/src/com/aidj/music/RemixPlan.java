package com.aidj.music;
import org.json.*;
import java.util.*;

/** A small moving pool of original tracks, not an expanded/fixed audio montage. */
final class RemixPlan {
    static boolean enabled(JSONObject q){return q.optBoolean("djEnabled")&&q.optBoolean("djRemix");}
    private static JSONObject source(JSONObject q,JSONObject e){JSONObject ts=q.optJSONObject("tracks");return ts==null||e==null?null:ts.optJSONObject(e.optString("trackId"));}
    private static ArrayList<JSONObject> list(JSONObject q){ArrayList<JSONObject> out=new ArrayList<>();JSONArray a=q.optJSONArray("entries");if(a!=null)for(int i=0;i<a.length();i++){JSONObject e=a.optJSONObject(i);if(e!=null)out.add(e);}return out;}
    private static ArrayList<JSONObject> cues(JSONObject q,JSONObject e){
        ArrayList<JSONObject> out=new ArrayList<>();JSONObject t=source(q,e);if(t==null||DjPlan.manual(t)||e.optBoolean("djFull"))return out;
        JSONArray a=e.optJSONArray("remixCues");if(a==null)return out;Set<String> seen=new HashSet<>();
        for(int i=0;i<Math.min(a.length(),24);i++){
            JSONObject c=a.optJSONObject(i);if(c==null||!c.optBoolean("entryVerified"))continue;
            double start=c.optDouble("start",-1),end=c.optDouble("end",-1),span=end-start,bpm=c.optDouble("bpm",0),fade=c.optDouble("fade",0);
            if(!Double.isFinite(start)||!Double.isFinite(end)||start<0||span<8||span>30||end>t.optDouble("duration")+.1||!Double.isFinite(bpm)||(bpm!=0&&(bpm<60||bpm>210))||!Double.isFinite(fade)||fade<.5||fade>8)continue;
            String key=start+":"+end;if(seen.add(key))out.add(c);
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
    private static JSONObject available(JSONObject q,JSONObject entry){
        if(entry.optInt("remixVisits",0)>=3)return null;ArrayList<JSONObject> options=cues(q,entry);Set<String> used=used(entry,options);
        // A metadata refresh must not change an already preloaded successor.
        JSONObject old=entry.optJSONObject("dj");
        if(entry.optBoolean("remix")&&old!=null&&old.optBoolean("entryVerified")&&old.optDouble("end")-old.optDouble("start")<=30&&DjPlan.cue(source(q,entry),entry,true)!=null&&!used.contains(signature(old)))return old;
        for(JSONObject cue:options)if(!used.contains(signature(cue)))return cue;
        return null;
    }
    private static JSONObject assign(JSONObject q,JSONObject e,double target){
        JSONObject candidate=available(q,e);
        try{
            if(candidate==null){e.remove("remix");return null;}
            JSONObject cue=new JSONObject(candidate.toString());double bpm=cue.optDouble("bpm",0),rate=bpm>0&&target>0?Math.max(.97,Math.min(1.03,target/bpm)):1;
            if(e.optBoolean("remix")&&candidate==e.optJSONObject("dj")&&Math.abs(rate-candidate.optDouble("rate",1))<.000001)return candidate;
            cue.put("rate",rate);e.put("dj",cue);e.put("remix",true);return cue;
        }catch(Exception ignored){return null;}
    }
    static void arrange(JSONObject q){arrange(q,"");}
    static void arrange(JSONObject q,String pinnedNext){
        if(!enabled(q))return;ArrayList<JSONObject> values=list(q);if(values.isEmpty())return;
        String current=q.optString("currentId");int at=-1;for(int i=0;i<values.size();i++)if(values.get(i).optString("id").equals(current)){at=i;break;}
        if(at<0)at=0;
        // Entries before a selected current song have already been played/skipped.
        if(at>0)values=new ArrayList<>(values.subList(at,values.size()));
        JSONObject first=values.get(0),left=first.optJSONObject("dj");
        if(first.optBoolean("djFull")||DjPlan.manual(source(q,first)))left=null;
        else if(!first.optBoolean("remix")||left==null||used(first,cues(q,first)).contains(signature(left)))left=assign(q,first,0);
        double target=left==null?0:left.optDouble("bpm",0)*left.optDouble("rate",1);
        int best=1;double score=Double.POSITIVE_INFINITY;
        for(int i=1;i<Math.min(values.size(),10);i++){
            JSONObject e=values.get(i),option=available(q,e);
            boolean usable=option!=null;double bpm=usable?option.optDouble("bpm",0):0;
            double cost=(i-1)*.20+(usable?0:3)+(target>0&&bpm>0?Math.min(2,Math.abs(Math.log(bpm/target))*5):.4);
            if(e.optString("trackId").equals(first.optString("trackId")))cost+=3;
            if(cost<score){score=cost;best=i;}
        }
        if(values.size()>1&&values.get(1).optString("id").equals(pinnedNext))best=1;
        if(values.size()>1){JSONObject chosen=values.remove(best);assign(q,chosen,target);values.add(1,chosen);}
        try{q.put("entries",new JSONArray(values));q.put("currentId",first.optString("id"));}catch(Exception ignored){}
    }
    static void finish(JSONObject q,String finishedId,String promotedId,boolean skipped){
        if(!enabled(q))return;ArrayList<JSONObject> values=list(q);JSONObject finished=null;
        for(int i=0;i<values.size();i++)if(finishedId.equals(values.get(i).optString("id"))){finished=values.remove(i);break;}
        if(finished!=null&&!skipped&&finished.optBoolean("remix")&&!finished.optBoolean("djFull")){
            int visits=finished.optInt("remixVisits",0)+1;LinkedHashSet<String> heard=used(finished,cues(q,finished));JSONObject played=finished.optJSONObject("dj");if(played!=null)heard.add(signature(played));
            try{finished.put("remixUsed",new JSONArray(new ArrayList<>(heard).subList(0,Math.min(3,heard.size()))));finished.put("remixVisits",visits);finished.remove("dj");finished.remove("remix");}catch(Exception ignored){}
            if(visits<3&&available(q,finished)!=null)values.add(Math.min(3,values.size()),finished);
        }
        try{
            q.put("entries",new JSONArray(values));q.put("currentId",values.isEmpty()?"":promotedId.isEmpty()?values.get(0).optString("id"):promotedId);
            q.put("time",0);
        }catch(Exception ignored){}
        arrange(q);
    }
}
