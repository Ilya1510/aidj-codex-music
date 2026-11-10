"""Explicit V2 cap; V1 serialization/default and every other guard stay intact."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from aidj import fixed_set_soft_entry as soft
from aidj.fixed_set_entry_review import selection_error as public_gate
from tests.test_fixed_set_soft_entry import fixture, checkpoint, voice_at

OWNER_BODIES = json.loads((Path(__file__).parent/'fixtures/soft_entry_owner_mix_bodies.json').read_text())['cases']


def owner_body(case):
    """Real unchanged native body traces, explicit synthetic surrounding fixture."""
    data = fixture(); speech = data[2]['speech']
    indexes = [i for i, (a, b) in enumerate(zip(speech['frameStartSeconds'], speech['frameEndSeconds']))
        if a < 80.05-1e-8 and b > 79.85+1e-8]
    assert len(indexes) == len(case['leftSpeechProbability'])
    for i, left, right in zip(indexes, case['leftSpeechProbability'], case['rightSpeechProbability']):
        speech['leftSpeechProbability'][i] = left; speech['rightSpeechProbability'][i] = right
        speech['maxSpeechProbability'][i] = max(left, right)
    return data


def check_v2(data, **kwargs):
    return soft.check_entry_v2(data[1], data[2], 80., data[0], data[3], data[4], **kwargs)


def saved_v2(tmp_path, data):
    item = checkpoint(fixture()); proof = check_v2(data); assert proof['accepted']
    item['entryProof'] = {**proof, 'cacheKey': data[1]['cacheKey'], 'mixCacheKey': data[2]['cacheKey']}
    work = tmp_path/'work'; folder = work/'vocal-evidence'; folder.mkdir(parents=True)
    mixes = work/'mix-vad-probes'; mixes.mkdir(); curated = tmp_path/'dj1-curation'; curated.mkdir()
    (curated/'owner-rejected.json').write_text('{}')
    (folder/(data[1]['cacheKey']+'.json')).write_text(json.dumps(data[1]))
    (mixes/(data[2]['cacheKey']+'.json')).write_text(json.dumps(data[2]))
    return item, folder


@pytest.mark.parametrize('case', OWNER_BODIES, ids=lambda c:c['title'])
def test_real_owner_good_body_traces_above_point01_require_explicit_v2(case):
    data = owner_body(case); before = copy.deepcopy(data)
    v1 = soft.check_entry(data[1], data[2], 80., data[0], data[3], data[4]); v2 = check_v2(data)
    assert not v1['accepted'] and v1['reason'] == 'raw-mix-speech-at-entry'
    assert v2['accepted'] and v2['version'] == 2 and v2['parameters']['mixSpeechMax'] == .05
    assert max(v2['leftMixSpeechMax'], v2['rightMixSpeechMax']) == case['actualBothChannelsMax']
    assert v2['parameters']['vocalMaxDbfs'] == -30 and v2['parameters']['vocalP95Dbfs'] == -33
    assert not v2['ASRWordsErased'] and not v2['asrOverrideApplied'] and not v2['nextObservedWordConfident']
    assert not v2['voiceAbsenceProven'] and not v2['verifiedWordBoundary'] and not v2['naturalSilenceClaimed']
    assert data == before


@pytest.mark.parametrize(('variant', 'expected'), [
    ('default', '3c1071ace086a03f6855bd1aced50470084dd3b00144f40946d68d673130ced8'),
    ('no-future', 'd0f07f96aadb83d8a10c7b53bc176eac8a5b19de4c83d49e3f4130fb204c7006'),
    ('far-future', '2590090f449782ea8e2c75478514d33dc43b11d612210d9137a64b43081ffe94'),
    ('rise', 'f1358e84822017d89dddbc777e43c85f9d17219bbada158332705f4af876bca2'),
    ('duplicates', '0b836c683995bfcee7585632c67f346ed6adc5540eb776efd3b36aeab16dd5b1'),
])
def test_default_v1_proofs_match_pre_v2_serialization_bit_for_bit(variant, expected):
    # Digests independently captured from the pre-V2 committed implementation.
    data = fixture()
    if variant == 'no-future': data[3][:] = [[0., 79.7, True]]
    if variant == 'far-future': data[3][1] = [95., 96., False]
    if variant == 'rise': voice_at(data[1], data[0], 80.15, -20.)
    if variant == 'duplicates': data[3].append([80.4, 80.8, True])
    proof = soft.check_entry(data[1], data[2], 80., data[0], data[3], data[4])
    assert hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == expected


def test_only_version_and_mix_cap_differ_when_both_versions_accept_the_same_inputs():
    data = fixture(); v1 = soft.check_entry(data[1], data[2], 80., data[0], data[3], data[4]); v2 = check_v2(data)
    assert v1['accepted'] and v2['accepted']
    v2['version'] = 1; v2['parameters']['mixSpeechMax'] = .01
    assert v2 == v1


@pytest.mark.parametrize('change', [
    lambda d:d[3].append([79.95, 80.03, True]),
    lambda d:d[3].append([79.95, 80.03, False]),
    lambda d:d[4].clear(),
    lambda d:d[3].__setitem__(1, [80.09, 81., False]),
    lambda d:voice_at(d[1], d[0], 80.08, -20.),
    lambda d:voice_at(d[1], d[0], 80., -29.9),
    lambda d:d[0].update(lufs=-25.),
])
def test_v2_cannot_bypass_any_word_coverage_preroll_voice_or_current_source_guard(change):
    data = fixture(); change(data)
    assert not check_v2(data)['accepted']


def test_v2_keeps_owner_veto_exact_cap_and_current_nextword_uncertainty():
    data = fixture()
    assert not check_v2(data, rejected=[80.2])['accepted']
    speech = data[2]['speech']; speech['rightSpeechProbability'] = [.05]*len(speech['rightSpeechProbability'])
    speech['maxSpeechProbability'] = [.05]*len(speech['maxSpeechProbability'])
    assert check_v2(data)['accepted']
    speech['rightSpeechProbability'][94] = .050001; speech['maxSpeechProbability'][94] = .050001
    assert not check_v2(data)['accepted']


def test_public_gate_replays_v2_and_refuses_unknown_or_mismatched_version_threshold(tmp_path):
    data = owner_body(OWNER_BODIES[0]); item, folder = saved_v2(tmp_path, data)
    assert public_gate(item, data[0], evidence_folder=folder) is None
    for version in [None, 0, 3, True, 2.0, 'v2']:
        wrong = copy.deepcopy(item); wrong['entryProof']['version'] = version
        assert public_gate(wrong, data[0], evidence_folder=folder) == 'soft-entry-policy-version-or-parameters'
    for change in [lambda p:p.update(version=1), lambda p:p['parameters'].update(mixSpeechMax=.1),
            lambda p:p['parameters'].update(vocalMaxDbfs=-20.), lambda p:p['parameters'].update(mixSpeechMax=.03)]:
        wrong = copy.deepcopy(item); change(wrong['entryProof'])
        assert public_gate(wrong, data[0], evidence_folder=folder) == 'soft-entry-policy-version-or-parameters'


def test_no_arbitrary_runtime_mix_cap_argument_is_available():
    data = fixture()
    with pytest.raises(TypeError):
        soft.check_entry_v2(data[1], data[2], 80., data[0], data[3], data[4], mix_cap=.1)
