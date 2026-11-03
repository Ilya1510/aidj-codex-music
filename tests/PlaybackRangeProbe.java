package com.aidj.music;
import org.json.JSONObject;
public final class PlaybackRangeProbe {
    public static void main(String[] args)throws Exception{
        JSONObject track=new JSONObject();track.put("rangeStart",30);track.put("rangeEnd",90);
        if(PlaybackRange.start(track,200000)!=30000||PlaybackRange.end(track,200000)!=90000)throw new AssertionError("wrong boundaries");
        if(PlaybackRange.clamp(track,200000,0)!=30000)throw new AssertionError("start and repeat must seek to the segment");
        if(PlaybackRange.clamp(track,200000,120000)>=90000)throw new AssertionError("lockscreen seek must stay inside the segment");
        if(PlaybackRange.clamp(track,200000,45000)!=45000)throw new AssertionError("valid seek changed");
        if(PlaybackRange.end(new JSONObject(),200000)!=200000)throw new AssertionError("full track default");
        System.out.println("NATIVE_RANGE_PASSED start repeat lockscreen_seek clamp full_track");
    }
}
