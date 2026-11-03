package com.aidj.music;
import org.json.JSONObject;

final class PlaybackRange {
    static long start(JSONObject track,long duration){return track==null?0:Math.max(0,Math.min((long)(track.optDouble("rangeStart",0)*1000),Math.max(0,duration-500)));}
    static long end(JSONObject track,long duration){return track==null?duration:Math.max(start(track,duration)+500,Math.min((long)(track.optDouble("rangeEnd",duration/1000.0)*1000),duration));}
    static long clamp(JSONObject track,long duration,long value){return Math.max(start(track,duration),Math.min(value,Math.max(start(track,duration),end(track,duration)-25)));}
}
