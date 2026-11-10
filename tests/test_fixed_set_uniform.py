import copy
import math

import pytest

from aidj.fixed_set_blend import artist_keys
from aidj.fixed_set_uniform import uniform_program
from aidj.fixed_sets import signature


def fixture(bpms, artists=None, *, end=70.):
    artists = artists or ['Artist '+str(i) for i in range(len(bpms))]
    assets, observations, rows = {}, {}, []
    for i, (bpm, artist) in enumerate(zip(bpms, artists)):
        aid = f'{i+1:024x}'; md5 = f'{i+1:032x}'
        assets[aid] = {'id': aid, 'md5': md5, 'duration': 100., 'artist': artist, 'title': 'Song '+str(i)}
        row = {'trackId': aid, 'md5': md5, 'start': 10.+i*.11, 'end': end,
               'fadeIn': .1, 'fadeOut': .8, 'rate': 1., 'transitionSeconds': 0.,
               'transitionKind': 'clean-cut'}
        rows.append(row)
        if bpm:
            period = 60/bpm
            beats = [.12+i*.07+j*period for j in range(400)]
            observation = {'md5': md5, 'sourceOffsetSeconds': 0., 'sourceEndSeconds': 100.,
                           'beats': [b for b in beats if 0 <= b <= 100.],
                           'estimatedBpm': bpm}
            observations[aid] = {'entry': copy.deepcopy(observation), 'exit': copy.deepcopy(observation)}
    value = {'id': 'dj1', 'playlistId': 1, 'revision': signature(rows), 'tracks': rows}
    return value, assets, observations


def assert_uniform(result, original):
    before = {r['trackId']: r for r in original['tracks']}
    rows = result['plan']['tracks']
    assert {r['trackId'] for r in rows} == set(before)
    for row in rows:
        assert all(row[k] == before[row['trackId']][k] for k in ('md5', 'start', 'end', 'fadeOut'))
        assert row['fadeIn'] == 5. and .97 <= row['rate'] <= 1.03
    assert all(r['transitionKind'] == 'crossfade' and 4.75 <= r['transitionSeconds'] <= 5.25 for r in rows[:-1])
    assert rows[-1]['transitionSeconds'] == 0 and rows[-1]['transitionKind'] == 'clean-cut'
    assert result['summary']['shortFallbackTransitions'] == 0


def test_same_style_for_unknown_irregular_and_incompatible_music_without_fake_confirmation():
    original, assets, obs = fixture([80., 160., None])
    untouched = copy.deepcopy(original)
    result = uniform_program(original, assets, obs)
    assert_uniform(result, original)
    assert all(not p['beatMatched'] and p['confidence'] == 'best-effort' for p in result['pairs'])
    assert original == untouched


def test_confirmed_phase_uses_final_constant_rates_on_both_sides_of_a_track():
    original, assets, obs = fixture([120., 122., 124.])
    result = uniform_program(original, assets, obs)
    assert_uniform(result, original)
    rows = {r['trackId']: r for r in result['plan']['tracks']}
    confirmed = [p for p in result['pairs'] if p['beatMatched']]
    assert len(confirmed) == 2
    for proof in confirmed:
        a, b = rows[proof['leftTrackId']], rows[proof['rightTrackId']]
        assert a['rate'] == proof['leftRate'] and b['rate'] == proof['rightRate']
        left_wall = (proof['leftAnchorSeconds']-proof['leftOverlapStartSeconds'])/a['rate']
        right_wall = (proof['rightAnchorSeconds']-b['start'])/b['rate']
        assert abs(left_wall-right_wall) < 1e-10
        assert proof['maxPhaseErrorSeconds'] < .05


def test_artist_guard_does_not_leave_an_impossible_last_block_and_keeps_opening():
    original, assets, obs = fixture([120.]*8, artists=['A','B','A','A','A','C','A','B'])
    opening = original['tracks'][0]['trackId']
    result = uniform_program(original, assets, obs, opening_id=opening)
    assert_uniform(result, original)
    rows = result['plan']['tracks']
    assert rows[0]['trackId'] == opening
    keys = [artist_keys(assets[r['trackId']]) for r in rows]
    assert not any(a & b & c for a, b, c in zip(keys, keys[1:], keys[2:]))
    impossible, bad_assets, bad_obs = fixture([120.]*6, artists=['A']*5+['B'])
    with pytest.raises(ValueError, match='ARTIST_LIMIT_IMPOSSIBLE'):
        uniform_program(impossible, bad_assets, bad_obs)


def test_stale_md5_and_missing_native_observations_do_not_become_verified_grids():
    original, assets, obs = fixture([120., 120.])
    obs[original['tracks'][1]['trackId']]['entry']['md5'] = 'stale'
    result = uniform_program(original, assets, obs)
    assert_uniform(result, original)
    assert not result['pairs'][0]['beatMatched']
    assert result['pairs'][0]['reason'] == 'missing-or-unstable-local-rhythm'
    no_observations = uniform_program(original, assets, {})
    assert_uniform(no_observations, original)
    assert no_observations['pairs'][0]['phaseAdjustmentKind'] == 'fixed-five-second-envelope'


def test_source_eof_is_not_claimed_sample_aligned_from_metadata_clock():
    original, assets, obs = fixture([120., 120.], end=100.)
    result = uniform_program(original, assets, obs)
    assert_uniform(result, original)
    assert not result['pairs'][0]['beatMatched']
    assert result['pairs'][0]['reason'] == 'decoder-eof-clock-unverified'


def test_unknown_bpm_can_use_real_onset_anchor_but_only_as_approximation():
    original, assets, obs = fixture([120., 120.])
    # Keep measured onsets in the actual overlap but remove enough observations
    # that a six-second local stable-pulse certificate cannot be constructed.
    for aid, value in obs.items():
        for role, observation in value.items():
            observation['beats'] = [b for b in observation['beats'] if (64 <= b <= 70. if role == 'exit' else 10 <= b <= 16.)]
    result = uniform_program(original, assets, obs)
    assert_uniform(result, original)
    proof = result['pairs'][0]
    assert not proof['beatMatched'] and proof['phaseAdjustmentKind'] == 'real-observed-onset-approximation'
    approximation = proof['approximation']
    assert approximation['leftObservedAnchorSeconds'] in obs[proof['leftTrackId']]['exit']['beats']
    assert approximation['rightObservedAnchorSeconds'] in obs[proof['rightTrackId']]['entry']['beats']


def test_current_ranges_still_limit_cuts_and_short_sources_do_not_get_hidden_short_mode():
    original, assets, obs = fixture([120., 121.])
    aid = original['tracks'][0]['trackId']
    with pytest.raises(ValueError, match='FIXED_SET_BOUNDS'):
        uniform_program(original, assets, obs, ranges={aid: (11., 100.)})
    original['tracks'][0]['end'] = original['tracks'][0]['start']+9.
    original['revision'] = signature(original['tracks'])
    with pytest.raises(ValueError, match='UNIFORM_EXCERPT_TOO_SHORT'):
        uniform_program(original, assets, obs)


def test_ordering_preserves_every_source_and_nearby_harmony_can_break_tempo_ties():
    original, assets, obs = fixture([120.]*4)
    ids = [r['trackId'] for r in original['tracks']]
    features = {aid: {'md5': assets[aid]['md5'], 'chroma': [1.]+[0.]*11,
                     'group': 'dance'} for aid in ids}
    features[ids[1]]['chroma'] = [0.,1.]+[0.]*10
    result = uniform_program(original, assets, obs, features=features)
    assert_uniform(result, original)
    assert result['plan']['tracks'][0]['trackId'] == ids[0]
    assert result['plan']['tracks'][1]['trackId'] != ids[1]


@pytest.mark.parametrize('patch', [{'target_seconds':1.2}, {'tolerance_seconds':.5}, {'lookahead':0}])
def test_invalid_uniform_policy_rejected(patch):
    original, assets, obs = fixture([120.,120.])
    with pytest.raises(ValueError, match='UNIFORM_PARAMETERS'):
        uniform_program(original, assets, obs, **patch)


def test_rates_optimize_actual_entry_exit_pairs_instead_of_representative_band_seams():
    from aidj.fixed_set_uniform import _rates
    rows = [{'trackId': str(i)} for i in range(4)]
    # Variable-tempo passages can have quite different entry and exit speeds.
    tempos = {str(i): {'entry': (a, 'local'), 'exit': (b, 'local')}
              for i, (a, b) in enumerate([(110., 113.306), (113.334, 145.),
                                         (146., 151.597), (153.928, 170.)])}
    audit = _rates(rows, tempos)[0]
    assert audit['finalRateSquaredError'] < audit['unitRateSquaredError']
    assert all(.97 <= row['rate'] <= 1.03 for row in rows)
    for i in (0, 2):
        before = abs(tempos[str(i)]['exit'][0]-tempos[str(i+1)]['entry'][0])
        after = abs(tempos[str(i)]['exit'][0]*rows[i]['rate']-
                    tempos[str(i+1)]['entry'][0]*rows[i+1]['rate'])
        assert after < before
