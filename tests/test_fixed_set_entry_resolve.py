import copy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import signal
import subprocess

import pytest

from aidj import fixed_set_entry_resolve as resolve
from aidj.fixed_set_entry_review import selection_error
from aidj.vocal_probe import MODEL_SHA256

MD5='1'*32


def vector(begin=90.,end=110.,quiet=True):
    n=round((end-begin)/.01)
    voices=[.000001 if quiet and 99.8-1e-7<=begin+i*.01<=100.2+1e-7 else .1 for i in range(n)]
    return {'md5':MD5,'modelSha256':MODEL_SHA256,'sourceOffsetSeconds':begin,
        'durationSeconds':end-begin,'frameHopSeconds':.01,'warnings':[],
        'cacheKey':hashlib.sha256(str((begin,end,quiet)).encode()).hexdigest(),
        'vocalRms':voices,'mixRms':[.3]*n,'instrumentalRms':[.3]*n,
        'vocalToMixDb':[20*math.log10(max(1e-8,v)/.3) for v in voices]}


def fixture():
    row={'trackId':'a'*24,'md5':MD5,'start':100.27,'end':160.45,'fadeIn':.1,'fadeOut':.8,
         'transitionKind':'clean-cut','transitionSeconds':0,'rate':1}
    point={'time':160.45,'fade':.8,'fadeStart':159.65,'regionStart':159.5,'regionEnd':161.5,
        'maxRms':.0001,'relativeMax':.001,'method':'strict-quiet','coverageComplete':True,
        'absoluteSilence':True,'wordPadding':.3}
    item={'status':'needs-review','entryReviewOnly':True,'md5':MD5,'trackId':row['trackId'],
        'row':row,'old':row,'exit':point,'sourceEnding':{'original':'must survive exactly'},
        'entryReviewStatus':'needs-review','entryProof':{'accepted':False,'reason':'old failure'},
        'stereoEvidence':{'old':True},'stereoVersion':1}
    song={'md5':MD5,'lo':0.,'hi':180.,'duration':180.,'manualStart':False,'lufs':-16.,'peak':-2.}
    evidence={'words':[[0.,158.5,False],[161.5,180.,True]],'coverage':[[0.,180.]],'repairProbes':[]}
    return item,song,evidence


def observation(begin,end,words=None):
    if words is None:
        words=([[begin+.2,99.65,True],[100.35,105.,True],[105.5,end-.2,True]] if begin<99 and end>106
               else [[begin+.2,begin+1,True],[begin+2,begin+3,True],[begin+4,begin+5,True]])
    return {'start':begin,'end':end,'words':words,'confident':sum(w[2] for w in words),
            'cacheKey':'a'*64,'modelFingerprint':'b'*64}


class Observer:
    def __init__(self,folder,*,empty=False,quiet=True):
        self.folder=folder;self.empty=empty;self.quiet=quiet;self.calls=[]
    def asr(self,a,b):
        self.calls.append(('asr',a,b));value=observation(a,b,[] if self.empty else None)
        value.update(version=resolve.ASR_CACHE_VERSION,sourceMD5=MD5,requestedEnd=b,
            parameters=resolve.ASR_PARAMETERS)
        value['cacheKey']=resolve.asr_key({'md5':MD5},a,b,{'fingerprint':value['modelFingerprint']})
        (self.folder/(value['cacheKey']+'.json')).write_text(json.dumps(value))
        return value
    def vocal(self,a,b):
        self.calls.append(('vocal',a,b));v=vector(a,b,quiet=self.quiet)
        (self.folder/(v['cacheKey']+'.json')).write_text(json.dumps(v));return v


def verifier(folder):
    return lambda row,song:selection_error(row,song,evidence_folder=folder)


def test_three_fresh_confident_words_replace_uncertain_blanket_but_keep_true_edge_word():
    previous={'words':[[0.,20.,False],[2.,4.,True],[30.,31.,True]],'coverage':[],'repairProbes':[]}
    fresh=[[9.,9.2,True],[10.,10.2,True],[11.,11.2,True]]
    result=resolve.apply_asr(previous,observation(2.,18.,fresh))
    assert [2.,4.,True] in result['words'] and [30.,31.,True] in result['words']
    assert [0.,3.,False] in result['words'] and [17.,20.,False] in result['words']
    assert [0.,20.,False] not in result['words']
    assert all(w in result['words'] for w in fresh)
    assert previous['words'][0]==[0.,20.,False]
    assert result['coverage']==[[2.,18.]] and result['repairProbes'][0]['confident']==3


@pytest.mark.parametrize('words',[[],[[9.,9.2,True],[10.,10.2,False]]])
def test_empty_or_weak_fresh_asr_never_erases_any_old_vocal_interval(words):
    old={'words':[[0.,20.,False],[8.,12.,True]],'coverage':[]}
    result=resolve.apply_asr(old,observation(2,18,words))
    assert result['words'][:2]==old['words'] and result['words'][2:]==words


def test_refreshed_asr_reuses_existing_vocals_and_passes_actual_public_entry_gate(tmp_path):
    item,song,evidence=fixture();v=vector();(tmp_path/(v['cacheKey']+'.json')).write_text(json.dumps(v))
    observer=Observer(tmp_path)
    result=resolve.refine(item,song,evidence,observer,existing=[v],verify_entry=verifier(tmp_path))
    assert result['status']=='ready' and not result['entryReviewOnly']
    assert [call[0] for call in observer.calls]==['asr']
    assert result['entryProof']['accepted'] and selection_error(result,song,evidence_folder=tmp_path) is None
    assert result['exit']==item['exit'] and result['sourceEnding']==item['sourceEnding']
    assert result['row']['end']==item['row']['end'] and result['row']['fadeOut']==item['row']['fadeOut']
    assert 'stereoEvidence' not in result and 'stereoVersion' not in result
    assert result['repairProbes'][0]['confident']==3 and result['entryReviewInputHash']
    assert item['row']['start']==100.27 and evidence['words'][0]==[0.,158.5,False]


def test_no_asr_or_vocal_child_runs_when_cached_current_evidence_already_certifies_entry(tmp_path):
    item,song,evidence=fixture();evidence['words']=[[0,99.65,True],[100.35,158.5,True],[161.5,180,True]]
    v=vector();(tmp_path/(v['cacheKey']+'.json')).write_text(json.dumps(v));observer=Observer(tmp_path)
    result=resolve.refine(item,song,evidence,observer,existing=[v],verify_entry=verifier(tmp_path))
    assert result['status']=='ready' and observer.calls==[] and result['entryResolveAttempts']==[]


def test_joint_context_waits_for_asr_then_gets_new_vocal_evidence_only_when_needed(tmp_path):
    item,song,evidence=fixture();v=vector(quiet=False)
    (tmp_path/(v['cacheKey']+'.json')).write_text(json.dumps(v));observer=Observer(tmp_path)
    result=resolve.refine(item,song,evidence,observer,existing=[v],verify_entry=verifier(tmp_path))
    assert result['status']=='ready' and [call[0] for call in observer.calls]==['asr','vocal']
    assert len(result['entryVocalEvidence'])==2 and result['entryResolveAttempts'][0]['vocalProbeNeeded']
    assert result['entryResolveBudget']['modelProcessesOverlap'] is False


def test_empty_asr_cannot_remove_split_word_veto_and_budget_is_two_serial_joint_windows(tmp_path):
    item,song,evidence=fixture();observer=Observer(tmp_path,empty=True)
    result=resolve.refine(item,song,evidence,observer,existing=[vector()],verify_entry=verifier(tmp_path))
    assert result['status']=='needs-review' and result['entryReviewOnly']
    assert [call[0] for call in observer.calls]==['asr','vocal','asr','vocal']
    assert all(b-a<=20 for _,a,b in observer.calls)
    assert result['entryResolveBudget']['contexts']==2
    assert evidence['words'][0] in result['observedWords'] and result['exit']==item['exit']


def test_actual_final_gate_failure_never_clears_a_previous_entry_failure(tmp_path):
    item,song,evidence=fixture();observer=Observer(tmp_path)
    result=resolve.refine(item,song,evidence,observer,existing=[vector()],verify_entry=lambda *a:'raw-cache-forged')
    assert result['status']=='needs-review' and result['entryReviewOnly']
    assert result.get('entryProof',{}).get('accepted') is not True
    assert result['entryResolveGateFailures']==['raw-cache-forged']


def test_public_entry_gate_receives_metadata_bounds_and_exact_fixed_end(tmp_path):
    item,song,evidence=fixture();song.update(hi=160.45,duration=160.45,decodedDuration=160.43)
    v=vector();(tmp_path/(v['cacheKey']+'.json')).write_text(json.dumps(v));observer=Observer(tmp_path)
    calls=[]
    def verify(row,current):
        calls.append(current['hi']);assert row['row']['end']==160.45
        return selection_error(row,current,evidence_folder=tmp_path)
    result=resolve.refine(item,song,evidence,observer,existing=[v],verify_entry=verify)
    assert result['status']=='ready' and calls==[160.45]
    assert result['row']['end']!=song['decodedDuration']


def test_final_owner_negative_still_vetoes_newly_aligned_breath(tmp_path):
    item,song,evidence=fixture();observer=Observer(tmp_path)
    result=resolve.refine(item,song,evidence,observer,existing=[vector()],rejected=[100],
        verify_entry=lambda row,current:selection_error(row,current,evidence_folder=tmp_path))
    assert result['status']=='needs-review' and result['entryReviewOnly']


def test_refiner_will_not_touch_non_entry_failures_or_a_bad_outgoing_cut(tmp_path):
    item,song,evidence=fixture();observer=Observer(tmp_path)
    with pytest.raises(RuntimeError,match='NOT_PENDING'):
        resolve.refine({**item,'entryReviewOnly':False},song,evidence,observer,verify_entry=verifier(tmp_path))
    changed=copy.deepcopy(item);changed['row']['fadeOut']=.5
    with pytest.raises(RuntimeError,match='PREVIOUS_EXIT_INVALID'):
        resolve.refine(changed,song,evidence,observer,verify_entry=verifier(tmp_path))
    assert observer.calls==[]


def test_model_manifest_pins_only_existing_local_files_and_detects_changed_weights(tmp_path):
    root=tmp_path/'models--Systran--faster-whisper-base';(root/'refs').mkdir(parents=True)
    (root/'refs/main').write_text('a'*40);folder=root/'snapshots'/('a'*40);folder.mkdir(parents=True)
    for name in ('model.bin','config.json','tokenizer.json','vocabulary.txt'):(folder/name).write_text(name)
    first=resolve.model_manifest(tmp_path);assert first['directory']==str(folder)
    (folder/'model.bin').write_text('changed')
    assert resolve.model_manifest(tmp_path)['fingerprint']!=first['fingerprint']
    (folder/'vocabulary.txt').unlink()
    with pytest.raises(RuntimeError,match='LOCAL_MODEL'):resolve.model_manifest(tmp_path)


def test_asr_cache_rechecks_source_model_window_parameters_and_boolean_word_counts():
    _,song,_=fixture();model={'fingerprint':'f'*64};a,b=90.,110.
    value={**observation(a,b),'version':1,'sourceMD5':MD5,'requestedEnd':b,
           'modelFingerprint':model['fingerprint'],'parameters':resolve.ASR_PARAMETERS,
           'cacheKey':resolve.asr_key(song,a,b,model)}
    assert resolve.validate_asr(value,song,a,b,model)==value
    for change in ({'sourceMD5':'2'*32},{'modelFingerprint':'e'*64},{'start':89.},
                   {'confident':0},{'end':109.},{'words':[[99,100,'yes']]}):
        with pytest.raises(RuntimeError,match='ENTRY_RESOLVE_ASR'):
            resolve.validate_asr({**value,**change},song,a,b,model)


def test_cached_asr_does_not_download_original_or_start_any_child(tmp_path,monkeypatch):
    _,song,_=fixture();model={'fingerprint':'f'*64};a,b=90.,110.
    folder=tmp_path/'entry-asr-evidence';folder.mkdir()
    value={**observation(a,b),'version':1,'sourceMD5':MD5,'requestedEnd':b,
           'modelFingerprint':model['fingerprint'],'parameters':resolve.ASR_PARAMETERS,
           'cacheKey':resolve.asr_key(song,a,b,model)}
    (folder/(value['cacheKey']+'.json')).write_text(json.dumps(value))
    def forbidden(*args,**kw):pytest.fail('cached words must not allocate a model or download')
    monkeypatch.setattr(resolve,'child_process',forbidden)
    observer=resolve.ChildObserver(tmp_path/'absent.mp3',song,tmp_path,'ffmpeg','uvr',model,forbidden,tmp_path)
    assert observer.asr(a,b)==value


def test_native_child_is_reaped_before_return_and_uses_a_separate_process_group(monkeypatch):
    calls=[]
    class Child:
        pid=1234
        def wait(self,timeout):calls.append(('wait',timeout));return 0
    monkeypatch.setattr(resolve.subprocess,'Popen',lambda cmd,**kw:(calls.append(('spawn',kw)) or Child()))
    resolve.child_process(['asr']);resolve.child_process(['uvr'])
    assert [c[0] for c in calls]==['spawn','wait','spawn','wait']
    assert calls[0][1]['start_new_session'] is True


@pytest.mark.parametrize('exception',[subprocess.TimeoutExpired('fake',180),KeyboardInterrupt()])
def test_timeout_or_interrupt_kills_entire_child_group_and_reaps_before_return(monkeypatch,exception):
    calls=[]
    class Child:
        pid=1234
        def wait(self,timeout):
            calls.append(('wait',timeout))
            if len(calls)==1:raise exception
            return 0
    monkeypatch.setattr(resolve.subprocess,'Popen',lambda *a,**kw:Child())
    monkeypatch.setattr(resolve.os,'killpg',lambda pid,sig:calls.append(('kill',pid,sig)))
    expected=RuntimeError if isinstance(exception,subprocess.TimeoutExpired) else KeyboardInterrupt
    with pytest.raises(expected):resolve.child_process(['asr'])
    assert calls==[('wait',180),('kill',1234,signal.SIGTERM),('wait',3)]


def test_a_killed_child_is_a_resource_failure_instead_of_silent_retry(monkeypatch):
    monkeypatch.setattr(resolve.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(wait=lambda timeout:-9))
    with pytest.raises(RuntimeError,match='CHILD_RESOURCE'):resolve.child_process(['asr'])


def test_new_asr_excerpt_uses_error_fatal_bounded_decode_not_tolerant_full_source():
    command=resolve.asr_decode_command('ffmpeg',Path('/original.mp3'),90.,110.,Path('/excerpt.f32'))
    assert '-xerror' in command and command[command.index('-ss')+1]=='90.0'
    assert command[command.index('-t')+1]=='20.0'
    assert command[command.index('-fs')+1]==str(16000*20*4+4)
    assert command[command.index('-threads')+1]=='1'
    assert command[command.index('-ar')+1]=='16000'


def test_second_context_maximizes_new_feasible_entry_seconds_instead_of_overlapping_first():
    item,song,_=fixture();item['row'].update(start=110.245,end=173.69)
    first,second=resolve.contexts(item,song)
    assert first==(100.245,120.245)
    assert second==pytest.approx((73.69,93.69))
    assert second[1]<first[0] and all(b-a==20 for a,b in (first,second))


def test_equal_novelty_windows_prefer_the_target_minute_start():
    item,song,_=fixture();item['row'].update(start=73.69,end=173.69)
    first,second=resolve.contexts(item,song)
    assert first==(63.69,83.69)
    assert second==pytest.approx((101.69,121.69))  # end−62, not arbitrary quarter.


def test_contexts_keep_manual_bounds_and_skip_a_second_window_with_no_new_feasible_entry():
    item,song,_=fixture();item['row'].update(start=100.,end=145.)
    song.update(lo=100.,hi=145.,manualStart=True)
    windows=resolve.contexts(item,song)
    assert len(windows)==1 and windows[0]==(100.,120.)


def test_policy_v2_preserves_actual_v1_recognition_cache_identity():
    _,song,_=fixture();model={'fingerprint':'f'*64}
    expected=hashlib.sha256(json.dumps([1,MD5,90.,110.,model['fingerprint'],
        resolve.ASR_PARAMETERS],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert resolve.VERSION==2 and resolve.ASR_CACHE_VERSION==1
    assert resolve.asr_key(song,90.,110.,model)==expected


def test_independent_lineage_replays_asr_cache_instead_of_trusting_stored_final_words(tmp_path):
    item,song,evidence=fixture();v=vector();(tmp_path/(v['cacheKey']+'.json')).write_text(json.dumps(v))
    observer=Observer(tmp_path)
    result=resolve.refine(item,song,evidence,observer,existing=[v],verify_entry=verifier(tmp_path))
    assert result['status']=='ready'
    args={'evidence_folder':tmp_path,'model_fingerprint':'b'*64}
    assert resolve.lineage_error(result,song,**args) is None
    assert result['entryResolveInput']['words']==evidence['words']
    assert len(result['entryResolveOperations'])==1
    changed=copy.deepcopy(result);changed['observedWords'].append([100.,100.1,False])
    assert resolve.lineage_error(changed,song,**args)=='entry-resolve-final-evidence'
    changed=copy.deepcopy(result);changed['entryResolveInput']['words'].clear()
    assert resolve.lineage_error(changed,song,**args)=='entry-resolve-input-hash'
    changed=copy.deepcopy(result);changed['entryResolveOperations'][0]['confidentWords']=0
    assert resolve.lineage_error(changed,song,**args)=='entry-resolve-operation-hash'
    assert resolve.lineage_error(result,song,**{**args,'model_fingerprint':'f'*64})=='entry-resolve-operation-input'
    key=result['entryResolveOperations'][0]['asrEvidenceKey'];path=tmp_path/(key+'.json')
    raw=json.loads(path.read_text());raw['words'][0][1]-=.01;path.write_text(json.dumps(raw))
    assert resolve.lineage_error(result,song,**args)=='entry-resolve-operation-output'


def test_empty_recognition_lineage_proves_previous_words_were_not_erased(tmp_path):
    item,song,evidence=fixture();observer=Observer(tmp_path,empty=True)
    result=resolve.refine(item,song,evidence,observer,existing=[vector()],verify_entry=verifier(tmp_path))
    assert result['status']=='needs-review'
    assert resolve.lineage_error(result,song,evidence_folder=tmp_path,model_fingerprint='b'*64) is None
    assert all(op['replacementPolicy']=='append-preserve-all-prior' for op in result['entryResolveOperations'])
    assert result['observedWords']==evidence['words']


def test_cache_only_success_also_has_an_auditable_zero_operation_snapshot(tmp_path):
    item,song,evidence=fixture();evidence['words']=[[0,99.65,True],[100.35,158.5,True],[161.5,180,True]]
    v=vector();(tmp_path/(v['cacheKey']+'.json')).write_text(json.dumps(v))
    result=resolve.refine(item,song,evidence,Observer(tmp_path),existing=[v],verify_entry=verifier(tmp_path))
    assert result['entryResolveOperations']==[]
    assert resolve.lineage_error(result,song,evidence_folder=tmp_path,model_fingerprint='b'*64) is None
    assert result['entryResolveInputHash']==result['entryResolveOutputHash']


def test_numeric_snapshot_is_bounded_and_malformed_lineage_returns_an_error():
    _,song,_=fixture()
    with pytest.raises(RuntimeError,match='SNAPSHOT_LIMIT'):
        resolve.numeric_state({'words':[['x'*(1024**2)]]},MD5)
    assert resolve.lineage_error({},song,evidence_folder=Path('/absent'),model_fingerprint='b'*64)
