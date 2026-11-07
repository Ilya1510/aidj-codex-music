"""Reusable word boundaries, bounded rescue, and explicitly provisional DSP."""
import math

import pytest

from aidj.dj import validate,compatible_v3
from aidj.dj_worker import anchor_graph,acoustic_graph,selection,usable_anchors,needs_rescue,rescue_windows,replace_observed_words,vocal_gaps


def base(duration=180):
    return {'version':3,'duration':duration,'words':[(s,s+1,True) for s in range(0,int(duration),3)],
            'coverage':[[0,duration]],'probes':[],'bpm':120,'beats':[i*.5 for i in range(int(duration*2))],
            'energy':[.7]*int(duration),'analysisScope':'full'}


def test_runtime_anchors_recover_a_useful_duration_between_old_fixed_pools():
    e=base(80);e['words']=[(s,s+.4,True) for s in range(9)]+[(32,32.5,True),(35.5,36,True)]
    value=selection(e)
    assert not value['cues'] and not value['remix']
    assert usable_anchors(value['anchors'])>0 and value['reason'] is None
    assert any(abs(row[0]-35.3)<.001 for row in value['anchors']['exits'])
    assert value['needsReview'] is False


def test_anchors_are_sorted_small_and_every_nonzero_point_is_a_guarded_observed_gap():
    e=base(600);e['words']=[(s,s+.2,True) for s in range(600)]
    graph=anchor_graph(e)
    assert graph['entries'][0][0]==0 and len(graph['entries'])<=256 and len(graph['exits'])<=256
    assert graph['entries']==sorted(graph['entries']) and graph['exits']==sorted(graph['exits'])
    for t,_ in graph['entries'][1:]:
        assert all(not (lo-.1999<t<hi+.1999) for lo,hi,_ in e['words'])
    for t,fade,_ in graph['exits']:
        assert .15<=fade<=2
        assert all(not (lo-.1999<t and hi+.1999>t-fade) for lo,hi,_ in e['words'])
    assert graph['entries'][-1][0]>590 and graph['exits'][-1][0]>590


def test_anchor_graph_is_validated_and_omitted_from_old_apks():
    value=selection(base());clean=validate(value,180)
    assert clean['anchors']==value['anchors'] and clean['anchors']['wordGuarded'] is True
    assert clean['acousticAnchors']['quality']=='acoustic' and clean['acousticAnchors']['needsReview'] is True
    old=compatible_v3(clean)
    assert not any(key in old for key in ('anchors','acousticAnchors','needsReview','reviewRecommended','fullFallback'))
    for wrong in [float('nan'),-1,181,True]:
        bad={**value,'anchors':{**value['anchors'],'entries':[[wrong,.5]]}}
        with pytest.raises(ValueError):validate(bad,180)
    with pytest.raises(ValueError):validate({**value,'anchors':{**value['anchors'],'entries':[[30,.5],[20,.5]]}},180)
    with pytest.raises(ValueError):validate({**value,'anchors':{**value['anchors'],'entries':[[i/10,.5] for i in range(257)]}},180)


def test_new_observation_replaces_an_uncertain_blanket_but_keeps_true_edge_words():
    # Old low-confidence singing alignment covered a whole 40-second span. Its
    # presence must not permanently mask independently observed local words.
    previous=[(0,40,False),(8,11,True),(29,32,True)]
    fresh=[(s,s+.5,True) for s in range(10,30,2)]
    words=replace_observed_words(previous,fresh,9,31)
    assert (8,11,True) in words and (29,32,True) in words
    assert (0,40,False) not in words and [0,10,False] in words and [30,40,False] in words
    e={**base(40),'words':words,'coverage':[[9,31]]}
    assert vocal_gaps(e,.5)
    assert all(10<lo<hi<30 for lo,hi in vocal_gaps(e,.5))


def test_rescue_is_shifted_bounded_and_never_repeated_after_completed_quality_pass():
    e=base();e['words']=[(s,s+.95,True) for s in range(180)]
    e['probes']=[[s,min(180,s+28)] for s in range(0,160,24)]
    assert needs_rescue(e)
    windows=rescue_windows(e)
    assert 0<len(windows)<=6 and all(hi-lo==20 for lo,hi in windows)
    assert all(not any(abs(lo-a)<.25 and abs(hi-b)<.25 for a,b in e['probes']) for lo,hi in windows)
    assert sum(hi-lo for lo,hi in windows)<=120
    assert not rescue_windows({**e,'boundaryPass':1}) and not needs_rescue({**e,'boundaryPass':1})
    assert not needs_rescue(base())


def test_acoustic_points_are_review_only_and_do_not_cross_confident_words():
    e=base();e['words']=[(12,25,True),(45,58,True),(60,80,False)]
    e['acousticFeatures']={'hop':.02,'regions':[[10,11,.1],[29,31,.1],[49,51,.1],[69,71,.1],[100,102,.1]]}
    graph=acoustic_graph(e)
    assert graph['wordGuarded'] is False and graph['quality']=='acoustic' and graph['needsReview'] is True
    assert any(60<t<80 for t,_ in graph['entries'])  # Unknown speech is provisional, never marked verified.
    for t,_ in graph['entries'][1:]:assert not (11.8<t<25.2 or 44.8<t<58.2)
    for t,fade,_ in graph['exits']:assert all(not(lo-.2<t and hi+.2>t-fade) for lo,hi,good in e['words'] if good)
    value=validate({'cueSchema':4,'cues':[],'remix':[],'acousticAnchors':graph,'needsReview':True},180)
    assert value['needsReview'] and value['acousticAnchors']['wordGuarded'] is False
    with pytest.raises(ValueError):validate({'cueSchema':4,'anchors':graph},180)


def test_continuous_known_words_do_not_get_fabricated_verified_internal_cuts():
    e=base(180);e['words']=[(s,s+1,True) for s in range(180)]
    e['acousticFeatures']={'hop':.02,'regions':[[t,t+.8,.05] for t in range(1,170,5)]}
    value=selection(e)
    assert not value['anchors']['exits'] and not value['acousticAnchors']['exits']
    assert value['needsReview'] and value['fullFallback'] and value['reviewRecommended']
    assert validate(value,180)['fullFallback']


def test_measured_rms_features_detect_a_real_valley_without_convolution_edge_silence():
    np=pytest.importorskip('numpy')
    from aidj.dj_worker import acoustic_features
    sr=16000;y=np.ones(sr*6,dtype=np.float32)
    assert acoustic_features(y,sr)['regions']==[]
    y[2*sr:3*sr]=.02
    features=acoustic_features(y,sr)
    assert features['hop']==.02 and len(features['regions'])<=512
    assert any(lo<2.2 and hi>2.8 and energy<.1 for lo,hi,energy in features['regions'])
    assert all(0<=lo<hi<=6 and math.isfinite(energy) for lo,hi,energy in features['regions'])


def test_full_quality_rescue_has_a_saved_plan_and_resumes_without_repeating_work(tmp_path,monkeypatch):
    import json
    import sys
    from types import SimpleNamespace as N
    from aidj.dj_worker import analyse
    duration=60;source=tmp_path/'source.mp3';source.write_bytes(b'original');checkpoint=tmp_path/'partial.json'
    class Samples:
        def __init__(self,path,**kwargs):self.length=path.stat().st_size//4;self._mmap=self
        def __len__(self):return self.length
        def __getitem__(self,slice):return (slice.start,slice.stop)
        def close(self):pass
    calls=[];failed=[False]
    class Model:
        def transcribe(self,sample,**kwargs):
            if len(calls)==4 and not failed[0]:failed[0]=True;raise InterruptedError('stop inside rescue')
            calls.append(sample)
            confident=len(calls)>3
            words=[N(start=i*2,end=i*2+1,probability=.9) for i in range(9)] if confident else []
            part=N(start=0,end=20,no_speech_prob=.1 if confident else .99,avg_logprob=-.2 if confident else -2,words=words)
            return iter([part]),None
    def command(args,**kwargs):
        assert args[0]=='ffmpeg'
        with open(args[-1],'wb') as stream:stream.truncate(duration*16000*4)
        return N(returncode=0)
    monkeypatch.setitem(sys.modules,'numpy',N(float32=object(),memmap=Samples,array=lambda value,**kwargs:value))
    monkeypatch.setitem(sys.modules,'faster_whisper',N(WhisperModel=lambda *args,**kwargs:Model()))
    monkeypatch.setattr('aidj.dj_worker.subprocess.run',command)
    seed={**base(duration),'words':[],'coverage':[],'acousticFeatures':{'hop':.02,'regions':[]}}
    with pytest.raises(InterruptedError):analyse(source,'ffmpeg',tmp_path/'models',cached=seed,checkpoint=checkpoint,full=True)
    saved=json.loads(checkpoint.read_text());done=list(calls)
    assert 0<len(saved['rescuePlan'])<=6 and not saved.get('boundaryPass') and saved['fullComplete'] is False
    result=analyse(source,'ffmpeg',tmp_path/'models',cached=saved,checkpoint=checkpoint,full=True)
    assert all(sample not in done for sample in calls[len(done):])
    assert result['evidence']['fullComplete'] and result['evidence']['boundaryPass']==1
    assert result['reviewRecommended'] and usable_anchors(result['anchors'])
    before=len(calls)
    finished=analyse(source,'ffmpeg',tmp_path/'models',cached=result['evidence'],full=True)
    assert finished['evidence']['boundaryPass']==1 and len(calls)==before
