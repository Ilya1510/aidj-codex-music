import pytest

from aidj.fixed_set_blend import artist_keys, blend
from aidj.fixed_sets import signature


def fixture(artists):
    assets = {str(i): {'id': str(i), 'artist': a, 'md5': str(i)*32,
                      'duration': 150.} for i, a in enumerate(artists)}
    rows = [{'trackId': k, 'md5': a['md5'], 'start': 20., 'end': 100.,
             'fadeIn': .05, 'fadeOut': .3, 'transitionSeconds': 0.,
             'transitionKind': 'clean-cut', 'rate': 1.} for k, a in assets.items()]
    return {'id': 'dj1', 'playlistId': 1, 'revision': signature(rows), 'tracks': rows}, assets


def test_artist_limit_counts_collaborations_case_punctuation_and_real_upload_credit():
    value, assets = fixture(['Sofi Tukker, Gorgon City', 'Ultra Records', 'Ultra Records',
                             'Era Istrefi', 'David Guetta, Sam Martin',
                             'Kelly Rowland, David Guetta', 'DAVID GUETTA', 'Rihanna'])
    assets['1']['title'] = 'SOFI TUKKER - Awoo feat. Betta Lemme (Official Video)'
    assets['2']['title'] = 'Sofi Tukker - Good Time Girl'
    result = blend(value, assets)
    assert [r['trackId'] for r in result['tracks']] == ['0', '1', '3', '2', '4', '5', '7', '6']
    for a, b, c in zip(result['tracks'], result['tracks'][1:], result['tracks'][2:]):
        assert not (artist_keys(assets[a['trackId']]) & artist_keys(assets[b['trackId']]) &
                    artist_keys(assets[c['trackId']]))
    previous = {r['trackId']: r for r in value['tracks']}
    for r in result['tracks']:
        assert all(r[k] == previous[r['trackId']][k] for k in ('start', 'end', 'md5', 'fadeOut', 'rate'))
        assert r['fadeIn'] == 3.
    assert result['tracks'][-1]['transitionSeconds'] == 0.
    assert result['duration'] == 8*80 - 7*3
    assert value['tracks'][0]['fadeIn'] == .05


def test_no_safe_artist_does_not_silently_publish_a_third_repeat():
    value, assets = fixture(['A', 'a', 'A'])
    with pytest.raises(ValueError, match='ARTIST_LIMIT_IMPOSSIBLE'):
        blend(value, assets)
    with pytest.raises(ValueError, match='ARTIST_CREDIT_MISSING'):
        artist_keys({'artist': 'Ultra Records', 'title': 'Unattributed'})


def test_allowed_ranges_survive_and_longer_existing_intro_is_preserved():
    value, assets = fixture(['A', 'B'])
    value['tracks'][0]['fadeIn'] = 4.
    value['revision'] = signature(value['tracks'])
    result = blend(value, assets, {'0': (20., 100.)})
    assert result['tracks'][0]['fadeIn'] == 4.
    assert artist_keys({'artist': 't.A.T.u.'}) == artist_keys({'artist': 'TATU'})
