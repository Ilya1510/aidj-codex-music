package com.aidj.music;

public final class PlaybackFadeProbe {
    private static void expect(float actual,float expected){if(Math.abs(actual-expected)>.0001f)throw new AssertionError("wrong fade gain: "+actual+" expected "+expected);}
    public static void main(String[] args){
        expect(PlaybackFade.gain(30000,30000,60000,0),1);
        expect(PlaybackFade.gain(30000,30000,60000,3000),0);
        expect(PlaybackFade.gain(31000,30000,60000,3000),1f/3);
        expect(PlaybackFade.gain(45000,30000,60000,3000),1);
        expect(PlaybackFade.gain(59000,30000,60000,3000),1f/3);
        expect(PlaybackFade.gain(60000,30000,60000,3000),0);
        expect(PlaybackFade.gain(10500,10000,11000,8000),1);
        expect(PlaybackFade.gain(10750,10000,11000,8000),.5f);
        System.out.println("NATIVE_FADE_PASSED default fade_in fade_out fragment seek short_track");
    }
}
