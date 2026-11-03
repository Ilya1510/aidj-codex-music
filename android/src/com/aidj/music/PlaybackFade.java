package com.aidj.music;

/** A position-based envelope also works after pause, seek and range edits. */
final class PlaybackFade {
    static float gain(long position,long start,long end,long requested){
        if(requested<=0||end<=start)return 1f;
        double window=Math.min(Math.min(requested,8000),(end-start)/2.0);
        return (float)Math.max(0,Math.min(1,Math.min((position-start)/window,(end-position)/window)));
    }
}
