import copy
import json
import threading

import pytest

from aidj.config import atomic_json
from aidj.fixed_set_auto import (CONFIG, STATUS, VERSION, cache_path, get, put, snapshot,
                                 process_next, update, insert)
from aidj.fixed_sets import publish, signature
from aidj.dj_remote import RemoteError
from tests.test_playlists import env


def song(db, i, *, ready=True, md5=None, position=None):
    aid = f'{i:024x}';md5 = md5 or f'{i:032x}'
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',
               (aid, 'auto:'+str(i), 'Song '+str(i), 'Artist '+str(i), 120., md5, 100, '/song.mp3'))
    db.execute("INSERT INTO playlist_tracks VALUES (1,?,?,?, ?,100,120.,?,'complete')",
               ('auto:'+str(i), position if position is not None else i, '/song.mp3', md5, 'Song'))
    if ready:
        db.execute('INSERT OR REPLACE INTO dj_evidence VALUES (?,?,0)', (md5, json.dumps({'fullComplete': True})))
        db.execute("INSERT OR REPLACE INTO audio_loudness(md5,method,status,updated) VALUES (?,'ebu-r128-v1','complete',0)", (md5,))
    db.commit();return aid


def prepared(song, row=None):
    row = row or {'trackId': song['id'], 'md5': song['md5'], 'start': 10., 'end': 72.,
                  'fadeIn': 5., 'fadeOut': .4, 'rate': 1., 'transitionSeconds': 0., 'transitionKind': 'clean-cut'}
    obs = {'md5': song['md5'], 'sourceOffsetSeconds': 0., 'sourceEndSeconds': 120.,
           'estimatedBpm': 120., 'beats': [i*.5 for i in range(241)]}
    return {'version': VERSION, 'md5': song['md5'], 'bytes': song['bytes'], 'duration': song['duration'],
            'row': row, 'observations': {'entry': obs, 'exit': copy.deepcopy(obs)}, 'features': {'bpm': 120.}}


def initialized(env):
    settings, db, *_ = env
    db.execute("INSERT INTO playlists VALUES (1,'Volleyball','volleyball','/Volleyball',0)");db.commit()
    song(db, 1);song(db, 2)
    snap = snapshot(db, 1)
    rows = []
    for s in snap['songs']:
        value = prepared(s);atomic_json(cache_path(settings, s), value);rows.append(value['row'])
    plan = publish(db, {'id': 'dj1', 'playlistId': 1, 'tracks': rows, 'revision': signature(rows)})
    put(db, CONFIG, {'enabled': True, 'version': VERSION, 'playlistId': 1})
    put(db, STATUS, {'state': 'complete', 'inputHash': snapshot(db, 1)['inputHash']});db.commit()
    return settings, db, plan


def test_new_song_is_prepared_once_inserted_and_frozen_old_plan_is_unchanged(env):
    settings, db, original = initialized(env);old = copy.deepcopy(original)
    new_id = song(db, 3);calls = []
    def prepare(settings, db, s, *args):calls.append(s['id']);return prepared(s)
    update(settings, db, snapshot(db, 1)['inputHash'], 'decoder', prepare=prepare)
    live = json.loads(db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0])
    assert calls == [new_id] and len(live['tracks']) == 3
    assert original == old and live['revision'] != old['revision']
    before = {r['trackId']: r for r in old['tracks']}
    assert [r['trackId'] for r in live['tracks'] if r['trackId'] in before] == list(before)
    assert all(all(r[k] == before[r['trackId']][k] for k in ('start', 'end', 'fadeOut', 'md5'))
               for r in live['tracks'] if r['trackId'] in before)
    assert all(4.75 <= r['transitionSeconds'] <= 5.25 for r in live['tracks'][:-1])
    assert not process_next(settings, db, 'python', 'decoder', threading.Event(), runner=lambda *a, **k: pytest.fail('unchanged'))


def test_added_song_waits_for_full_analysis_and_loudness(env):
    settings, db, original = initialized(env);aid = song(db, 3, ready=False)
    assert not process_next(settings, db, 'python', 'decoder', threading.Event(), runner=lambda *a, **k: pytest.fail('not ready'))
    assert get(db, STATUS)['state'] == 'waiting'
    assert json.loads(db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0]) == original
    assert aid not in [r['trackId'] for r in original['tracks']]


@pytest.mark.parametrize('change', ['membership', 'md5', 'crop', 'vote', 'disable'])
def test_inputs_changed_during_preparation_cannot_publish_stale_plan(env, change):
    settings, db, original = initialized(env);song(db, 3)
    expected = snapshot(db, 1)['inputHash']
    def prepare(settings, db, s, *args):
        if change == 'membership':song(db, 4)
        elif change == 'md5':db.execute("UPDATE web_assets SET md5=? WHERE id=?", ('f'*32, s['id']))
        elif change == 'crop':db.execute('INSERT INTO playback_ranges VALUES (?,30,90,0)', (s['id'],))
        elif change == 'vote':
            db.execute("INSERT INTO dj_boundary_points VALUES ('bad',?,10000,'[\"entry\"]','owner','reviewed',0,0)", (s['md5'],))
            db.execute("INSERT INTO dj_boundary_votes VALUES ('bad',1,1,-1,0)")
        else:put(db, CONFIG, {'enabled': False, 'playlistId': 1})
        db.commit();return prepared(s)
    with pytest.raises(ValueError, match='AUTO_INPUT_CHANGED'):
        update(settings, db, expected, 'decoder', prepare=prepare)
    assert json.loads(db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0]) == original


def test_publication_and_status_are_in_one_transaction(env, monkeypatch):
    import aidj.fixed_set_auto as auto
    settings, db, original = initialized(env);song(db, 3)
    real_put = auto.put
    def fail_status(db, key, value):
        if key == STATUS:raise RuntimeError('disk/status error')
        return real_put(db, key, value)
    monkeypatch.setattr(auto, 'put', fail_status)
    with pytest.raises(RuntimeError):update(settings, db, snapshot(db, 1)['inputHash'], 'decoder', prepare=lambda a,b,s,*args:prepared(s))
    assert json.loads(db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0]) == original


def test_source_alias_is_not_added_twice_and_removed_members_leave_next_version(env):
    settings, db, original = initialized(env)
    song(db, 3, md5=original['tracks'][0]['md5'])
    db.execute("DELETE FROM playlist_tracks WHERE track_key='auto:2'");db.commit()
    update(settings, db, snapshot(db, 1)['inputHash'], 'decoder', prepare=lambda *a:pytest.fail('cached'))
    live = json.loads(db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0])
    assert [r['trackId'] for r in live['tracks']] == [original['tracks'][0]['trackId']]
    assert live['tracks'][0]['transitionSeconds'] == 0


def test_empty_playlist_removes_set_and_next_addition_recreates_it(env):
    settings, db, _ = initialized(env)
    db.execute('DELETE FROM playlist_tracks');db.commit()
    update(settings, db, snapshot(db, 1)['inputHash'], 'decoder')
    assert not db.execute('SELECT 1 FROM dj_fixed_sets').fetchone()
    song(db, 3)
    update(settings, db, snapshot(db, 1)['inputHash'], 'decoder', prepare=lambda a,b,s,*args:prepared(s))
    assert len(json.loads(db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0])['tracks']) == 1


def test_failed_fingerprint_is_not_retried_and_stopped_job_can_resume(env, monkeypatch):
    import aidj.fixed_set_auto as auto
    settings, db, original = initialized(env);song(db, 3)
    monkeypatch.setattr(auto.shutil, 'disk_usage', lambda p:type('Space', (), {'free': 2*1024**3})())
    calls = []
    def fail(*args, **kw):calls.append(1);raise RemoteError('RESOURCE_LIMIT')
    assert process_next(settings, db, 'python', 'decoder', threading.Event(), runner=fail)
    assert get(db, STATUS)['state'] == 'failed'
    assert not process_next(settings, db, 'python', 'decoder', threading.Event(), runner=fail)
    assert len(calls) == 1
    put(db, STATUS, {'state': 'queued'});db.commit()
    def stop(*args, **kw):raise RemoteError('INTERRUPTED')
    assert process_next(settings, db, 'python', 'decoder', threading.Event(), runner=stop)
    assert get(db, STATUS)['state'] == 'queued'
    assert json.loads(db.execute('SELECT payload FROM dj_fixed_sets').fetchone()[0]) == original


def test_insertion_chooses_matching_local_tempo_and_respects_artist_limit():
    rows = [{'trackId': str(i)} for i in range(4)];new = {'trackId': 'new'}
    assets = {str(i): {'artist': str(i)} for i in range(4)};assets['new'] = {'artist': 'new'}
    all_rows = rows+[new]
    prepared_rows = {r['trackId']: {'features': {}, 'observations': {'entry': {'estimatedBpm': bpm}, 'exit': {'estimatedBpm': bpm}}}
                     for r, bpm in zip(all_rows, [80., 120., 121., 170., 120.5])}
    result = insert(rows, new, prepared_rows, assets)
    assert [r['trackId'] for r in result] == ['0', '1', 'new', '2', '3']


def test_moving_source_path_changes_retry_fingerprint_and_bad_cache_is_repaired(env):
    settings, db, _ = initialized(env)
    old = snapshot(db, 1)['inputHash']
    db.execute("UPDATE web_assets SET remote_path='/moved.mp3' WHERE track_key='auto:1'");db.commit()
    assert snapshot(db, 1)['inputHash'] != old
    s = snapshot(db, 1)['songs'][0];cache_path(settings, s).write_text('{broken')
    calls = []
    def prepare(settings, db, song, snap, scratch, decoder, keep):
        calls.append(song['id']);assert keep is not None;return prepared(song, keep)
    update(settings, db, snapshot(db, 1)['inputHash'], 'decoder', prepare=prepare)
    assert calls == [s['id']]


def test_failed_prerequisite_is_diagnosed_and_can_become_ready_without_manual_dj1_retry(env):
    settings, db, _ = initialized(env);aid = song(db, 3, ready=False)
    digest = db.execute('SELECT md5 FROM web_assets WHERE id=?', (aid,)).fetchone()[0]
    db.execute("INSERT INTO dj_batch_runs VALUES ('failed-run','all',0)")
    db.execute("INSERT INTO dj_batch_jobs(run_id,md5,asset_id,state,updated) VALUES ('failed-run',?,?,'failed',0)", (digest, aid));db.commit()
    assert not process_next(settings, db, 'python', 'decoder', threading.Event())
    assert get(db, STATUS)['error'] == 'AUTO_PREREQUISITE_FAILED'
    db.execute('INSERT INTO dj_evidence VALUES (?,?,0)', (digest, json.dumps({'fullComplete': True})))
    db.execute("INSERT INTO audio_loudness(md5,method,status,updated) VALUES (?,'ebu-r128-v1','complete',0)", (digest,));db.commit()
    def run(*args, **kw):
        update(settings, db, snapshot(db, 1)['inputHash'], 'decoder', prepare=lambda a,b,s,*rest:prepared(s))
        return {'done': True}
    assert process_next(settings, db, 'python', 'decoder', threading.Event(), runner=run)
    assert get(db, STATUS)['state'] == 'complete'


def test_native_cli_persists_safe_preparation_error_for_controller(env, monkeypatch):
    import aidj.fixed_set_auto as auto
    settings, db, _ = initialized(env);song(db, 3)
    expected = snapshot(db, 1)['inputHash']
    put(db, STATUS, {'state': 'working', 'inputHash': expected});db.commit()
    monkeypatch.setattr(auto, 'Settings', lambda:settings)
    monkeypatch.setattr('sys.argv', ['fixed-auto', 'run', '--input-hash', expected, '--ffmpeg', 'decoder'])
    def no_cut(*args):raise ValueError('AUTO_NO_USABLE_CUT')
    monkeypatch.setattr(auto, 'update', no_cut)
    with pytest.raises(SystemExit):auto.main()
    assert get(db, STATUS)['state'] == 'failed'
    assert get(db, STATUS)['error'] == 'AUTO_NO_USABLE_CUT'
