"""A short measured breath can precede a later word without a600ms ASR gap."""
import copy
import json
import math

import pytest

from aidj import fixed_set_entry_review as review
from aidj.vocal_probe import MODEL_SHA256

MD5='1'*32
COVERAGE=[[0.,180.]]


def fixtures(*,rise=None):
    voices=[.0005 if rise is None or 37.3+i*.01<rise-1e-7 else .04 for i in range(540)]
    raw={'md5':MD5,'modelSha256':MODEL_SHA256,'sourceOffsetSeconds':37.3,'durationSeconds':5.4,
         'requestedEndSeconds':42.7,'frameHopSeconds':.01,'cacheKey':'a'*64,'warnings':[],
         'vocalRms':voices,'mixRms':[.3]*540,'instrumentalRms':[.3]*540,
         'vocalToMixDb':[20*math.log10(v/.3) for v in voices]}
    row={'trackId':'a'*24,'md5':MD5,'start':40.,'end':100.,'fadeIn':.1,'fadeOut':.8,
         'transitionKind':'clean-cut','transitionSeconds':0,'rate':1.}
    item={'trackId':row['trackId'],'md5':MD5,'row':row,'status':'ready','entry':{'time':40.},
          'exit':{'validated':True,'stereoVerified':True},'stereoVersion':1,'stereoEvidence':{'rowStart':40.}}
    song={'md5':MD5,'lo':0.,'hi':180.,'duration':180.,'manualStart':False,'lufs':-16.,'peak':-2.}
    words=[[39.1,39.85,True],[40.8,41.,True]]
    return item,song,raw,words


def checked(raw,words,**kw):
    return review.check_entry_preroll(raw,40,words,-3.098,MD5,coverage=COVERAGE,**kw)


def test_later_new_word_and_quiet_instrumental_window_need_no350ms_word_deadline():
    _,_,raw,words=fixtures()
    assert not review.check_entry(raw,40,words,-3.098,MD5,coverage=COVERAGE)['accepted']
    assert not review.check_entry_onset(raw,40,words,-3.098,MD5,coverage=COVERAGE)['accepted']
    proof=checked(raw,words,fade_in=.1)
    assert proof['accepted'] and proof['method']==review.PREROLL_METHOD
    assert proof['nextObservedWord']==[40.8,41.,True]
    assert proof['fadeInSeconds']==.08 and proof['risingVoiceTime'] is None
    assert proof['riseObservedWithin350ms'] is False and proof['legacy300msAsrClear'] is False
    assert proof['verifiedWordBoundary'] is False and proof['naturalSilenceClaimed'] is False


@pytest.mark.parametrize('confidence',[True,False])
@pytest.mark.parametrize('word',[[39.8,40.2],[39.99,40.01],[39.7,39.95],[40.04,40.2]])
def test_every_known_word_crossing_the80ms_guard_vetoes_even_an_internal_syllable_silence(word,confidence):
    _,_,raw,words=fixtures()
    proof=checked(raw,[[*word,confidence],*words])
    assert not proof['accepted'] and proof['reason']=='entry-word-crosses-preroll-guard'


def test_low_confidence_future_word_is_retained_and_ramp_still_finishes_before_it():
    _,_,raw,_=fixtures(rise=40.12)
    proof=checked(raw,[[39.5,39.85,False],[40.16,40.8,False]],fade_in=.1)
    assert proof['accepted'] and proof['allConfidenceASRVeto']
    assert proof['nextObservedWord']==[40.16,40.8,False]
    assert proof['risingVoiceTime']==pytest.approx(40.12)
    assert proof['fadeInSeconds']<=proof['risingVoiceDelaySeconds']-.03+1e-7
    assert 40+proof['fadeInSeconds']<=40.16-.03


def test_one_early_vocal_frame_is_checked_before_percentiles_or_average_can_hide_it():
    _,_,raw,words=fixtures(rise=40.12)
    raw['vocalRms'][278]=.04
    raw['vocalToMixDb'][278]=20*math.log10(.04/.3)
    proof=checked(raw,words)
    assert not proof['accepted'] and proof['reason']=='entry-new-voice-preroll'
    raw['vocalRms'][270]=.04;raw['vocalToMixDb'][270]=20*math.log10(.04/.3)
    assert checked(raw,words)['reason']=='entry-vocal-not-quiet'


def test_ramp_cap_protects_both_actual_voice_and_next_retained_word():
    _,_,raw,_=fixtures(rise=40.1)
    proof=checked(raw,[[40.2,40.5,True]],fade_in=.1)
    assert proof['accepted'] and proof['fadeInSeconds']==pytest.approx(.07)
    _,_,raw,_=fixtures(rise=40.2)
    proof=checked(raw,[[40.09,40.5,False]],fade_in=.1)
    assert proof['accepted'] and proof['fadeInSeconds']==pytest.approx(.06)


@pytest.mark.parametrize('change',[
    lambda v:v.update(md5='2'*32),lambda v:v.update(modelSha256='wrong'),
    lambda v:v.update(warnings=['near-zero-vocal-output']),
    lambda v:v['vocalRms'].__setitem__(270,float('nan'))])
def test_unverified_or_malformed_raw_evidence_never_certifies_the_new_tier(change):
    _,_,raw,words=fixtures();change(raw)
    assert not checked(raw,words)['accepted']


def test_coverage_and_longer_rise_search_cannot_be_assumed_from200ms_window():
    _,_,raw,words=fixtures()
    assert not review.check_entry_preroll(raw,40,words,-3.098,MD5,coverage=[[39.7,40.3]])['accepted']
    raw.update(sourceOffsetSeconds=39.5,durationSeconds=.8,requestedEndSeconds=40.3)
    for key in ['vocalRms','mixRms','instrumentalRms','vocalToMixDb']:raw[key]=raw[key][:80]
    assert not checked(raw,words)['accepted']


@pytest.mark.parametrize('words',[None,[[1,2]],[[2,1,True]],[[1,2,'false']],[[float('nan'),2,False]]])
def test_malformed_words_fail_closed_without_an_unpacking_exception(words):
    _,_,raw,_=fixtures()
    assert checked(raw,words)['reason']=='invalid-entry-words'


def test_search_uses_new_tier_only_after_legacy_checks_and_preserves_an_already_valid_exit():
    item,song,raw,words=fixtures()
    def forbidden(*args):raise AssertionError('known numeric context is sufficient')
    result=review.review_checkpoint(item,song,words,forbidden,coverage=COVERAGE,existing_evidence=[raw])
    assert result['entryProof']['method']==review.PREROLL_METHOD
    assert result['row']['start']==40 and result['row']['end']==100 and result['row']['fadeOut']==.8
    assert result['row']['fadeIn']==.08 and result['entryShiftNeedsStereoRecheck']
    assert 'stereoVersion' not in result and 'stereoEvidence' not in result
    assert result['entryReviewVersion']==3  # Existing validated tiers stay valid.
    assert not result['entryReviewOnly'] and result['status']=='ready'


def test_final_gate_reloads_raw_provenance_parameters_words_and_fade(tmp_path):
    item,song,raw,words=fixtures()
    result=review.review_checkpoint(item,song,words,lambda *args:raw,coverage=COVERAGE,existing_evidence=[raw])
    path=tmp_path/('a'*64+'.json');path.write_text(json.dumps(raw))
    assert review.selection_error(result,song,evidence_folder=tmp_path) is None
    for change in (lambda v:v['entryProof']['parameters'].update(asrCrossingMarginSeconds=.01),
                   lambda v:v['entryProof'].update(risingVoiceTime=40.2),
                   lambda v:v['row'].update(fadeIn=.1)):
        changed=copy.deepcopy(result);change(changed)
        changed['entryReviewInputHash']=review.fingerprint(changed,song,[])
        assert review.selection_error(changed,song,evidence_folder=tmp_path) is not None
    raw['vocalRms'][278]=.04;raw['vocalToMixDb'][278]=20*math.log10(.04/.3)
    path.write_text(json.dumps(raw))
    assert review.selection_error(result,song,evidence_folder=tmp_path)=='entry-proof-entry-new-voice-preroll'


def test_wide_scanner_and_certified_pair_replay_the_new_method_without_native_work(monkeypatch):
    from aidj import fixed_set_hard_review as hard
    item,song,raw,words=fixtures();evidence={'words':words,'coverage':COVERAGE}
    entries=hard.scanned_entries(song,evidence,[raw])
    found=[v for v in entries if v['kind']==review.PREROLL_METHOD]
    assert found and all(v['fade']<=.08 for v in found)
    good=min(found,key=lambda v:abs(v['time']-40))
    # Just beyond100s excludes the independent physical-source-zero fallback,
    # so this assertion actually exercises the scanned candidate's raw proof.
    endpoint={'point':{'time':100.01,'fade':.8},'score':0}
    monkeypatch.setattr(hard,'scanned_entries',lambda *a,**kw:[good])
    assert hard.pair(item['row'],song,evidence,[endpoint],[raw],certified_only=True) is not None
    for change in ({'fade':.3},{'time':good['time']+.01},
                   {'vector':{**raw,'md5':'2'*32}},
                   {'check':{**good['check'],'playbackGainDb':-99}}):
        monkeypatch.setattr(hard,'scanned_entries',lambda *a,**kw:[{**good,**change}])
        assert hard.pair(item['row'],song,evidence,[endpoint],[raw],certified_only=True) is None
    monkeypatch.setattr(hard,'scanned_entries',lambda *a,**kw:[good])
    changed={**evidence,'words':[*words,[39.9,40.1,False]]}
    assert hard.pair(item['row'],song,changed,[endpoint],[raw],certified_only=True) is None
    assert hard.pair(item['row'],song,{**evidence,'coverage':[]},[endpoint],[raw],certified_only=True) is None


def test_new_wide_method_cannot_escape_owner_veto_current_range_or_segment_length():
    from aidj import fixed_set_hard_review as hard
    item,song,raw,words=fixtures();evidence={'words':words,'coverage':COVERAGE}
    # Dense retained words block the rest of this excerpt; the only possible
    # breath is around40. Its entire neighborhood is owner-vetoed here.
    evidence['words']=[[0.,39.85,True],[40.8,180.,True]]
    assert hard.scanned_entries(song,evidence,[raw],rejected=[40.2,40.6])==[]
    assert hard.scanned_entries({**song,'lo':41.},evidence,[raw])==[]
    assert hard.pair(item['row'],song,evidence,[{'point':{'time':44.,'fade':.8},'score':0}],
                     [raw],certified_only=True) is None
