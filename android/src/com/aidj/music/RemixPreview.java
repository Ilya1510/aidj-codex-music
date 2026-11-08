package com.aidj.music;

import org.json.*;

/** Read-only forecast of the same moving pool used by playback. Worker thread only. */
final class RemixPreview {
    private static final int LIMIT=10,MAX_ENTRIES=5000,MAX_JSON=8*1024*1024;
    private Tag cachedTag;
    private String cachedResult;

    // Use playback's typed option interpretation. Neither playhead time nor large
    // anchor graphs participate in the poll cache key.
    private static final class Tag {
        final String currentId,manualNextId,nextId,currentDj,nextDj,currentRange,nextRange;
        final long revision;
        final int entries,visits,lookahead;
        final double duration;
        Tag(JSONObject q){
            currentId=q.optString("currentId");manualNextId=q.optString("manualNextId");revision=q.optLong("uiRevision");
            JSONArray all=q.optJSONArray("entries");entries=all==null?0:all.length();
            visits=RemixPlan.maxVisits(q);lookahead=RemixPlan.lookahead(q);duration=RemixPlan.desiredDuration(q);
            int at=index(q,currentId);JSONObject current=at<0?null:all.optJSONObject(at),next=at<0?null:all.optJSONObject(at+1);
            nextId=next==null?"":next.optString("id");currentDj=spec(current);nextDj=spec(next);currentRange=range(q,current);nextRange=range(q,next);
        }
        private static String spec(JSONObject e){return e==null?"":e.optBoolean("djFull")+"|"+e.optBoolean("remix")+"|"+e.optInt("remixVisits")+"|"+String.valueOf(e.optJSONObject("dj"));}
        private static String range(JSONObject q,JSONObject e){
            JSONObject tracks=q.optJSONObject("tracks"),t=tracks==null||e==null?null:tracks.optJSONObject(e.optString("trackId"));
            return t==null?"":e.optString("trackId")+"|"+t.optDouble("duration")+"|"+DjPlan.lower(t)+"|"+DjPlan.upper(t);
        }
        boolean same(Tag other){
            return other!=null&&revision==other.revision&&entries==other.entries&&visits==other.visits&&lookahead==other.lookahead&&duration==other.duration
                &&currentId.equals(other.currentId)&&manualNextId.equals(other.manualNextId)&&nextId.equals(other.nextId)
                &&currentDj.equals(other.currentDj)&&nextDj.equals(other.nextDj)&&currentRange.equals(other.currentRange)&&nextRange.equals(other.nextRange);
        }
    }
    private static int index(JSONObject q,String id){
        JSONArray entries=q.optJSONArray("entries");if(entries!=null)for(int i=0;i<entries.length();i++){JSONObject e=entries.optJSONObject(i);if(e!=null&&id.equals(e.optString("id")))return i;}return -1;
    }
    private static void restrictSources(JSONObject q)throws JSONException{
        JSONObject originals=q.optJSONObject("tracks"),tracks=new JSONObject();JSONArray entries=q.optJSONArray("entries");
        if(originals!=null&&entries!=null)for(int i=0;i<entries.length();i++){
            JSONObject entry=entries.optJSONObject(i);if(entry==null)continue;String id=entry.optString("trackId");if(tracks.has(id))continue;
            JSONObject source=originals.optJSONObject(id);if(source==null)continue;JSONObject small=new JSONObject();
            // DjPlan.track copies source metadata for each segment. Forecasting
            // needs only source bounds, never artwork or loudness analysis.
            for(String key:new String[]{"duration","rangeStart","rangeEnd"})if(source.has(key))small.put(key,source.opt(key));
            tracks.put(id,small);
        }
        q.put("tracks",tracks);
    }
    synchronized JSONObject plan(State state,JSONObject lightQueue){
        if(lightQueue==null||!RemixPlan.enabled(lightQueue)){cachedTag=null;cachedResult=null;return null;}
        try{
            Tag tag=new Tag(lightQueue);if(tag.entries==0||tag.entries>MAX_ENTRIES)return null;
            if(tag.same(cachedTag))return cachedResult==null?null:new JSONObject(cachedResult);
            // State.read returns an immutable string, including for chunked SQLite
            // rows. All scheduler mutation stays within this private parsed copy.
            String saved=state.read("nativeQueue","{}");if(saved.length()>MAX_JSON)return null;
            JSONObject copy=new JSONObject(saved);
            if(!RemixPlan.enabled(copy)||!tag.same(new Tag(copy)))return null;
            restrictSources(copy);
            JSONArray segments=new JSONArray();
            for(int i=0;i<LIMIT;i++){
                String current=copy.optString("currentId");int at=index(copy,current);if(at<0)break;
                JSONArray entries=copy.optJSONArray("entries");JSONObject next=entries.optJSONObject(at+1);
                // Preserve the successor already selected/preloaded by playback.
                RemixPlan.finish(copy,current,next==null?"":next.optString("id"),false);
                int now=index(copy,copy.optString("currentId"));if(now<0)break;
                JSONObject entry=copy.getJSONArray("entries").optJSONObject(now),tracks=copy.optJSONObject("tracks");
                JSONObject source=tracks==null?null:tracks.optJSONObject(entry.optString("trackId"));
                if(source==null||!(source.opt("duration") instanceof Number))break;
                JSONObject effective=DjPlan.track(source,entry,true);
                double start=DjPlan.lower(effective),end=DjPlan.upper(effective);
                if(!Double.isFinite(start)||!Double.isFinite(end)||end<=start)break;
                segments.put(new JSONObject().put("position",i+1).put("entryId",entry.optString("id")).put("trackId",entry.optString("trackId")).put("start",start).put("end",end));
            }
            JSONObject result=new JSONObject().put("currentId",tag.currentId).put("uiRevision",tag.revision).put("segments",segments);
            cachedResult=result.toString();cachedTag=tag;return new JSONObject(cachedResult);
        }catch(Exception ignored){return null;}
    }
}
