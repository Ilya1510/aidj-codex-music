"""Frozen-set source identity, allowed ranges, and genuinely empty word gaps."""
import copy
import sqlite3

import pytest

from aidj.fixed_set_prepare import occupied, points, source_rows
from aidj.fixed_sets import publish, records, signature, validate


def manifest():
    tracks = [{'trackId': 'a'*24, 'md5': '1'*32, 'start': 10, 'end': 72,
               'fadeIn': .08, 'fadeOut': .4, 'transitionSeconds': 0,
               'transitionKind': 'clean-cut', 'rate': 1}]
    return {'id': 'dj1', 'playlistId': 1, 'revision': signature(tracks), 'tracks': tracks}


def assets():
    return {'a'*24: {'md5': '1'*32, 'duration': 100}}


def database():
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE web_assets(id TEXT PRIMARY KEY,md5 TEXT,duration REAL,track_key TEXT);
        CREATE TABLE playback_ranges(track_id TEXT PRIMARY KEY,start REAL,end REAL);
        CREATE TABLE playlists(id INTEGER PRIMARY KEY);
        CREATE TABLE playlist_tracks(playlist_id INTEGER,track_key TEXT,state TEXT,position INTEGER);
        CREATE TABLE dj_fixed_sets(id TEXT PRIMARY KEY,payload TEXT);
    ''')
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?)', ('a'*24, '1'*32, 100, 'key-a'))
    db.execute('INSERT INTO playlists VALUES (1)')
    db.execute("INSERT INTO playlist_tracks VALUES (1,'key-a','complete',0)")
    db.commit()
    return db


def test_frozen_plan_keeps_exact_source_bounds_and_drops_untrusted_description():
    value = manifest(); value['description'] = 'Every transition manually listened to'
    clean = validate(value, assets(), {'a'*24: (5, 90)})
    assert clean['tracks'] == value['tracks']
    assert clean['duration'] == 62
    assert 'listened' not in clean['description']
    assert clean['revision'] == signature(clean['tracks'])


@pytest.mark.parametrize('field,value', [
    ('start', True), ('end', float('nan')), ('rate', float('inf')),
    ('fadeOut', -1), ('transitionSeconds', 1), ('md5', '2'*32),
])
def test_invalid_or_stale_source_data_cannot_be_served(field, value):
    plan = manifest(); plan['tracks'][0][field] = value
    with pytest.raises(ValueError): validate(plan, assets())


def test_new_manual_crop_invalidates_previously_published_set():
    with database() as db:
        publish(db, manifest())
        assert len(records(db)) == 1
        db.execute('INSERT INTO playback_ranges VALUES (?,?,?)', ('a'*24, 20, 90))
        assert records(db) == []


def test_new_source_md5_invalidates_previously_published_set():
    with database() as db:
        publish(db, manifest())
        db.execute('UPDATE web_assets SET md5=?', ('2'*32,))
        assert records(db) == []


def test_incomplete_plan_is_never_published_over_existing_complete_plan():
    with database() as db:
        publish(db, manifest())
        before = db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0]
        db.execute('INSERT INTO web_assets VALUES (?,?,?,?)', ('b'*24, '2'*32, 150, 'key-b'))
        db.execute("INSERT INTO playlist_tracks VALUES (1,'key-b','complete',1)"); db.commit()
        with pytest.raises(ValueError, match='FIXED_SET_INCOMPLETE'): publish(db, manifest())
        assert db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0] == before


def test_bad_or_absent_stored_manifest_does_not_break_catalog():
    with database() as db:
        assert records(db) == []
        db.execute("INSERT INTO dj_fixed_sets VALUES ('dj1',?)", ('{broken json',))
        assert records(db) == []


def test_changed_cut_requires_a_new_manifest_revision():
    value = manifest(); value['tracks'][0]['start'] += .5
    with pytest.raises(ValueError, match='FIXED_SET_REVISION_CHANGED'):
        validate(value, assets())
    value['revision'] = signature(value['tracks'])
    assert validate(value, assets())['tracks'][0]['start'] == 10.5


def test_aliases_of_identical_audio_use_the_same_first_playlist_identity():
    with database() as db:
        # Insert the alias second in SQLite but before the first source in the
        # playlist. Preparation and publication must choose by playlist order.
        db.execute('INSERT INTO web_assets VALUES (?,?,?,?)', ('b'*24, '1'*32, 100, 'key-b'))
        db.execute("INSERT INTO playlist_tracks VALUES (1,'key-b','complete',-1)"); db.commit()
        rows = list(source_rows(db, 1))
        assert len(rows) == 1 and rows[0]['id'] == 'b'*24
        wrong = manifest()
        with pytest.raises(ValueError, match='FIXED_SET_INCOMPLETE'): publish(db, wrong)
        correct = manifest(); correct['tracks'][0]['trackId'] = 'b'*24
        correct['revision'] = signature(correct['tracks'])
        assert publish(db, correct)['tracks'][0]['trackId'] == rows[0]['id']
        assert len(records(db)) == 1


def test_overlapping_recognitions_never_manufacture_a_word_gap():
    song = {'allowed_start': 0, 'allowed_end': 20, 'duration': 20}
    # The short recognition at 2..3 is contained within a sung 1..10 span.
    # A naive adjacent-pair scan calls 3..5 empty even though that word continues.
    words = [[1, 10, True], [2, 3, True], [5, 6, True], [12, 13, True]]
    original = copy.deepcopy(words)
    entries, exits, _ = points(song, {}, {'words': words})
    assert any(origin == 'word-gap' for _, origin, *_ in entries)
    for at, origin, *_ in entries:
        if origin == 'word-gap':
            assert occupied(words, at-.025, at+.025) == 0
    for at, origin, _, fade in exits:
        if origin == 'word-gap':
            assert occupied(words, at-fade, at) == 0
    assert words == original


def test_owner_rejections_apply_to_both_entry_and_exit_candidates():
    song = {'allowed_start': 0, 'allowed_end': 20, 'duration': 20}
    graph = {'anchors': {'entries': [[5, .5]], 'exits': [[5, .5, .5]]}}
    entries, exits, _ = points(song, graph, {'words': []}, human=[5], rejected=[5])
    assert all(abs(row[0]-5) >= .3 for row in entries+exits)
