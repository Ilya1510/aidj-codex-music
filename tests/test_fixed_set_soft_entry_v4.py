"""Explicit diagnostic VAD role; every prior proof keeps its exact contract."""
import copy
import hashlib
import json
import pytest
from aidj import fixed_set_soft_entry as soft
from aidj.fixed_set_entry_review import selection_error as public_gate, fingerprint
from tests.test_fixed_set_soft_entry import fixture, voice_at, checkpoint


def high_speech():
    data=fixture();speech=data[2]['speech']
    speech['leftSpeechProbability']=[.951]*len(speech['leftSpeechProbability'])
    speech['rightSpeechProbability']=[.87]*len(speech['rightSpeechProbability'])
    speech['maxSpeechProbability']=[.951]*len(speech['maxSpeechProbability'])
    return data


def check_v4(data,**kwargs):
    return soft.check_entry_v4(data[1],data[2],80.,data[0],data[3],data[4],**kwargs)


def saved_v4(tmp_path,data):
    item=checkpoint(fixture());proof=check_v4(data);assert proof['accepted']
    item['observedWords']=copy.deepcopy(data[3]);item['observedCoverage']=copy.deepcopy(data[4])
    item['entryProof']={**proof,'cacheKey':data[1]['cacheKey'],'mixCacheKey':data[2]['cacheKey']}
    item['entryReviewInputHash']=fingerprint(item,data[0],[])
    folder=tmp_path/'work'/'vocal-evidence';folder.mkdir(parents=True);mixes=folder.parent/'mix-vad-probes';mixes.mkdir()
    curated=tmp_path/'dj1-curation';curated.mkdir();(curated/'owner-rejected.json').write_text('{}')
    (folder/(data[1]['cacheKey']+'.json')).write_text(json.dumps(data[1]));(mixes/(data[2]['cacheKey']+'.json')).write_text(json.dumps(data[2]))
    return item,folder


def test_high_actual_channel_scores_remain_visible_as_diagnostics_only_in_explicit_v4():
    data=high_speech();before=copy.deepcopy(data)
    for checker in(soft.check_entry,soft.check_entry_v2,soft.check_entry_v3):
        assert checker(data[1],data[2],80.,data[0],data[3],data[4])['reason']=='raw-mix-speech-at-entry'
    proof=check_v4(data)
    assert proof['accepted']and proof['version']==4
    assert proof['parameters']=={**soft.PARAMETERS_V3,'mixSpeechMax':None,'mixSpeechDecisionRole':'diagnostic-only'}
    assert proof['leftMixSpeechMax']==.951 and proof['rightMixSpeechMax']==.87
    assert proof['mixSpeechThresholdApplied']is False and proof['speechProbabilityDiagnosticOnly']is True
    assert 'measured-MIX-diagnostic'in proof['confidenceKind']
    assert all(proof[k]is False for k in('voiceAbsenceProven','naturalSilenceClaimed','verifiedWordBoundary','ASRWordsErased','asrOverrideApplied'))
    assert data==before


@pytest.mark.parametrize('change',[
    lambda d:d[3].append([79.95,80.03,True]),
    lambda d:d[3].append([79.95,80.03,False]),
    lambda d:d[4].clear(),lambda d:d[3].__setitem__(1,[80.09,81.,False]),
    lambda d:voice_at(d[1],d[0],80.08,-20.),lambda d:voice_at(d[1],d[0],79.9,-29.9),
    lambda d:d[0].update(lufs=-25.),lambda d:d[2]['identity'].update(requestedEndSeconds=84.),
    lambda d:d[2]['speech']['maxSpeechProbability'].__setitem__(94,.001),
    lambda d:d[2].update(normalizedInputSHA256='missing'),
])
def test_no_vad_cutoff_does_not_relax_any_word_energy_source_model_or_frame_guard(change):
    data=high_speech();change(data);assert not check_v4(data)['accepted']


def test_stereo_mix_remains_mandatory_and_owner_negative_is_still_a_veto():
    data=high_speech()
    assert not check_v4(data,rejected=[80.2])['accepted']
    assert soft.check_entry_v4(data[1],None,80.,data[0],data[3],data[4])['reason']=='invalid-raw-stereo-mix-evidence'


def test_public_v4_gate_reloads_actual_scores_and_rejects_policy_or_diagnostic_claim_tamper(tmp_path):
    data=high_speech();item,folder=saved_v4(tmp_path,data)
    assert public_gate(item,data[0],evidence_folder=folder)is None
    for mutate in[lambda p:p.update(version=3),lambda p:p['parameters'].update(mixSpeechMax=.99),
            lambda p:p.update(leftMixSpeechMax=.001),lambda p:p.update(speechProbabilityDiagnosticOnly=False),
            lambda p:p.update(mixSpeechThresholdApplied=True),lambda p:p.update(voiceAbsenceProven=True),
            lambda p:p['parameters'].update(mixSpeechDecisionRole='silence-proof')]:
        wrong=copy.deepcopy(item);mutate(wrong['entryProof']);assert public_gate(wrong,data[0],evidence_folder=folder)


@pytest.mark.parametrize('variant,expected',[
    ('default','daef98084895cc27aa391bcb5d891f5a5cde65c6f9bd9ca8218ed07553f45472'),
    ('no-future','7a0dae44d0200ec4e0bdd79f14b6cbafff64191dcdd2736dbef1d9027d0bcd0c'),
    ('far-future','10b2daf60ae25f90df07d3247ee8b1d4fb615564967ddcff5995cdf5a2a16a31'),
    ('rise','5776756b76675c13ba1f98b4192273cdbe630bc80eaca3a40742af59daa2349c'),
    ('duplicates','d82726b6cb74997d5854ce3fd1502554df0f87212c9979cf446d86191d7e5509'),
])
def test_v3_proofs_match_pre_v4_commit_bytes(variant,expected):
    data=fixture()
    if variant=='no-future':data[3][:]=[[0.,79.7,True]]
    if variant=='far-future':data[3][1]=[95.,96.,False]
    if variant=='rise':voice_at(data[1],data[0],80.15,-20.)
    if variant=='duplicates':data[3].append([80.4,80.8,True])
    proof=soft.check_entry_v3(data[1],data[2],80.,data[0],data[3],data[4])
    assert hashlib.sha256(json.dumps(proof,sort_keys=True,separators=(',',':')).encode()).hexdigest()==expected


def test_no_arbitrary_runtime_cutoff_or_unversioned_diagnostic_mode():
    data=high_speech()
    for kwargs in[{'mix_cap':None},{'version':4.0},{'diagnostic_only':True}]:
        with pytest.raises(TypeError):check_v4(data,**kwargs)
    for version in[True,4.0,'v4',5,None]:assert soft._policy(version)is None
