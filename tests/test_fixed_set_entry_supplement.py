"""Bounded supplemental ASR keeps and replays the original raw lineage."""
import copy
import json

import pytest
from aidj import fixed_set_entry_resolve as r
from tests.test_fixed_set_entry_resolve import MD5, Observer, fixture, vector, verifier

FP='b'*64

def parent(folder):
    item,song,_=fixture()
    initial=r.numeric_state({'words':[[0.,95.,True],[98.7,103.5,True],[161.5,180.,True]],
        'coverage':[[0.,180.]],'repairProbes':[]},MD5)
    state=copy.deepcopy(initial);ops=[];observer=Observer(folder)
    for a,b in [(80.,100.),(102.,122.)]:
        obs=observer.asr(a,b);before=r.operation_hash(state)
        state=r.numeric_state(r.apply_asr(state,obs),MD5)
        op={'version':2,'beforeHash':before,'afterHash':r.operation_hash(state),
            'asrEvidenceKey':obs['cacheKey'],'modelFingerprint':FP,'start':a,
            'requestedEnd':b,'actualEnd':b,'confidentWords':3,
            'replacementPolicy':'replace-observed-interior-1s'}
        ops.append({**op,'operationHash':r.operation_hash(op)})
    item.update(entryResolveVersion=2,entryResolveAsrSource='mix',entryResolveInput=initial,
        entryResolveInputHash=r.operation_hash(initial),entryResolveOperations=ops,
        entryResolveOutputHash=r.operation_hash(state),observedWords=state['words'],
        observedCoverage=state['coverage'],repairProbes=state['repairProbes'],
        entryResolveAttempts=[{'start':80.,'end':100.},{'start':102.,'end':122.}])
    assert [98.7,103.5,True] in state['words']
    assert r.lineage_error(item,song,evidence_folder=folder,model_fingerprint=FP) is None
    return item,song

def supplement(folder,*,empty=False,quiet=True,windows=None,verify=None):
    item,song=parent(folder);v=vector(quiet=quiet)
    (folder/(v['cacheKey']+'.json')).write_text(json.dumps(v));observer=Observer(folder,empty=empty)
    result=r.refine_supplement(item,song,observer,windows or [[92.,112.],[94.,114.]],
        evidence_folder=folder,model_fingerprint=FP,existing=[v],
        verify_entry=verify or verifier(folder))
    return item,song,result,observer

def test_three_raw_operations_preserve_parent_and_replace_only_fully_observed_word(tmp_path):
    old,song,new,observer=supplement(tmp_path)
    assert new['status']=='ready' and observer.calls==[('asr',92.,112.)]
    assert len(new['entryResolveOperations'])==3
    assert new['entryResolveOperations'][:2]==old['entryResolveOperations']
    assert new['entryResolveInput']==old['entryResolveInput']
    assert new['entryResolveInputHash']==old['entryResolveInputHash']
    assert [98.7,103.5,True] not in new['observedWords']
    assert [0.,95.,True] in new['observedWords']
    assert new['exit']==old['exit'] and new['sourceEnding']==old['sourceEnding']
    for key in ['end','fadeOut']:assert new['row'][key]==old['row'][key]
    assert 'stereoEvidence' not in new and new['entryShiftNeedsStereoRecheck']
    assert new['entryResolveSupplement']['addedOperationCount']==1
    assert r.lineage_error(new,song,evidence_folder=tmp_path,model_fingerprint=FP) is None

def test_four_raw_operations_empty_asr_preserves_blocking_word_and_serial_budget(tmp_path):
    old,song,new,observer=supplement(tmp_path,empty=True)
    assert new['entryReviewOnly'] and [98.7,103.5,True] in new['observedWords']
    assert len(new['entryResolveOperations'])==4
    assert [x[0] for x in observer.calls]==['asr','vocal','asr','vocal']
    assert new['entryResolveBudget']['maximumASRAudioSeconds']==40
    assert new['entryResolveBudget']['modelProcessesOverlap'] is False
    assert r.lineage_error(new,song,evidence_folder=tmp_path,model_fingerprint=FP) is None

@pytest.mark.parametrize('mutate',[
    lambda x:x.pop('entryResolveSupplement'),
    lambda x:x.pop('entryResolveSupplementVersion'),
    lambda x:x['entryResolveSupplement'].update(version=2),
    lambda x:x['entryResolveSupplement'].update(sourceMD5='c'*32),
    lambda x:x['entryResolveSupplement'].update(modelFingerprint='c'*64),
    lambda x:x['entryResolveSupplement'].update(parentInputHash='c'*64),
    lambda x:x['entryResolveSupplement'].update(parentOutputHash='c'*64),
    lambda x:x['entryResolveSupplement'].update(parentOperationsHash='c'*64),
    lambda x:x['entryResolveSupplement'].update(baseOperationCount=3),
    lambda x:x['entryResolveSupplement'].update(addedOperationCount=3),
    lambda x:x['entryResolveSupplement'].update(requestedWindowsHash='c'*64),
    lambda x:x['entryResolveSupplement']['parameters'].update(maximumAddedContexts=3),
    lambda x:x.update(entryResolveAsrSource='vocals'),
    lambda x:x['entryResolveOperations'].append(copy.deepcopy(x['entryResolveOperations'][-1])),
    lambda x:x['entryResolveOperations'].__setitem__(0,'malformed'),
])
def test_supplement_declaration_and_budget_tamper_rejected(tmp_path,mutate):
    _,song,item,_=supplement(tmp_path,empty=True);mutate(item)
    assert r.lineage_error(item,song,evidence_folder=tmp_path,model_fingerprint=FP)

def test_undisclosed_four_operation_chain_rejected_even_with_valid_raw_replay(tmp_path):
    _,song,item,_=supplement(tmp_path,empty=True)
    item.pop('entryResolveSupplement');item.pop('entryResolveSupplementVersion')
    assert r.lineage_error(item,song,evidence_folder=tmp_path,model_fingerprint=FP)

@pytest.mark.parametrize('windows',[[],[[0,21]],[[0,20]]*2,[[0,20],[20,40],[40,60]],[[170,190]],[[True,20]],[[float('nan'),20]]])
def test_explicit_windows_bounded_before_any_observation(tmp_path,windows):
    old,song=parent(tmp_path);obs=Observer(tmp_path)
    with pytest.raises(RuntimeError):r.refine_supplement(old,song,obs,windows,
        evidence_folder=tmp_path,model_fingerprint=FP,verify_entry=verifier(tmp_path))
    assert obs.calls==[]

def test_old_window_repeat_and_repeated_supplement_rejected_before_native(tmp_path):
    old,song,new,_=supplement(tmp_path,empty=True);obs=Observer(tmp_path)
    for item,windows in [(old,[[80.,100.]]),(new,[[90.,110.]])]:
        with pytest.raises(RuntimeError):r.refine_supplement(item,song,obs,windows,
            evidence_folder=tmp_path,model_fingerprint=FP,verify_entry=verifier(tmp_path))
    assert obs.calls==[]

def test_changed_parent_raw_observation_is_not_replaced_with_fresh_initial_state(tmp_path):
    item,song=parent(tmp_path);key=item['entryResolveOperations'][0]['asrEvidenceKey']
    p=tmp_path/(key+'.json');raw=json.loads(p.read_text());raw['words']=[];raw['confident']=0;p.write_text(json.dumps(raw))
    obs=Observer(tmp_path)
    with pytest.raises(RuntimeError,match='PARENT_LINEAGE'):r.refine_supplement(item,song,obs,[[92.,112.]],
        evidence_folder=tmp_path,model_fingerprint=FP,verify_entry=verifier(tmp_path))
    assert obs.calls==[]

def test_final_public_gate_failure_does_not_promote_supplement(tmp_path):
    _,song,new,_=supplement(tmp_path,verify=lambda *a:'current-owner-or-source-veto')
    assert new['entryReviewOnly'] and new['entryResolveGateFailures']==['current-owner-or-source-veto']
    assert len(new['entryResolveOperations'])==4
    assert r.lineage_error(new,song,evidence_folder=tmp_path,model_fingerprint=FP) is None

def test_existing_owner_certified_entry_keeps_two_ops_without_native_or_new_declaration(tmp_path):
    old,song=parent(tmp_path);obs=Observer(tmp_path)
    from aidj.fixed_set_entry_review import selection_error
    result=r.refine_supplement(old,song,obs,[[92.,112.]],evidence_folder=tmp_path,
        model_fingerprint=FP,owner=[100.27],verify_entry=lambda row,current:
        selection_error(row,current,owner=[100.27],evidence_folder=tmp_path))
    assert result['status']=='ready' and obs.calls==[]
    assert result['entryResolveOperations']==old['entryResolveOperations']
    assert 'entryResolveSupplementVersion' not in result
