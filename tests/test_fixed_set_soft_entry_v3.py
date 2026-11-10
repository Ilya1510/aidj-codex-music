"""Explicit short body: preserve whole-word/rise guards and old proof bytes."""
import copy
import hashlib
import json

import pytest
from aidj import fixed_set_soft_entry as soft
from aidj.fixed_set_entry_review import selection_error as public_gate
from tests.test_fixed_set_soft_entry import fixture, checkpoint, voice_at


def short_gap():
    data = fixture()
    # A previous tail is in V2's body but precedes V3's entire observed body.
    for tick in range(85, 90):
        voice_at(data[1], data[0], 79.+tick*.01, -24.)
    data[3][1] = [80.1, 80.6, False]
    return data


def check_v3(data, **kwargs):
    return soft.check_entry_v3(data[1], data[2], 80., data[0], data[3], data[4], **kwargs)


def saved_v3(tmp_path, data):
    item = checkpoint(fixture()); proof = check_v3(data); assert proof['accepted']
    item['observedWords'] = copy.deepcopy(data[3]); item['observedCoverage'] = copy.deepcopy(data[4])
    item['entryProof'] = {**proof, 'cacheKey': data[1]['cacheKey'], 'mixCacheKey': data[2]['cacheKey']}
    item['row']['fadeIn'] = proof['fadeInSeconds']
    from aidj.fixed_set_entry_review import fingerprint
    item['entryReviewInputHash'] = fingerprint(item, data[0], [])
    folder = tmp_path/'work'/'vocal-evidence'; folder.mkdir(parents=True)
    mixes = folder.parent/'mix-vad-probes'; mixes.mkdir()
    curated = tmp_path/'dj1-curation'; curated.mkdir()
    (curated/'owner-rejected.json').write_text('{}')
    (folder/(data[1]['cacheKey']+'.json')).write_text(json.dumps(data[1]))
    (mixes/(data[2]['cacheKey']+'.json')).write_text(json.dumps(data[2]))
    return item, folder


def test_previous_sound_tail_is_excluded_but_old_rise_context_and_future_word_remain():
    data = short_gap(); before = copy.deepcopy(data)
    v2 = soft.check_entry_v2(data[1], data[2], 80., data[0], data[3], data[4])
    v3 = check_v3(data)
    assert v2['reason'] == 'vocal-above-soft-ceiling'
    assert v3['accepted'] and v3['version'] == 3
    assert v3['parameters'] == {**soft.PARAMETERS_V2, 'beforeSeconds': .1}
    assert (v3['proofStart'], v3['proofEnd']) == (79.9, 80.05)
    assert v3['normalizedVocalMaxDbfs'] == pytest.approx(-34.)
    # The previous tail remains in the old [-150,-20]ms rise reference.
    assert v3['preVocalP95Dbfs'] == pytest.approx(-24.)
    assert v3['risingThresholdDbfs'] == pytest.approx(-16.)
    assert v3['nextObservedWord'] == [80.1, 80.6, False]
    assert v3['fadeInSeconds'] == .05
    assert v3['fadeInSeconds']+.03 <= v3['nextObservedWordDelaySeconds']
    assert data == before


def test_only_touching_new_body_mix_frames_are_scored_and_both_channels_still_veto():
    data = short_gap(); speech = data[2]['speech']
    old_only = next(i for i, (a,b) in enumerate(zip(speech['frameStartSeconds'],speech['frameEndSeconds']))
        if a < 79.9 and b <= 79.9 and b > 79.85)
    speech['rightSpeechProbability'][old_only] = .5; speech['maxSpeechProbability'][old_only] = .5
    assert check_v3(data)['accepted']
    touching = check_v3(data)['mixFrameFirst']
    speech['rightSpeechProbability'][touching] = .050001; speech['maxSpeechProbability'][touching] = .050001
    assert check_v3(data)['reason'] == 'raw-mix-speech-at-entry'
    speech['maxSpeechProbability'][touching] = .001
    assert check_v3(data)['reason'] == 'invalid-raw-stereo-mix-evidence'


@pytest.mark.parametrize('confident', [True, False])
def test_short_intra_word_syllable_gap_never_erases_known_whole_word(confident):
    data = short_gap(); data[3].append([79.5, 80.4, confident])
    assert check_v3(data)['reason'] == 'known-word-crosses-entry-guard'


@pytest.mark.parametrize('change', [
    lambda d:d[4].clear(),
    lambda d:d[3].__setitem__(1, [80.09, 81., False]),
    lambda d:voice_at(d[1], d[0], 80.08, -20.),
    lambda d:voice_at(d[1], d[0], 79.9, -29.9),
    lambda d:d[0].update(lufs=-25.),
    lambda d:d[2]['identity'].update(requestedEndSeconds=84.),
])
def test_short_body_does_not_relax_coverage_preroll_energy_source_or_canonical_guards(change):
    data = fixture(); change(data)
    assert not check_v3(data)['accepted']


def test_public_gate_replays_v3_and_fails_relabelling_or_geometry_tamper(tmp_path):
    data = short_gap(); item, folder = saved_v3(tmp_path, data)
    assert public_gate(item, data[0], evidence_folder=folder) is None
    for mutate in [lambda p:p.update(version=2), lambda p:p['parameters'].update(beforeSeconds=.15),
            lambda p:p.update(proofStart=79.85), lambda p:p.update(mixFrameFirst=p['mixFrameFirst']-1),
            lambda p:p['parameters'].update(risePreStartSeconds=-.1)]:
        bad = copy.deepcopy(item); mutate(bad['entryProof'])
        assert public_gate(bad, data[0], evidence_folder=folder)
    assert not check_v3(data, rejected=[80.2])['accepted']


@pytest.mark.parametrize('variant,expected', [
    ('default', '3484838562caeef5766415f2c5c211a7436958254f912ca38288e7ef7d99ced6'),
    ('no-future', 'b584682b4847e7bae46e5f82e6ae20c542f812caf6c8b6dfa828d1a6a3d9e049'),
    ('far-future', '6538801aaf5c812afcc78b797dab8fe546cdfac7a71856465d5490d67b598fd6'),
    ('rise', 'c25295ab0f07df8599ed235f5fc4b5ca575b4aed2072ca7a51e7083bc6593aeb'),
    ('duplicates', '1e82c9d61521da7f89bea7a4b26d3d933460d3bc56b319d4adc6622226366207'),
])
def test_v2_proofs_remain_byte_identical_to_pre_v3_commit(variant, expected):
    # Independently captured by running HEAD's pre-V3 source before this test.
    data = fixture()
    if variant == 'no-future': data[3][:] = [[0., 79.7, True]]
    if variant == 'far-future': data[3][1] = [95., 96., False]
    if variant == 'rise': voice_at(data[1], data[0], 80.15, -20.)
    if variant == 'duplicates': data[3].append([80.4, 80.8, True])
    result = soft.check_entry_v2(data[1], data[2], 80., data[0], data[3], data[4])
    assert hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == expected


def test_no_arbitrary_short_body_or_cap_parameter():
    data = fixture()
    for kwargs in [{'before_seconds':.01}, {'mix_cap':.1}, {'version':3.0}]:
        with pytest.raises(TypeError): check_v3(data, **kwargs)
    for version in [True, 3.0, 'v3', 4, None]:
        assert soft._policy(version) is None
