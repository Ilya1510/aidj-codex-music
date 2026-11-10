"""Lower-confidence incoming proof must retain words and both independent models."""
import copy
import hashlib
import json
import math

import pytest

from aidj import fixed_set_soft_entry as soft
from aidj.fixed_set_entry_review import VERSION as ENTRY_VERSION, fingerprint, selection_error as public_gate
from aidj.fixed_set_instrumental_entry import digest, mix_identity
from aidj.fixed_set_vocal_review import VERSION as UVR_VERSION, gain_db
from aidj.vocal_probe import MODEL_SHA256
from aidj.vocal_speech import INPUT_RATE, MODEL_RATE, WINDOW


def fixture():
    song = {'id': 'a'*24, 'md5': '1'*32, 'bytes': 1000, 'duration': 180.,
        'lo': 0., 'hi': 180., 'lufs': -16., 'peak': -2., 'manualStart': False,
        'loudnessMethod': 'ebu-r128-v1', 'loudnessStatus': 'complete'}
    a, b = 70., 90.
    voice = 10**((-34-gain_db(song))/20)
    key = hashlib.sha256(json.dumps([UVR_VERSION, song['md5'], a, b, MODEL_SHA256],
        separators=(',', ':')).encode()).hexdigest()
    vocal = {'cacheKey': key, 'md5': song['md5'], 'sourceOffsetSeconds': a,
        'requestedEndSeconds': b, 'durationSeconds': 20., 'frameHopSeconds': .01,
        'modelSha256': MODEL_SHA256, 'warnings': [], 'vocalRms': [voice]*2000,
        'mixRms': [.3]*2000, 'instrumentalRms': [.3]*2000,
        'vocalToMixDb': [20*math.log10(voice/.3)]*2000}
    words = [[0., 79.7, True], [80.4, 81., False], [141.5, 180., True]]
    coverage = [[0., 180.]]
    return song, vocal, mix_fixture(song, 77., 83.), words, coverage


def mix_fixture(song, a, b):
    n = round((b-a)*INPUT_RATE)
    real = math.ceil(n*160/441)
    padded = math.ceil((real+64)/WINDOW)*WINDOW
    frames = math.ceil(n*160/(441*WINDOW))
    starts = [a+i*WINDOW/MODEL_RATE for i in range(frames)]
    ends = [min(a+(i+1)*WINDOW/MODEL_RATE, b) for i in range(frames)]
    last = ends[-1]-starts[-1]
    partial = last < WINDOW/MODEL_RATE-1e-12
    identity = mix_identity(song, a, b)
    mix = {'version': 1, 'cacheKey': digest(identity), 'identity': identity,
        'sourceMD5': song['md5'], 'sourceOriginalMD5Verified': True,
        'sourceDuration': song['duration'], 'sourceRange': [song['lo'], song['hi']],
        'playbackGainDb': gain_db(song),
        'playbackProfile': {k: song[k] for k in ('lufs', 'peak', 'loudnessMethod', 'loudnessStatus')},
        'normalizedInputLayout': 'channel-major-float32-little',
        'normalizedInputSHA256': 'c'*64, 'uvrInferred': False,
        'stemInput': False, 'channelsAveraged': False, 'audioRetained': False,
        'decode': {'method': 'ffmpeg-xerror-20s-stereo-f32le', 'pcmFrames': n,
            'pcmBytes': n*8, 'sampleRate': INPUT_RATE, 'channels': 2,
            'sourceOffsetSeconds': a, 'actualEndSeconds': b, 'pcmSHA256': 'd'*64},
        'speech': {'method': 'silero-v5-stereo-speech-probability', 'version': 1,
            'modelArtifactsVerified': True, 'modelSHA256': identity['modelSHA256'],
            'modelWrapperVersion': 'faster-whisper-1.1.1', 'sourceOffsetSeconds': a,
            'sourceEndSeconds': b, 'durationSeconds': b-a, 'inputSampleRate': INPUT_RATE,
            'inputSamples': n, 'modelSampleRate': MODEL_RATE, 'resampledSamples': real,
            'modelPaddedSamples': padded, 'frameHopSeconds': WINDOW/MODEL_RATE,
            'paddingFramesExcluded': padded//WINDOW-frames,
            'rawProbabilityShape': [2, padded//WINDOW],
            'frameStartSeconds': starts, 'frameEndSeconds': ends,
            'leftSpeechProbability': [.001]*frames, 'rightSpeechProbability': [.001]*frames,
            'maxSpeechProbability': [.001]*frames, 'lastFrameSeconds': last,
            'lastFramePartial': partial, 'lastFrameIncludesPadding': partial}}
    return mix


def check(data, point=80., **kwargs):
    song, vocal, mix, words, coverage = data
    return soft.check_entry(vocal, mix, point, song, words, coverage, **kwargs)


def voice_at(vocal, song, time, dbfs):
    index = round((time-vocal['sourceOffsetSeconds'])/.01)
    amplitude = 10**((dbfs-gain_db(song))/20)
    vocal['vocalRms'][index] = amplitude
    vocal['vocalToMixDb'][index] = 20*math.log10(amplitude/vocal['mixRms'][index])


def checkpoint(data):
    song, vocal, mix, words, coverage = data
    proof = check(data)
    assert proof['accepted']
    row = {'trackId': song['id'], 'md5': song['md5'], 'start': 80., 'end': 140.,
        'fadeIn': proof['fadeInSeconds'], 'fadeOut': .8, 'rate': 1,
        'transitionKind': 'clean-cut', 'transitionSeconds': 0}
    item = {'trackId': song['id'], 'md5': song['md5'], 'row': row,
        'status': 'ready', 'entryReviewOnly': False, 'entryReviewStatus': 'ready',
        'entryReviewVersion': ENTRY_VERSION, 'observedWords': copy.deepcopy(words),
        'observedCoverage': copy.deepcopy(coverage),
        'entryProof': {**proof, 'cacheKey': vocal['cacheKey'], 'mixCacheKey': mix['cacheKey']}}
    item['entryReviewInputHash'] = fingerprint(item, song, [])
    return item


def test_distinct_soft_tier_is_approximate_and_keeps_low_confidence_words():
    data = fixture()
    before = copy.deepcopy(data)
    proof = check(data)
    assert proof['accepted'] and proof['normalizedVocalP95Dbfs'] == pytest.approx(-34.)
    assert proof['normalizedVocalP95Dbfs'] > -43  # It is not the original quiet tier.
    assert proof['fadeInSeconds'] == .05 and proof['nextObservedWord'] == [80.4, 81., False]
    assert proof['nextObservedWordConfident'] is False and proof['ASRWordsErased'] is False
    assert proof['speechModelCanMissSinging'] is True and proof['asrOverrideApplied'] is False
    assert all(proof[k] is False for k in ('verifiedWordBoundary', 'naturalSilenceClaimed', 'voiceAbsenceProven'))
    assert proof['prerollEvidenceKind'] == 'no-relative-voice-rise-observed'
    assert data == before


@pytest.mark.parametrize('confident', [True, False])
def test_every_known_whole_word_is_a_veto_even_if_it_contains_a_quiet_internal_syllable(confident):
    data = fixture()
    data[3].append([79.9, 80.3, confident])
    assert check(data)['reason'] == 'known-word-crosses-entry-guard'


def test_word_90ms_ahead_cannot_satisfy_the_100ms_preroll():
    data = fixture()
    data[3][1] = [80.09, 81., False]
    assert check(data)['reason'] == 'observed-word-preroll-under100ms'
    data[3][1] = [80.1, 81., False]
    assert check(data)['accepted']


def test_observed_early_voice_frame_is_a_veto_despite_a_quiet_200ms_interval():
    data = fixture()
    voice_at(data[1], data[0], 80.08, -20.)
    assert check(data)['reason'] == 'observed-voice-preroll-under100ms'
    data = fixture()
    voice_at(data[1], data[0], 80.15, -20.)
    proof = check(data)
    assert proof['accepted'] and proof['risingVoiceDelaySeconds'] == pytest.approx(.15)
    assert proof['prerollEvidenceKind'] == 'observed-relative-voice-rise'
    assert proof['fadeInSeconds']+.03 <= proof['risingVoiceDelaySeconds']


def test_no_or_far_future_word_is_retained_as_unknown_or_far_instead_of_invented():
    data = fixture()
    data[3][:] = [[0., 79.7, True]]
    proof = check(data)
    assert proof['accepted'] and proof['nextObservedWord'] is None
    assert proof['nextObservedWordConfident'] is None and proof['nextObservedWordDelaySeconds'] is None
    data[3].append([95., 96., False])
    proof = check(data)
    assert proof['accepted'] and proof['nextObservedWordDelaySeconds'] == 15.
    assert not proof['nextObservedWordConfident']


def test_duplicate_future_spans_do_not_turn_a_false_confidence_into_certainty():
    data = fixture()
    data[3].append([80.4, 80.8, True])
    proof = check(data)
    assert proof['accepted'] and proof['nextObservedWordConfident'] is False
    assert len(proof['nextObservedWordsAtFirstStart']) == 2


def test_one_loud_vocal_frame_or_high_p95_cannot_be_hidden_by_the_other_model():
    data = fixture()
    voice_at(data[1], data[0], 80., -29.9)
    assert check(data)['reason'] == 'vocal-above-soft-ceiling'
    data = fixture()
    for n in range(-15, 5):
        voice_at(data[1], data[0], 80.+n*.01, -32.9)
    assert check(data)['reason'] == 'vocal-above-soft-ceiling'


def test_either_real_mix_channel_vetoes_and_a_forged_maximum_cannot_hide_it():
    data = fixture()
    speech = data[2]['speech']
    index = next(i for i, (a, b) in enumerate(zip(speech['frameStartSeconds'], speech['frameEndSeconds']))
                 if a < 80.05 and b > 79.85)
    speech['rightSpeechProbability'][index] = .01001
    speech['maxSpeechProbability'][index] = .01001
    assert check(data)['reason'] == 'raw-mix-speech-at-entry'
    speech['maxSpeechProbability'][index] = .001
    assert check(data)['reason'] == 'invalid-raw-stereo-mix-evidence'


def test_favorable_overlapping_context_cannot_replace_the_single_calibration_context():
    data = fixture()
    assert check(data)['accepted']
    assert soft.centered_mix_bounds(80., 180.) == (77., 83.)
    alternate = mix_fixture(data[0], 70., 90.)
    alternate['speech']['leftSpeechProbability'] = [0.]*len(alternate['speech']['leftSpeechProbability'])
    alternate['speech']['rightSpeechProbability'] = [0.]*len(alternate['speech']['rightSpeechProbability'])
    alternate['speech']['maxSpeechProbability'] = [0.]*len(alternate['speech']['maxSpeechProbability'])
    changed = (data[0], data[1], alternate, data[3], data[4])
    assert check(changed)['reason'] == 'mix-window-not-canonical-centered6s'
    shifted = mix_fixture(data[0], 76.99, 82.99)
    changed = (data[0], data[1], shifted, data[3], data[4])
    assert check(changed)['reason'] == 'mix-window-not-canonical-centered6s'


@pytest.mark.parametrize('point,duration,expected', [
    (1., 180., (0., 6.)), (179., 180., (174., 180.)), (2., 4., (0., 4.)),
])
def test_centered_context_shifts_only_at_real_source_edges(point, duration, expected):
    assert soft.centered_mix_bounds(point, duration) == expected


def test_unknown_coverage_model_edge_warmup_and_end_guard_fail_closed():
    data = fixture()
    data[4][:] = [[79.8, 180.]]
    assert check(data)['reason'] == 'ASR-coverage-unknown'
    data = fixture()
    data[3][:] = []
    assert check(data, point=70.4)['reason'] == 'incomplete-vocal-context'
    assert check(data, point=70.9)['reason'] == 'mix-window-not-canonical-centered6s'
    assert check(data, point=89.71)['reason'] == 'incomplete-vocal-context'
    song, vocal, _, words, coverage = data
    vocal.update(sourceOffsetSeconds=0., requestedEndSeconds=20.)
    vocal['cacheKey'] = hashlib.sha256(json.dumps(
        [UVR_VERSION, song['md5'], 0., 20., MODEL_SHA256], separators=(',', ':')).encode()).hexdigest()
    early = (song, vocal, mix_fixture(song, 0., 6.), words, coverage)
    assert check(early, point=.9)['reason'] == 'mix-warmup-or-end-edge'


@pytest.mark.parametrize('change', [{'md5': '2'*32}, {'bytes': 1001}, {'duration': 181.},
    {'lo': 1.}, {'hi': 170.}, {'lufs': -25.}, {'peak': -1.}, {'loudnessStatus': 'pending'}])
def test_current_source_range_and_playback_profile_cannot_reuse_stale_mix(change):
    data = fixture()
    data = ({**data[0], **change}, *data[1:])
    assert not check(data)['accepted']


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(stemInput=True),
    lambda d: d.update(channelsAveraged=True),
    lambda d: d['speech'].update(modelArtifactsVerified=False),
    lambda d: d['speech']['modelSHA256'].update(silero_encoder_v5='wrong'),
    lambda d: d['speech']['frameStartSeconds'].__setitem__(2, 90.),
    lambda d: d['speech']['leftSpeechProbability'].__setitem__(2, float('nan')),
    lambda d: d.update(normalizedInputSHA256='missing'),
    lambda d: d['speech'].update(paddingFramesExcluded=17),
    lambda d: d['speech'].update(lastFrameIncludesPadding=not d['speech']['lastFrameIncludesPadding']),
    lambda d: d.update(identity=[]),
    lambda d: d.update(decode=[]),
    lambda d: d.update(speech=[]),
])
def test_wrong_model_channels_frame_geometry_input_hash_or_padding_is_not_a_raw_mix_proof(mutate):
    data = fixture()
    mutate(data[2])
    assert check(data)['reason'] == 'invalid-raw-stereo-mix-evidence'


def test_vocal_cache_key_raw_ratio_or_warning_must_be_valid():
    data = fixture()
    data[1]['cacheKey'] = '2'*64
    assert check(data)['reason'] == 'invalid-raw-vocal-evidence'
    data = fixture()
    data[1]['vocalToMixDb'][1000] += .1
    assert check(data)['reason'] == 'invalid-raw-vocal-evidence'
    data = fixture()
    data[1]['warnings'] = ['near-zero-vocal-output']
    assert check(data)['reason'] == 'invalid-raw-vocal-evidence'


def test_owner_negative_radius_is_enforced_without_erasing_observed_words():
    data = fixture()
    assert check(data, rejected=[80.2])['reason'] == 'owner-rejected-entry'
    assert check(data, rejected=[80.36])['accepted']


def test_public_gate_reloads_both_actual_caches_current_owner_map_and_all_proof_fields(tmp_path):
    data = fixture()
    song, vocal, mix, _, _ = data
    item = checkpoint(data)
    work = tmp_path/'research'/'quiet'
    folder = work/'vocal-evidence'
    mixes = work/'mix-vad-probes'
    curation = work.parent/'dj1-curation'
    for p in (folder, mixes, curation):
        p.mkdir(parents=True, exist_ok=True)
    rawfile = folder/(vocal['cacheKey']+'.json')
    mixfile = mixes/(mix['cacheKey']+'.json')
    rawfile.write_text(json.dumps(vocal))
    mixfile.write_text(json.dumps(mix))
    rejected = curation/'owner-rejected.json'
    rejected.write_text('{}')
    assert public_gate(item, song, evidence_folder=folder) is None
    changed = copy.deepcopy(item)
    changed['entryProof']['voiceAbsenceProven'] = True
    assert public_gate(changed, song, evidence_folder=folder) == 'soft-entry-proof-mismatch'
    changed = copy.deepcopy(item)
    changed['observedWords'].append([79.9, 80.2, False])
    changed['entryReviewInputHash'] = fingerprint(changed, song, [])
    assert public_gate(changed, song, evidence_folder=folder) == 'soft-entry-known-word-crosses-entry-guard'
    rejected.write_text(json.dumps({song['id']: [80.2]}))
    assert public_gate(item, song, evidence_folder=folder) == 'soft-entry-owner-rejected-entry'
    rejected.write_text('{}')
    voice_at(vocal, song, 80., -29.9)
    rawfile.write_text(json.dumps(vocal))
    assert public_gate(item, song, evidence_folder=folder) == 'soft-entry-vocal-above-soft-ceiling'


def test_final_source_signature_word_observations_fade_and_span_are_not_just_saved_flags(tmp_path):
    data = fixture()
    song, vocal, mix, _, _ = data
    item = checkpoint(data)
    folder = tmp_path/'vocal-evidence'
    folder.mkdir()
    mixes = tmp_path/'mix-vad-probes'
    mixes.mkdir()
    (folder/(vocal['cacheKey']+'.json')).write_text(json.dumps(vocal))
    (mixes/(mix['cacheKey']+'.json')).write_text(json.dumps(mix))
    assert soft.selection_error(item, song, evidence_folder=folder, rejected=[]) is None
    for mutate in (
        lambda d: d['entryProof'].update(sourceSignature='stale'),
        lambda d: d['entryProof'].update(observationsHash='stale'),
        lambda d: d['entryProof'].update(mixNormalizedInputSHA256='e'*64),
    ):
        changed = copy.deepcopy(item)
        mutate(changed)
        assert soft.selection_error(changed, song, evidence_folder=folder, rejected=[]) == 'soft-entry-proof-mismatch'
    changed = copy.deepcopy(item)
    changed['row']['end'] = 181.
    changed['entryReviewInputHash'] = fingerprint(changed, song, [])
    assert soft.selection_error(changed, song, evidence_folder=folder, rejected=[]) == 'soft-entry-row'
