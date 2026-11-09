"""Numeric vocal evidence is a conservative fade guard, not a word detector."""
import copy
import json
import math
from pathlib import Path
import pytest
from aidj import fixed_set_vocal_review as review
from aidj.vocal_probe import MODEL_SHA256


def vectors(offset=66.5,duration=5.4,voice=.0005,mix=.2):
    n=math.ceil(duration/.01-1e-8)
    v=[voice]*n if isinstance(voice,(int,float)) else list(voice)
    m=[mix]*n if isinstance(mix,(int,float)) else list(mix)
    return {'modelSha256':MODEL_SHA256,'sourceOffsetSeconds':offset,'durationSeconds':duration,
            'frameHopSeconds':.01,'vocalRms':v,'mixRms':m,'instrumentalRms':[max(0,a-b) for a,b in zip(m,v)],
            'vocalToMixDb':[20*math.log10(max(1e-8,b)/max(1e-8,a)) for a,b in zip(m,v)],'warnings':[],
            'cacheKey':str(offset)}


def point(end=70,fade=1.2):
    return {'time':end,'fade':fade,'fadeStart':end-fade}


def test_quiet_reverb_assigned_to_vocals_passes_absolute_guard_even_at_zero_db_ratio():
    p=point();d=vectors(voice=.006,mix=.006)
    result=review.check_endpoint(d,p,[],-6.648,.1)
    assert result['accepted'] and result['criterion']=='absolute-quiet'
    assert result['vocalToMixP95Db']==0 and result['normalizedVocalP95Dbfs'] < -43
    assert result['proofStart']==68.6 and result['proofEnd']==70.2
    assert result['verifiedWordBoundary'] is False


def test_suppressed_voice_can_pass_but_one_loud_frame_still_vetoes_p95():
    d=vectors(voice=.015,mix=.8)
    result=review.check_endpoint(d,point(),[],-3.098,.1)
    assert result['accepted'] and result['criterion']=='suppressed-vocal'
    assert result['normalizedVocalP95Dbfs'] > -43 and result['vocalToMixP95Db'] < -30
    d=vectors(voice=.0005);d['vocalRms'][250]=.2
    assert not review.check_endpoint(d,point(),[],-3.098,.1)['accepted']


def test_known_word_cannot_be_overruled_by_internal_digital_silence_or_empty_asr():
    d=vectors(voice=.00001,mix=.00001)
    assert not review.check_endpoint(d,point(),[[69,69.1,True]],-3.098,.1)['accepted']
    assert review.check_endpoint(d,point(),[],-3.098,.1)['accepted']
    d['warnings']=['near-zero-vocal-output']
    assert review.check_endpoint(d,point(),[],-3.098,.1)['reason']=='invalid-vocal-evidence'


def test_whole_fade_plus_margins_needs_real_context_and_finite_pinned_vectors():
    d=vectors(offset=69,duration=2)
    assert review.check_endpoint(d,point(),[],-3.098,.1)['reason']=='incomplete-fade-context'
    for mutate in (lambda d:d.update(modelSha256='wrong'),lambda d:d['vocalRms'].__setitem__(0,float('nan')),
                   lambda d:d.update(frameHopSeconds=.5),lambda d:d['mixRms'].pop(),lambda d:d['vocalToMixDb'].__setitem__(0,-100)):
        bad=vectors();mutate(bad)
        assert not review.check_endpoint(bad,point(),[],-3.098,.1)['accepted']


def fixtures():
    old={'trackId':'a'*24,'md5':'1'*32,'start':0.,'end':70.,'fadeIn':.1,'fadeOut':1.2}
    song={'lo':0.,'hi':180.,'duration':180.,'artist':'Example','title':'Song','lufs':-16.,'peak':-2.}
    checkpoint={'status':'ready','row':copy.deepcopy(old),'referenceRms':.2,'entry':{'time':0.,'kind':'source-start','fade':.08},
                'exit':{**point(),'method':'reobserved-instrumental-fade'},'alternatives':[]}
    return old,song,checkpoint,{'words':[],'coverage':[[0,180]],'repairProbes':[]}


def test_loud_music_gets_wider_generated_fade_only_when_both_stem_and_words_allow():
    old,song,checkpoint,evidence=fixtures();calls=[]
    def probe(a,b):calls.append((a,b));return vectors(a,b-a)
    result=review.review_checkpoint(old,song,checkpoint,evidence,probe,search=False)
    assert result['status']=='ready' and result['exit']['method']=='vocal-checked-instrumental-fade'
    assert result['row']['end']==70 and result['row']['fadeOut']==2.4
    assert calls==[(66.5,71.9)] and result['row']['trackId']==old['trackId']
    assert result['exit']['rampFacts']['kind']=='generated-release'
    assert result['exit']['rampFacts']['naturalSilenceClaimed'] is False
    assert result['exit']['vocalCheck']['accepted']
    # Only the original shorter window is quiet; extending it in either
    # direction would touch a vocal. Keep the short, actually checked release.
    def short_probe(a,b):
        v=[.0005 if 68.5 <= a+i*.01 < 70.3 else .2 for i in range(540)]
        return vectors(a,b-a,voice=v)
    result=review.review_checkpoint(old,song,checkpoint,evidence,short_probe,search=False)
    assert result['status']=='ready' and result['row']['fadeOut']==1.2


def test_vocal_veto_chooses_a_safe_alternative_without_changing_track_order_or_source():
    old,song,checkpoint,evidence=fixtures()
    checkpoint['alternatives']=[{'entry':checkpoint['entry'],'exit':point(90),'score':1}]
    def probe(a,b):return vectors(a,b-a,voice=.2 if a<80 else .0005)
    result=review.review_checkpoint(old,song,checkpoint,evidence,probe,search=False)
    assert result['status']=='ready' and result['row']['end']==90
    assert result['row']['trackId']==old['trackId'] and result['row']['md5']==old['md5']
    assert any(not c['accepted'] for c in result['vocalReviewAttempts'])
    assert checkpoint['row']['end']==70  # Input checkpoint is never mutated.


def test_instrumental_needs_probe_does_not_require_three_recognised_words():
    old,song,checkpoint,evidence=fixtures();checkpoint['status']='needs-probe';evidence={'words':[],'coverage':[]}
    result=review.review_checkpoint(old,song,checkpoint,evidence,lambda a,b:vectors(a,b-a),search=False)
    assert result['status']=='ready' and result['exit']['method']=='vocal-isolated-fade'
    assert result['repairProbes']==[] and result['exit']['vocalCheck']['asrClear']


def test_generic_context_search_uses_a_sustained_window_and_safe_entry():
    old,song,checkpoint,evidence=fixtures();checkpoint={'status':'needs-review'};calls=[]
    def probe(a,b):calls.append((a,b));return vectors(a,b-a)
    result=review.review_checkpoint(old,song,checkpoint,evidence,probe)
    assert calls==[(60.,80.)] and result['status']=='ready'
    assert 45 <= result['row']['end']-result['row']['start'] <= 100
    assert result['exit']['method']=='vocal-isolated-fade'
    assert result['row']['end'] <= 80 and result['row']['trackId']==old['trackId']
    # A loud vocal throughout the context cannot be labelled resolved.
    result=review.review_checkpoint(old,song,checkpoint,evidence,lambda a,b:vectors(a,b-a,voice=.2))
    assert result['status']=='needs-review'


def test_private_awoo_calibration_when_available():
    root=Path('/home/ilya-grid/.local/share/aidj-codex-music/research/dj1-quiet-v2/vocal-pilot')
    if not (root/'good.json').exists():pytest.skip('Private calibration vectors are not bundled')
    gain=review.gain_db({'lufs':-12.45,'peak':-1.74})
    good=json.loads((root/'good.json').read_text());bad=json.loads((root/'bad.json').read_text())
    assert review.check_endpoint(good,point(192.5,.65),[],gain,.2)['accepted']
    assert not review.check_endpoint(bad,point(166.674,.65),[],gain,.2)['accepted']


def test_cached_numeric_probe_reuses_exact_source_window_without_decoder_or_model(tmp_path,monkeypatch):
    import hashlib
    a,b=66.5,71.9;song={'md5':'1'*32,'duration':180.}
    key=hashlib.sha256(json.dumps([review.VERSION,song['md5'],a,b,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
    cached={**vectors(a,b-a),'cacheKey':key,'md5':song['md5'],'requestedEndSeconds':b}
    (tmp_path/(key+'.json')).write_text(json.dumps(cached))
    def forbidden(*a,**k):raise AssertionError('numeric cache must avoid audio work')
    monkeypatch.setattr(review.subprocess,'run',forbidden);monkeypatch.setattr(review,'probe_vocals',forbidden)
    probe=review.CachedProbe(Path('unused'),song,tmp_path,'unused','unused')
    assert probe(a,b)==cached


def test_bad_decoded_excerpt_is_removed_before_loading_model(tmp_path,monkeypatch):
    from types import SimpleNamespace
    def decode(cmd,**kwargs):Path(cmd[-1]).write_bytes(b'bad');return SimpleNamespace(returncode=0)
    def forbidden(*a,**k):raise AssertionError('invalid PCM must not reach model')
    monkeypatch.setattr(review.subprocess,'run',decode);monkeypatch.setattr(review,'probe_vocals',forbidden)
    probe=review.CachedProbe(Path('unused'),{'md5':'1'*32,'duration':180.},tmp_path,'unused','unused')
    with pytest.raises(RuntimeError,match='VOCAL_REVIEW_DECODE'):probe(66.5,71.9)
    assert list(tmp_path.iterdir())==[]


def test_probe_budget_caps_alternatives_and_context_with_no_unbounded_inference():
    old,song,checkpoint,evidence=fixtures();calls=[]
    checkpoint['alternatives']=[{'entry':checkpoint['entry'],'exit':point(71+i),'score':i} for i in range(40)]
    def probe(a,b):calls.append((a,b));return vectors(a,b-a,voice=.2)
    result=review.review_checkpoint(old,song,checkpoint,evidence,probe)
    assert result['status']=='needs-review'
    assert len(calls)<=review.MAX_ENDPOINT_PROBES+review.MAX_CONTEXT_PROBES==6
    assert result['vocalReviewBudget']['distinctWindows']==len(calls)
    assert result['vocalReviewBudget']['maxRequestedAudioSeconds']==47
    with pytest.raises(RuntimeError,match='VOCAL_REVIEW_TRACK_TIMEOUT'):
        review.review_checkpoint(old,song,checkpoint,evidence,probe,max_seconds=-1)
