"""Forecast real RemixPlan transitions without touching playback or persisted state."""
from tests.test_native_downloads import compile_probe


def test_remix_preview_follows_native_execution_and_caches_only_matching_snapshots(tmp_path):
    fixtures={
        'com/aidj/music/RemixPreviewProbe.java':r'''package com.aidj.music;
import org.json.*;import java.util.*;
final class State {
    String saved="{}";int reads=0;
    String read(String key,String fallback){if(!key.equals("nativeQueue"))throw new AssertionError("unexpected state read");reads++;return saved;}
}
public final class RemixPreviewProbe {
    static void check(boolean v,String label){if(!v)throw new AssertionError(label);}
    static JSONObject cue(double start,double bpm){return new JSONObject().put("start",start).put("end",start+12).put("bpm",bpm).put("fade",.8).put("mix",3.2).put("energy",.8).put("wordGuarded",true).put("entryVerified",true);}
    static JSONObject queue(int visits,int count){
        JSONObject tracks=new JSONObject(),q=new JSONObject().put("djEnabled",true).put("djRemix",true).put("remixMaxVisits",visits).put("remixLookahead",10).put("uiRevision",10).put("currentId","e0").put("tracks",tracks);
        JSONArray entries=new JSONArray();q.put("entries",entries);
        for(int i=0;i<count;i++){
            double bpm=i==1?65:128;JSONArray cues=new JSONArray();for(int j=0;j<10;j++)cues.put(cue(20+j*20,bpm));
            tracks.put("t"+i,new JSONObject().put("id","t"+i).put("duration",240));
            entries.put(new JSONObject().put("id","e"+i).put("trackId","t"+i).put("remixCues",cues));
        }
        RemixPlan.arrange(q);return q;
    }
    // Mirror lightQueue's omission of bulky analysis metadata, retaining typed
    // playback controls and selected excerpts. No fixture scheduler is involved.
    static JSONObject light(JSONObject q){JSONObject out=new JSONObject(q.toString());JSONArray entries=out.getJSONArray("entries");for(int i=0;i<entries.length();i++){JSONObject e=entries.getJSONObject(i);for(String k:new String[]{"remixCues","remixPool","remixAnchors","remixAcousticAnchors"})e.remove(k);}out.put("remixMetadataOmitted",true);return out;}
    static JSONObject forecast(RemixPreview planner,State db,JSONObject q){db.saved=q.toString();return planner.plan(db,light(q));}
    static JSONObject entry(JSONObject q,String id){JSONArray all=q.getJSONArray("entries");for(int i=0;i<all.length();i++)if(all.getJSONObject(i).getString("id").equals(id))return all.getJSONObject(i);throw new AssertionError("missing entry "+id);}
    static void matchesActual(JSONObject original,JSONObject preview){
        JSONObject q=new JSONObject(original.toString());JSONArray segments=preview.getJSONArray("segments");
        for(int i=0;i<segments.length();i++){
            JSONArray es=q.getJSONArray("entries");String current=q.getString("currentId"),next="";
            for(int j=0;j<es.length();j++)if(es.getJSONObject(j).getString("id").equals(current)&&j+1<es.length())next=es.getJSONObject(j+1).getString("id");
            RemixPlan.finish(q,current,next,false);
            JSONObject e=entry(q,q.getString("currentId")),source=q.getJSONObject("tracks").getJSONObject(e.getString("trackId")),actual=DjPlan.track(source,e,true),s=segments.getJSONObject(i);
            check(s.getInt("position")==i+1&&s.getString("entryId").equals(e.getString("id"))&&s.getString("trackId").equals(e.getString("trackId")),"forecast positions are native execution positions");
            check(s.getDouble("start")==DjPlan.lower(actual)&&s.getDouble("end")==DjPlan.upper(actual),"forecast intervals are native source intervals");
        }
    }
    public static void main(String[] args)throws Exception{
        State db=new State();RemixPreview planner=new RemixPreview();JSONObject q=queue(3,4);String original=q.toString();
        JSONObject p=forecast(planner,db,q);JSONArray ss=p.getJSONArray("segments");
        check(q.getJSONArray("entries").getJSONObject(1).getString("id").equals("e2"),"fixture must pick a nonsequential tempo match");
        check(ss.getJSONObject(0).getString("entryId").equals("e2"),"first future position honors the already chosen successor");
        check(ss.length()==10,"future positions exclude current and include later visits");
        Set<String> repeats=new HashSet<>();boolean repeated=false,returnedCurrent=false;for(int i=0;i<ss.length();i++){String id=ss.getJSONObject(i).getString("entryId");if(!repeats.add(id))repeated=true;if(id.equals("e0"))returnedCurrent=true;}
        check(repeated&&returnedCurrent,"future plan retains repeated entry positions and returns of the current entry");matchesActual(q,p);
        check(q.toString().equals(original)&&db.saved.equals(original),"simulation must leave source and persisted queue byte-for-byte unchanged");
        int reads=db.reads;p.getJSONArray("segments").getJSONObject(0).put("entryId","corrupted");
        JSONObject cached=planner.plan(db,light(q));check(db.reads==reads&&cached.getJSONArray("segments").getJSONObject(0).getString("entryId").equals("e2"),"cached forecast is detached and avoids repeated durable reads");
        JSONObject onlyTime=light(q).put("time",123.45);planner.plan(db,onlyTime);check(db.reads==reads,"time polls never recompute the scheduler");
        JSONObject changed=new JSONObject(q.toString()).put("remixLookahead",1);db.saved=changed.toString();planner.plan(db,light(changed));check(db.reads==reads+1,"lookahead control invalidates cache even without a new revision");
        changed.put("remixDurationSeconds",10);db.saved=changed.toString();planner.plan(db,light(changed));check(db.reads==reads+2,"duration control invalidates cache");
        q=queue(3,4).put("manualNextId","e1").put("uiRevision",11);RemixPlan.arrange(q);p=forecast(planner,db,q);check(p.getJSONArray("segments").getJSONObject(0).getString("entryId").equals("e1"),"manual next overrides tempo preference");matchesActual(q,p);
        q=queue(1,4).put("uiRevision",12);p=forecast(planner,db,q);check(p.getJSONArray("segments").length()==3,"one visit retires each track after its first segment");
        q=queue(3,1).put("uiRevision",13);p=forecast(planner,db,q);check(p.getJSONArray("segments").length()==2,"single source can return twice with distinct segments");matchesActual(q,p);
        q=queue(3,4).put("uiRevision",14);q.getJSONObject("tracks").getJSONObject("t2").put("rangeStart",35).put("rangeEnd",74);RemixPlan.arrange(q);p=forecast(planner,db,q);matchesActual(q,p);ss=p.getJSONArray("segments");for(int i=0;i<ss.length();i++){JSONObject s=ss.getJSONObject(i);if(s.getString("trackId").equals("t2"))check(s.getDouble("start")>=35&&s.getDouble("end")<=74,"manual source bounds survive every simulated return");}
        // Entry identity stays independent of song identity.
        q=queue(1,3).put("uiRevision",15);entry(q,"e1").put("trackId","t2");RemixPlan.arrange(q);p=forecast(planner,db,q);Set<String> ids=new HashSet<>();ss=p.getJSONArray("segments");for(int i=0;i<ss.length();i++)ids.add(ss.getJSONObject(i).getString("entryId"));check(ids.contains("e1")&&ids.contains("e2"),"duplicate song IDs do not merge queue entries");
        q=queue(3,4).put("uiRevision",16);JSONObject live=light(q);db.saved=new JSONObject(q.toString()).put("uiRevision",15).toString();check(planner.plan(db,live)==null,"stale durable revision must never label a new snapshot");db.saved=q.toString();check(planner.plan(db,live)!=null,"stale mismatch is retried on the next snapshot");
        q.put("uiRevision",17);JSONObject stale=new JSONObject(q.toString()).put("currentId","e2");db.saved=stale.toString();live=light(q);check(planner.plan(db,live)==null,"mismatched durable current entry must be rejected even with the same revision");
        q=queue(10,8).put("uiRevision",18);p=forecast(planner,db,q);check(p.getJSONArray("segments").length()==10,"long plans are bounded at ten future segments");
        check(forecast(planner,db,new JSONObject(q.toString()).put("djRemix",false))==null,"ordinary playback does not receive a remix forecast");
        JSONObject bad=queue(3,2).put("uiRevision",19);bad.getJSONObject("tracks").getJSONObject("t1").remove("duration");p=forecast(planner,db,bad);check(p.getJSONArray("segments").length()==0,"invalid source ranges do not invent a future position");
        System.out.println("PROBE_PASSED scheduler_forecast source_bounds immutable_cache stale_guard bounded no_media");
    }
}''',
    }
    compile_probe(tmp_path,fixtures,['RemixPreview.java','RemixPlan.java','DjPlan.java','PlaybackRange.java'],'RemixPreviewProbe')
