from aidj.fixed_set_overlap import quiet_vocal_window, fallback_overlap
from aidj.vocal_probe import MODEL_SHA256


def example():
    return {'md5':'a'*32,'modelSha256':MODEL_SHA256,'warnings':[],
            'sourceOffsetSeconds':10.,'durationSeconds':10.,'frameHopSeconds':.01,
            'mixRms':[.1]*1000,'vocalRms':[.001]*1000,
            'instrumentalRms':[.099]*1000,'vocalToMixDb':[-40.]*1000}


def test_long_overlap_requires_actual_full_model_coverage_and_quiet_voice():
    e=example()
    def check(md5='a'*32,start=11.,end=16.,gain=0,**kwargs):
        return quiet_vocal_window(e,md5,start,end,gain,words=kwargs.get('words',[]),coverage=kwargs.get('coverage',[[10.,20.]]))
    assert check(end=16.6)['frames']==560
    assert check(start=10.,end=15.) is None
    assert check(start=15.,end=20.) is None
    assert check(md5='changed') is None
    assert check(words=[[12.,12.1,False]]) is None
    assert check(coverage=[[10.,12.],[12.1,20.]]) is None
    assert quiet_vocal_window(e,'a'*32,11.,16.,0) is None
    e['vocalRms'][200]=.1;e['vocalToMixDb'][200]=0.
    assert check() is None
    # Attenuation is applied from current loudness, not saved as a permanent pass.
    assert check(gain=-30)['normalizedVocalMaxDbfs']==-50


def test_short_fallback_does_not_use_unobserved_gap_or_extend_over_final_word():
    c={'row':{'end':100.},'exit':{'regionStart':99.2}}
    assert fallback_overlap(c)==.7
    assert fallback_overlap(c,rate=1.03)*1.03 <= .7001
    assert fallback_overlap({'row':{'end':100.},'exit':{}})==0
    assert fallback_overlap({'row':{'end':100.},'exit':{'regionStart':100.1}})==0
    assert fallback_overlap({'row':{'end':100.},'exit':{'regionStart':90.}})==3
