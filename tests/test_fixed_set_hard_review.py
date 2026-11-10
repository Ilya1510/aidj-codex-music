"""Hard cases get genuinely new context, never looser word or energy guards."""
import copy
import hashlib
import json
import math
from types import SimpleNamespace

import pytest

import aidj.fixed_set_hard_review as hard
from aidj.vocal_probe import MODEL_SHA256


def vector(offset, duration, voice=.000001, mix=.001):
    n=round(duration/.01)
    voices=[voice]*n if isinstance(voice,(int,float)) else list(voice)
    levels=[mix]*n if isinstance(mix,(int,float)) else list(mix)
    return {'md5':'1'*32,'modelSha256':MODEL_SHA256,'sourceOffsetSeconds':offset,
        'durationSeconds':duration,'frameHopSeconds':.01,'warnings':[],
        'cacheKey':hashlib.sha256(str((offset,duration)).encode()).hexdigest(),
        'vocalRms':voices,'mixRms':levels,
        'instrumentalRms':[max(0,a-b) for a,b in zip(levels,voices)],
        'vocalToMixDb':[20*math.log10(max(1e-8,b)/max(1e-8,a)) for a,b in zip(levels,voices)]}


def fixture(end=70):
    old={'trackId':'a'*24,'md5':'1'*32,'start':0.,'end':end,'fadeIn':.08,
        'fadeOut':.8,'rate':1,'transitionSeconds':0,'transitionKind':'clean-cut'}
    song={'id':'a'*24,'md5':'1'*32,'lo':0.,'hi':180.,'duration':180.,
          'artist':'Example','title':'Hard example','lufs':-16.,'peak':-2.,'manualStart':False}
    checkpoint={'status':'needs-review','trackId':'a'*24,'md5':'1'*32,'inputHash':'original',
                'old':old,'referenceRms':.1}
    return old,song,checkpoint,{'words':[],'coverage':[[0,180]],'repairProbes':[]}


def forbidden(*args):
    raise AssertionError('A valid cached pair must not run another model probe')


def test_all_cached_windows_are_reused_beyond_the_old_twelve_reference_cap():
    refs=[{'cacheKey':f'{n:064x}'} for n in range(15)]
    calls=[]
    class Probe:
        def existing(self,group):
            calls.append(group)
            return [{'cacheKey':r['cacheKey']} for r in group[:12]]
    found=hard.all_existing(Probe(),refs+refs[:3])
    assert len(found)==15 and len(calls)==15 and all(len(c)==1 for c in calls)


def test_a_short_protected_quiet_pause_can_resolve_without_relaxing_energy():
    old,song,item,evidence=fixture()
    voices=[.1]*600
    voices[300:380]=[.000001]*80  # Quiet69.0..69.8, insufficient for a long fade.
    cached=vector(66,6,voice=voices)
    result=hard.review(old,song,item,evidence,forbidden,existing=[cached])
    assert result['status']=='ready' and result['row']['fadeOut']==.3
    assert result['exit']['vocalCheck']['normalizedMixMaxDbfs']<=-35
    assert result['hardReviewBudget']['newContexts']==0
    assert item['status']=='needs-review' and result['inputHash']=='original'


def test_exact_seven_hundred_ms_pause_is_not_missed_by_a_coarse_search_grid():
    old,song,item,evidence=fixture()
    voices=[.1]*600;voices[301:371]=[.000001]*70
    result=hard.review(old,song,item,evidence,forbidden,
                       existing=[vector(66,6,voice=voices)])
    assert result['status']=='ready' and result['row']['fadeOut']==.3
    assert result['row']['end']==pytest.approx(69.51)


def test_loud_instrumental_music_cannot_get_the_short_release():
    old,song,item,evidence=fixture()
    cached=vector(66,6,mix=.5)
    result=hard.review(old,song,item,evidence,forbidden,existing=[cached])
    assert result['status']=='ready' and result['row']['fadeOut']>=2
    assert result['exit']['rampFacts']['kind']=='generated-release'
    assert result['exit']['rampFacts']['naturalSilenceClaimed'] is False


def test_continuous_vocals_remain_unresolved_after_only_two_new_contexts():
    old,song,item,evidence=fixture();calls=[]
    def probe(begin,end):
        calls.append((begin,end));return vector(begin,end-begin,voice=.1,mix=.3)
    result=hard.review(old,song,item,evidence,probe,existing=[vector(60,20,voice=.1,mix=.3)])
    assert result['status']=='needs-review' and len(calls)==2
    assert all(end-begin<=20 for begin,end in calls)
    assert result['hardReviewBudget']['maximumNewAudioSeconds']==40


def test_cached_valid_exit_gets_a_new_entry_context_instead_of_new_exit_search():
    old,song,item,evidence=fixture(160)
    # This ASR gap is a valid0.7s guarded entry breath, but too short for the
    # old entries_for0.9s rule, so no eligible entry exists before the new probe.
    evidence['words']=[[0,99.65],[100.35,158.5],[161.5,180]]
    cached=vector(155,6)
    calls=[]
    def probe(begin,end):
        calls.append((begin,end));n=round((end-begin)/.01)
        voices=[.000001 if 99.8<=begin+i*.01<=100.2 else .1 for i in range(n)]
        return vector(begin,end-begin,voice=voices,mix=.1)
    result=hard.review(old,song,item,evidence,probe,existing=[cached])
    assert result['status']=='ready' and len(calls)==1
    assert calls[0][0]<100<calls[0][1]
    assert 99.8<=result['row']['start']<=100.2
    assert result['entry']['kind']=='vocal-free-entry-breath'
    assert result['hardEntryCandidate']['asrCoverageComplete']
    assert len(result['vocalEvidence'])==len(result['entryVocalEvidence'])==2


def test_missing_asr_coverage_does_not_create_a_new_entry_from_stem_silence():
    _,song,_,evidence=fixture();v=vector(90,20)
    assert hard.scanned_entries(song,{**evidence,'coverage':[]},[v])==[]


def test_newly_measured_eof_timing_reuses_cached_stems_before_new_inference():
    old,song,item,evidence=fixture();evidence['words']=[[160,180]]
    calls=[]
    def ensure_timing():
        calls.append('real-source-timing')
        song['sourceProof']={'sourcePinned':True,'md5':song['md5'],
            'decodedDuration':180.,'metadataDuration':180.,'allowedStart':0.,
            'allowedEnd':180.,'manualEnd':False,'decoderMethod':'full-verified-decode'}
    result=hard.review(old,song,item,evidence,forbidden,owner=[110],
        existing=[vector(160,20,voice=.000001,mix=.00001)],
        ensure_source_timing=ensure_timing)
    assert calls==['real-source-timing'] and result['status']=='ready'
    assert result['hardReviewBudget']['newContexts']==0
    assert result['exit']['vocalCheck']['criterion']=='physical-EOF-quiet'
    assert result['exit']['sourceProof']['md5']==song['md5']


def test_entry_only_review_keeps_already_verified_exit_exactly():
    old,song,item,evidence=fixture(160.45)
    evidence['words']=[[0,99.65],[100.35,158.5],[161.5,180]]
    exit_={'time':160.45,'fade':.8,'fadeStart':159.65,'regionStart':159.5,'regionEnd':161.5,
           'maxRms':.0001,'relativeMax':.001,'method':'strict-quiet',
           'coverageComplete':True,'absoluteSilence':True,'wordPadding':.3}
    item.update(row={**old,'start':100.27},exit=exit_,entryReviewOnly=True,observedWords=evidence['words'])
    voices=[.000001 if 99.8<=90+i*.01<=100.2 else .1 for i in range(2000)]
    result=hard.review(old,song,item,evidence,forbidden,existing=[vector(90,20,voice=voices,mix=.1)],entry_only=True)
    assert result['status']=='ready' and result['row']['start']!=100.27
    assert result['exit']==exit_ and result['row']['end']==item['row']['end']
    assert result['row']['fadeOut']==item['row']['fadeOut'] and not result['entryReviewOnly']


def test_fresh_context_avoids_shifting_already_measured_windows():
    _,song,_,_=fixture()
    context=hard.fresh_context(song,[vector(80,20)],round_number=1)
    assert context!=(80,100)
    assert hard.fresh_context(song,[],target=100)==(90,110)


def test_full_timing_decode_is_real_eof_not_a_metadata_limited_excerpt(tmp_path,monkeypatch):
    _,song,_,_=fixture();seen=[]
    def fake_run(command,**kwargs):
        seen.append(command)
        with open(command[-1],'wb') as out:out.truncate(16000*8)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(hard.subprocess,'run',fake_run)
    timing=hard.measure_source_timing(tmp_path/'source.mp3',song,'ffmpeg',tmp_path)
    assert timing['decodedDuration']==1 and timing['metadataDuration']==180
    assert timing['sourceMD5']==song['md5'] and timing['sourceTimingMethod']=='full-verified-decode-strict-v2'
    assert all(flag not in seen[0] for flag in ('-t','-ss','-to','-shortest'))
    assert '-xerror' in seen[0] and '-fs' in seen[0]
    assert timing['decoderMethod']=='full-verified-decode-strict-v2'
    assert timing['decoderErrorPolicy']=='xerror' and timing['completeSource'] is True
    assert timing['decoderExitCode']==0 and timing['sampleRate']==16000 and timing['channels']==2
    assert timing['pcmBytes']==16000*8 and timing['pcmFrames']==16000
    assert not (tmp_path/'hard-source-timing.f32').exists()


def hints_document(song, windows=None):
    return {'version':1,'baselineRevision':'e6a17fe4ae64aae06daf','tracks':{
        song['id']:{'sourceMD5':song['md5'],'allowedRange':[song['lo'],song['hi']],
                    'sourceDuration':song['duration'],'windows':windows or [
                        {'start':110.,'end':130.,'candidateEndpoint':120.,
                         'rankScore':5,'kind':'coarse-energy-valley',
                         'indicators':{'validated':True,'newUsefulSeconds':999}}]}}}


def test_hint_file_requires_version_and_exact_baseline_identity(tmp_path):
    _,song,_,_=fixture();doc=hints_document(song);path=tmp_path/'private-hints.json'
    path.write_text(json.dumps(doc))
    assert hard.load_hints(None,'any') is None
    assert hard.load_hints(path,doc['baselineRevision'])['version']==1
    with pytest.raises(RuntimeError,match='HINTS_REVISION'):
        hard.load_hints(path,'another-version')
    doc['version']=True;path.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError,match='HINTS_VERSION'):
        hard.load_hints(path,doc['baselineRevision'])


def test_stale_hint_source_duration_or_manual_bounds_are_ignored():
    _,song,_,_=fixture();doc=hints_document(song)
    assert len(hard.hint_windows(doc,song))==1
    for change in ({'md5':'2'*32},{'hi':170},{'lo':5},{'duration':181}):
        assert hard.hint_windows(doc,{**song,**change})==[]
    # Container padding may cap decoded time slightly. The hint identity still
    # belongs to the current asset's metadata and the original allowed range.
    capped={**song,'duration':179.98,'hi':179.98,'metadataDuration':180,'originalAllowedEnd':180}
    assert len(hard.hint_windows(doc,capped))==1


def test_hint_windows_validate_all_numbers_and_context_bounds():
    _,song,_,_=fixture();good=hints_document(song)['tracks'][song['id']]['windows'][0]
    bad=[{**good,'start':-1},{**good,'end':140},{**good,'end':115},
         {**good,'candidateEndpoint':float('nan')},{**good,'rankScore':True},
         {**good,'candidateEndpoint':110.5},{**good,'end':181}]
    doc=hints_document(song,[*bad,good])
    windows=hard.hint_windows(doc,song)
    assert len(windows)==1 and windows[0]['start']==110
    assert 'indicators' not in windows[0]  # A hint's fake verdict is irrelevant.


def test_ranked_hints_precede_fractions_but_never_displace_targeted_entry_search():
    _,song,_,_=fixture();doc=hints_document(song,[
        {'start':110,'end':130,'candidateEndpoint':120,'rankScore':5,'kind':'valley'},
        {'start':50,'end':70,'candidateEndpoint':60,'rankScore':9,'kind':'new-metadata-kind'}])
    hints=hard.hint_windows(doc,song)
    detail=hard.fresh_context(song,[],hints=hints,details=True)
    assert (detail['start'],detail['end'])==(50,70)
    assert detail['reason']['rankScore']==9 and not detail['reason']['treatedAsProof']
    entry=hard.fresh_context(song,[],target=100,hints=hints,details=True)
    assert (entry['start'],entry['end'])==(90,110)
    assert entry['reason']['source']=='targeted-entry'


def test_actual_union_of_cached_interiors_blocks_nominally_new_hint():
    _,song,_,_=fixture();hints=hard.hint_windows(hints_document(song,[
        {'start':50,'end':70,'candidateEndpoint':60,'rankScore':9,'kind':'valley'}]),song)
    # Neither raw window contains the proposed20s context. Together their
    # actual guarded interiors cover it completely.
    cached=[vector(49.75,10.5),vector(59.75,10.5)]
    detail=hard.fresh_context(song,cached,hints=hints,details=True)
    assert (detail['start'],detail['end'])!=(50,70)
    assert detail['reason']['source']=='fraction-fallback'


def test_hint_requires_eight_actual_new_seconds_and_unmeasured_three_second_core():
    _,song,_,_=fixture();hints=hard.hint_windows(hints_document(song,[
        {'start':50,'end':70,'candidateEndpoint':60,'rankScore':9,'kind':'valley'}]),song)
    # A tiny old context covers the interesting3s core, despite16.5 new seconds
    # elsewhere. Re-running the same centre does not answer a new question.
    core=hard.fresh_context(song,[vector(58.25,3.5)],hints=hints,details=True)
    assert core['reason']['source']=='fraction-fallback'
    # The hint's claim of high novelty cannot hide only7.75 actual new seconds.
    overlap=hard.fresh_context(song,[vector(49.75,12.5)],hints=hints,details=True)
    assert overlap['reason']['source']=='fraction-fallback'
    fresh=hard.fresh_context(song,[vector(49.75,10.5)],hints=hints,details=True)
    assert fresh['reason']['source']=='curated-context-hint'
    assert fresh['reason']['actualNewUsefulSeconds']==pytest.approx(9.75)


def test_hints_choose_contexts_but_cannot_make_a_vocal_fragment_ready():
    old,song,item,evidence=fixture();calls=[]
    hints=hard.hint_windows(hints_document(song,[
        {'start':110,'end':130,'candidateEndpoint':120,'rankScore':9,'kind':'valley'},
        {'start':50,'end':70,'candidateEndpoint':60,'rankScore':8,'kind':'observed-ASR-gap'}]),song)
    def probe(begin,end):
        calls.append((begin,end));return vector(begin,end-begin,voice=.1,mix=.3)
    result=hard.review(old,song,item,evidence,probe,hints=hints)
    assert result['status']=='needs-review' and calls==[(110,130),(50,70)]
    assert result['hardReviewBudget']['newContexts']==2
    assert result['hardReviewAttempts'][0]['contextReason']['source']=='curated-context-hint'
    assert result['hardReviewAttempts'][0]['contextReason']['actualNewUsefulSeconds']==19.5
    assert not result['hardReviewAttempts'][0]['contextReason']['treatedAsProof']


@pytest.mark.parametrize('size,returncode',[(0,0),(9,0),(16000*1800*8,0),(16000*1800*8+8,0),(16000*8,1)])
def test_strict_full_timing_rejects_cutoff_invalid_pcm_or_decoder_error(tmp_path,monkeypatch,size,returncode):
    _,song,_,_=fixture();seen=[]
    def fake_run(command,**kwargs):
        seen.append((command,kwargs))
        with open(command[-1],'wb') as stream:stream.truncate(size)
        return SimpleNamespace(returncode=returncode)
    monkeypatch.setattr(hard.subprocess,'run',fake_run)
    with pytest.raises(RuntimeError,match='HARD_REVIEW_TIMING_DECODE'):
        hard.measure_source_timing(tmp_path/'source.mp3',song,'ffmpeg',tmp_path)
    command,kwargs=seen[0]
    assert kwargs['timeout']==120 and '-xerror' in command
    assert all(flag not in command for flag in ('-t','-ss','-to','-shortest'))
    assert command[command.index('-fs')+1]==str(16000*1800*8+8)
    assert not (tmp_path/'hard-source-timing.f32').exists()
