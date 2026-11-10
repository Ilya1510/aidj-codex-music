"""Incoming audio can sing soon after a verified breath, never across the cut."""
import copy
import hashlib
import json
import math
from pathlib import Path

import pytest

from aidj import fixed_set_entry_review as review
from aidj.vocal_probe import MODEL_SHA256

MD5='1'*32
COVERAGE=[[0.,180.]]


def vectors(offset=37.3,duration=5.4,voice=.0005,mix=.3):
    n=math.ceil(duration/.01-1e-8)
    voices=[voice]*n if isinstance(voice,(int,float)) else voice
    return {'modelSha256':MODEL_SHA256,'md5':MD5,'sourceOffsetSeconds':offset,
            'durationSeconds':duration,'requestedEndSeconds':offset+duration,'frameHopSeconds':.01,
            'vocalRms':voices,'mixRms':[mix]*n,'instrumentalRms':[mix]*n,
            'vocalToMixDb':[20*math.log10(max(v,1e-8)/max(mix,1e-8)) for v in voices],
            'cacheKey':'a'*64,'warnings':[]}


def fixtures(start=40.):
    row={'trackId':'a'*24,'md5':MD5,'start':start,'end':100.,'fadeIn':.1,'fadeOut':.8,
         'transitionKind':'clean-cut','transitionSeconds':0,'rate':1.}
    item={'trackId':row['trackId'],'md5':MD5,'row':row,'status':'ready','position':1,
          'entry':{'time':start,'kind':'observed-word-gap'},'exit':{'validated':True},'observedWords':[]}
    song={'md5':MD5,'lo':0.,'hi':180.,'duration':180.,'manualStart':False,
          'lufs':-16.,'peak':-2.}
    return item,song


def test_singing_after_the_breath_is_expected_but_one_frame_at_cut_is_a_veto():
    v=vectors(voice=[.0005 if 37.3+i*.01<40.1 else .3 for i in range(540)])
    good=review.check_entry(v,40,[[40.35,41,True]],-3.098,MD5,coverage=COVERAGE)
    assert good['accepted'] and good['frameCount']==20
    assert good['verifiedWordBoundary'] is False and good['naturalSilenceClaimed'] is False
    # Touching all10ms frames, not an average or p95 alone, prevents a short
    # vocal onset hiding inside nineteen quiet frames.
    v['vocalRms'][270]=.3;v['vocalToMixDb'][270]=0
    assert not review.check_entry(v,40,[],-3.098,MD5,coverage=COVERAGE)['accepted']


def test_retained_word_vetoes_even_an_acoustically_quiet_dip_inside_word():
    v=vectors()
    assert not review.check_entry(v,40,[[39.8,40.2,True]],-3.098,MD5,coverage=COVERAGE)['accepted']
    assert review.check_entry(v,40,[],-3.098,MD5,coverage=COVERAGE)['accepted']


def test_invalid_source_model_and_excerpt_edges_cannot_certify_entry():
    v=vectors()
    assert review.check_entry(v,37.4,[],-3.098,MD5,coverage=COVERAGE)['reason']=='incomplete-entry-context'
    for change in (lambda v:v.update(md5='2'*32),lambda v:v.update(modelSha256='wrong'),
                   lambda v:v.update(warnings=['near-zero-vocal-output']),
                   lambda v:v['vocalRms'].__setitem__(0,float('nan'))):
        bad=copy.deepcopy(v);change(bad)
        assert not review.check_entry(bad,40,[],-3.098,MD5,coverage=COVERAGE)['accepted']


def test_only_physical_zero_explicit_manual_start_or_exact_owner_are_exempt():
    item,song=fixtures(0.)
    def forbidden(*a):raise AssertionError('exemption needs no model or download')
    result=review.review_checkpoint(item,song,[],forbidden)
    assert result['entryProof']['method']=='physical-source-start'
    assert result['entryProof']['acousticProofClaimed'] is False
    assert review.exemption(3,{**song,'lo':3},[]) is None
    assert review.exemption(3,{**song,'lo':3,'manualStart':True},[])=='manual-range-start'
    assert review.exemption(40,song,[40])=='owner-approved'
    assert review.exemption(40.01,song,[40]) is None


def test_kept_source_start_name_cannot_exempt_a_nonzero_actual_entry():
    item,song=fixtures();item['entry']['kind']='kept-source-start';calls=[]
    def probe(a,b):calls.append((a,b));return vectors(a,b-a)
    result=review.review_checkpoint(item,song,[],probe,coverage=COVERAGE)
    assert calls==[(37.3,42.699999999999996)]
    assert result['entryProof']['method']=='vocal-free-entry-breath'


def test_reuse_and_nearest_safe_shift_preserve_exit_but_invalidate_stereo_row():
    item,song=fixtures();item['stereoEvidence']={'rowStart':40};item['stereoVersion']=1
    item['exit']['stereoVerified']=True
    v=vectors(voice=[.0005 if 37.3+i*.01<39.7 else .3 for i in range(540)])
    def forbidden(*a):raise AssertionError('cached vectors already cover a safe nearby entry')
    result=review.review_checkpoint(item,song,[],forbidden,coverage=COVERAGE,existing_evidence=[v])
    assert result['status']=='ready' and 39.3<result['row']['start']<39.7
    assert result['row']['end']==100 and result['row']['fadeOut']==.8
    assert result['row']['trackId']==item['trackId'] and result['md5']==MD5
    assert 'stereoEvidence' not in result and 'stereoVersion' not in result
    assert 'stereoVerified' not in result['exit'] and result['entryShiftNeedsStereoRecheck']
    assert result['entryReviewBudget']['newProbes']==0
    assert item['row']['start']==40 and item['stereoEvidence']=={'rowStart':40}


def test_search_never_escapes_allowed_range_duration_or_owner_negative_veto():
    item,song=fixtures(55);item['row']['end']=100
    calls=[]
    def loud(a,b):calls.append((a,b));return vectors(a,b-a,voice=.3)
    result=review.review_checkpoint(item,song,[],loud,coverage=COVERAGE,rejected=[55])
    assert result['status']=='needs-review' and result['row']['start']==55
    assert result['entryReviewOnly'] and result['entryReviewBudget']['newProbes']==1 and len(calls)==1
    v=vectors(52.3,voice=[.0005 if 52.3+i*.01>55.5 else .3 for i in range(540)])
    # A later safe breath would leave less than45s, so it must stay unresolved.
    result=review.review_checkpoint(item,song,[],lambda a,b:v,coverage=COVERAGE,existing_evidence=[v])
    assert result['status']=='needs-review'


def test_candidate_search_validates_each_raw_excerpt_once_but_final_gate_revalidates(tmp_path,monkeypatch):
    item,song=fixtures();v=vectors(voice=.3);calls=[];original=review._vectors
    def counted(raw):calls.append(raw);return original(raw)
    monkeypatch.setattr(review,'_vectors',counted)
    result=review.review_checkpoint(item,song,[],lambda a,b:v,coverage=COVERAGE,existing_evidence=[v])
    assert result['entryReviewStatus']=='needs-review'
    assert len(calls)==3  # Initial cache validation, then cached/new searches.
    good=vectors();result=review.review_checkpoint(item,song,[],lambda a,b:good,coverage=COVERAGE)
    (tmp_path/('a'*64+'.json')).write_text(json.dumps(good));before=len(calls)
    assert review.selection_error(result,song,evidence_folder=tmp_path) is None
    assert len(calls)==before+1


def test_final_gate_reloads_raw_vectors_and_rejects_stale_or_forged_verdict(tmp_path):
    item,song=fixtures();v=vectors()
    result=review.review_checkpoint(item,song,[],lambda a,b:v,coverage=COVERAGE)
    path=tmp_path/('a'*64+'.json');path.write_text(json.dumps(v))
    assert review.selection_error(result,song,evidence_folder=tmp_path) is None
    changed=copy.deepcopy(result);changed['row']['start']+=.01
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-proof-stale'
    changed=copy.deepcopy(result);changed['observedWords']=[[39.8,40.2,True]]
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-input-changed'
    changed=copy.deepcopy(result);changed['entryProof']['method']='imaginary-boundary'
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-proof-method'
    changed=copy.deepcopy(result);changed['entryProof']['normalizedVocalMaxDbfs']=-100
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-proof-mismatch'
    v['vocalRms'][270]=.3;v['vocalToMixDb'][270]=0;path.write_text(json.dumps(v))
    assert review.selection_error(result,song,evidence_folder=tmp_path).startswith('entry-proof-')


def test_owner_exemption_is_rechecked_at_final_gate(tmp_path):
    item,song=fixtures()
    result=review.review_checkpoint(item,song,[],lambda *a:None,owner=[40])
    assert review.selection_error(result,song,owner=[40],evidence_folder=tmp_path) is None
    assert review.selection_error(result,song,owner=[],evidence_folder=tmp_path)=='entry-input-changed'


def test_failed_entry_review_can_retry_without_changing_an_already_valid_exit():
    item,song=fixtures()
    bad=review.review_checkpoint(item,song,[],lambda a,b:vectors(a,b-a,voice=.3),coverage=COVERAGE)
    assert bad['status']=='needs-review' and bad['entryReviewOnly']
    assert bad['entryVocalEvidence'][0]['cacheKey']=='a'*64
    good=review.review_checkpoint(bad,song,[],lambda a,b:vectors(a,b-a),coverage=COVERAGE)
    assert good['status']=='ready' and not good['entryReviewOnly']
    assert good['exit']==item['exit'] and good['row']['end']==item['row']['end']


def test_missing_or_partial_asr_coverage_cannot_turn_absent_words_into_a_safe_entry():
    v=vectors()
    result=review.check_entry(v,40,[],-3.098,MD5)
    assert not result['accepted'] and result['reason']=='entry-asr-unobserved'
    assert result['asrClear'] and result['asrCoverageComplete'] is False
    assert not review.check_entry(v,40,[],-3.098,MD5,coverage=[[0,40.2]])['accepted']
    # Consecutive actually observed windows may cover the whole guard.
    assert review.check_entry(v,40,[],-3.098,MD5,coverage=[[39.7,40],[40,40.3]])['accepted']
    assert not review.check_entry(v,40,[],-3.098,MD5,coverage=[[39.7,39.9],[40,40.3]])['accepted']


def test_final_gate_rejects_removed_coverage_and_replays_the_observation_bounds(tmp_path):
    item,song=fixtures();v=vectors()
    result=review.review_checkpoint(item,song,[],lambda a,b:v,coverage=COVERAGE)
    (tmp_path/('a'*64+'.json')).write_text(json.dumps(v))
    assert review.selection_error(result,song,evidence_folder=tmp_path) is None
    changed=copy.deepcopy(result);changed['observedCoverage']=[]
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-input-changed'
    # Even a recomputed fingerprint cannot launder an unobserved ASR window.
    changed['entryReviewInputHash']=review.fingerprint(changed,song,[])
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-proof-entry-asr-unobserved'


def onset_vectors(at=40.12,pre=.0005,post=.04):
    return vectors(voice=[pre if 37.3+i*.01<at-1e-7 else post for i in range(540)])


def test_asymmetric_entry_allows_new_word_after_measured_breath_without_symmetric_future_veto():
    v=onset_vectors();words=[[40.16,40.7,True]]
    assert not review.check_entry(v,40,words,-3.098,MD5,coverage=COVERAGE)['accepted']
    check=review.check_entry_onset(v,40,words,-3.098,MD5,coverage=COVERAGE,fade_in=.1)
    assert check['accepted'] and check['method']=='vocal-breath-before-new-word'
    assert check['risingVoiceDelaySeconds']==pytest.approx(.12)
    assert check['nextWordDelaySeconds']==pytest.approx(.16)
    assert check['fadeInSeconds']==.08 and check['fadeInCapSeconds']==.08
    assert check['vocalRiseDb']>30 and check['pre']['vocalMaxDbfs']<-43
    assert not check['verifiedWordBoundary'] and not check['lowSpeechProbabilityUsed']


@pytest.mark.parametrize('extra,reason',[
    ([[39.9,40.08,True]],'entry-word-crosses-point'),
    ([[39.99,40.01,False]],'entry-word-crosses-point'),
    ([[39.8,39.95,True]],'entry-prior-word-too-close'),
    ([[40.1,40.13,False]],'entry-new-word-not-confident')])
def test_onset_tier_never_overrides_crossing_unknown_or_recent_prior_words(extra,reason):
    check=review.check_entry_onset(onset_vectors(),40,extra+[[40.16,40.7,True]],
        -3.098,MD5,coverage=COVERAGE)
    assert not check['accepted'] and check['reason']==reason


def test_onset_tier_rejects_low_rise_loud_prevoice_and_one_early_voice_frame():
    words=[[40.16,40.7,True]]
    for v,reason in [(onset_vectors(pre=.0005,post=.0008),'entry-new-voice-rise-too-small'),
                     (onset_vectors(pre=.025,post=.041),'entry-prebreath-not-quiet')]:
        checked=review.check_entry_onset(v,40,words,-3.098,MD5,coverage=COVERAGE)
        assert not checked['accepted'] and checked['reason']==reason
    v=onset_vectors();v['vocalRms'][269]=.04
    v['vocalToMixDb'][269]=20*math.log10(.04/.3)
    checked=review.check_entry_onset(v,40,words,-3.098,MD5,coverage=COVERAGE)
    assert checked['reason']=='entry-new-voice-preroll'


def test_onset_tier_requires_real_coverage_and_native_tick_preroll():
    v=onset_vectors()
    for words,cov,reason in [([[40.052,40.7,True]],COVERAGE,'entry-new-word-preroll'),
                             ([[40.16,40.7,True]],[[39.7,40.3]],'entry-asr-unobserved'),
                             ([],COVERAGE,'entry-new-word-unobserved')]:
        checked=review.check_entry_onset(v,40,words,-3.098,MD5,coverage=cov)
        assert not checked['accepted'] and checked['reason']==reason
    assert review.check_entry_onset(onset_vectors(at=40.09),40,[[40.16,40.7,True]],
        -3.098,MD5,coverage=COVERAGE)['reason']=='entry-new-voice-preroll'


def test_onset_fade_cap_finishes_before_actual_voice_and_asr_word():
    checked=review.check_entry_onset(onset_vectors(at=40.1),40,[[40.105,40.7,True]],
        -3.098,MD5,coverage=COVERAGE,fade_in=.2)
    assert checked['accepted'] and checked['fadeInSeconds']==pytest.approx(.07)
    assert checked['fadeInSeconds']<=checked['risingVoiceDelaySeconds']-.03+1e-9
    assert checked['fadeInSeconds']<=checked['nextWordDelaySeconds']-.03+1e-9


def test_entry_search_prefers_longer_preroll_and_discards_stereo_for_fade_only_change(tmp_path):
    item,song=fixtures();item['stereoEvidence']={'rowStart':40};item['stereoVersion']=1
    item['exit']['stereoVerified']=True
    v=onset_vectors();words=[[40.16,40.7,True]]
    result=review.review_checkpoint(item,song,words,lambda *a:pytest.fail('cached'),
        coverage=COVERAGE,existing_evidence=[v])
    assert result['row']['start']==40 and result['row']['fadeIn']==.08
    assert result['entryProof']['method']==review.ONSET_METHOD
    assert result['entryReviewVersion']==3 and 'stereoEvidence' not in result
    assert 'stereoVerified' not in result['exit']
    path=tmp_path/('a'*64+'.json');path.write_text(json.dumps(v))
    assert review.selection_error(result,song,evidence_folder=tmp_path) is None
    # Exactly100ms can move20ms earlier when the same observed breath supports it.
    result2=review.review_checkpoint(item,song,[[40.14,40.7,True]],lambda *a:None,
        coverage=COVERAGE,existing_evidence=[onset_vectors(at=40.1)])
    assert result2['row']['start']==pytest.approx(39.98)
    assert result2['entryProof']['risingVoiceDelaySeconds']==pytest.approx(.12)


def test_onset_final_gate_replays_all_metrics_fade_coverage_and_current_gain(tmp_path):
    item,song=fixtures();v=onset_vectors();words=[[40.16,40.7,True]]
    result=review.review_checkpoint(item,song,words,lambda *a:v,coverage=COVERAGE)
    path=tmp_path/('a'*64+'.json');path.write_text(json.dumps(v))
    assert review.selection_error(result,song,evidence_folder=tmp_path) is None
    changed=copy.deepcopy(result);changed['entryProof']['vocalRiseDb']+=.1
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-proof-mismatch'
    changed=copy.deepcopy(result);changed['row']['fadeIn']=.1
    changed['entryReviewInputHash']=review.fingerprint(changed,song,[])
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-proof-mismatch'
    louder={**song,'lufs':-25.}
    changed=copy.deepcopy(result);changed['entryReviewInputHash']=review.fingerprint(changed,louder,[])
    assert review.selection_error(changed,louder,evidence_folder=tmp_path)=='entry-proof-mismatch'
    changed=copy.deepcopy(result);changed['observedCoverage']=[[0,40.3]]
    changed['entryReviewInputHash']=review.fingerprint(changed,song,[])
    assert review.selection_error(changed,song,evidence_folder=tmp_path)=='entry-proof-entry-asr-unobserved'
    v['vocalRms'][269]=.04;v['vocalToMixDb'][269]=20*math.log10(.04/.3)
    path.write_text(json.dumps(v))
    assert review.selection_error(result,song,evidence_folder=tmp_path)=='entry-proof-entry-new-voice-preroll'


def cached_vector(folder,offset=37.3,digest=MD5):
    from aidj.fixed_set_vocal_review import VERSION as CACHE_VERSION
    value=vectors(offset);value['md5']=digest
    value['requestedEndSeconds']=offset+value['durationSeconds']
    key=hashlib.sha256(json.dumps([CACHE_VERSION,digest,offset,value['requestedEndSeconds'],MODEL_SHA256],
        separators=(',',':')).encode()).hexdigest()
    value['cacheKey']=key;path=folder/(key+'.json');path.write_text(json.dumps(value))
    return value,path


def test_discovered_source_pinned_raw_cache_recovers_lost_refs_without_audio(tmp_path):
    from aidj.fixed_set_vocal_review import CachedProbe
    item,song=fixtures();raw,path=cached_vector(tmp_path)
    index=review.discover_cached_references(tmp_path)
    assert set(index)=={MD5} and index[MD5][0]['cacheKey']==raw['cacheKey']
    assert set(index[MD5][0])=={'cacheKey','sourceOffsetSeconds','durationSeconds','requestedEndSeconds','modelSha256'}
    refs=review.entry_cached_references(item,index[MD5])
    probe=CachedProbe(Path('not-downloaded'),song,tmp_path,'not-used','not-used')
    existing=probe.existing(refs,limit=None)
    result=review.review_checkpoint(item,song,[],lambda *a:pytest.fail('lost refs must not force audio work'),
        coverage=COVERAGE,existing_evidence=existing)
    assert result['entryProof']['accepted'] and result['entryReviewBudget']['newProbes']==0
    assert result['entryProof']['cacheKey']==raw['cacheKey']


@pytest.mark.parametrize('change',['wrong-source','wrong-key','wrong-model','bad-offset','bad-end','bad-duration','bad-array'])
def test_cached_reference_discovery_drops_forged_identity_bounds_or_arrays(tmp_path,change):
    value,path=cached_vector(tmp_path)
    if change=='wrong-source':value['md5']='2'*32
    elif change=='wrong-key':value['cacheKey']='f'*64
    elif change=='wrong-model':value['modelSha256']='f'*64
    elif change=='bad-offset':value['sourceOffsetSeconds']=-1.
    elif change=='bad-end':value['requestedEndSeconds']=1801.
    elif change=='bad-duration':value['durationSeconds']=1.
    else:value['vocalRms'][0]=float('nan')
    path.write_text(json.dumps(value))
    assert review.discover_cached_references(tmp_path)=={}


def test_discovery_skips_oversized_malformed_names_and_keeps_sources_separate(tmp_path,monkeypatch):
    import builtins
    first,_=cached_vector(tmp_path);second,_=cached_vector(tmp_path,offset=50.,digest='2'*32)
    huge=tmp_path/('f'*64+'.json')
    with huge.open('wb') as stream:stream.truncate(review.MAX_CACHED_EVIDENCE_BYTES+1)
    (tmp_path/'malformed-name.json').write_text('{}')
    (tmp_path/('e'*64+'.json')).write_text('not json')
    old_open=builtins.open
    def guarded_open(file,*args,**kwargs):
        assert str(file)!=str(huge),'oversized JSON must never be opened'
        return old_open(file,*args,**kwargs)
    monkeypatch.setattr(builtins,'open',guarded_open)
    index=review.discover_cached_references(tmp_path)
    assert set(index)=={MD5,'2'*32}
    assert index[MD5][0]['cacheKey']==first['cacheKey']
    assert index['2'*32][0]['cacheKey']==second['cacheKey']


def test_selected_entry_proof_raw_reference_has_priority_and_is_not_duplicated():
    selected={'cacheKey':'f'*64};other={'cacheKey':'a'*64}
    refs=review.entry_cached_references({'entryProof':selected,'entryVocalEvidence':[other],
        'vocalEvidence':[selected]},[other,selected,{'cacheKey':'b'*64}])
    assert [r['cacheKey'] for r in refs]==['f'*64,'a'*64,'b'*64]
