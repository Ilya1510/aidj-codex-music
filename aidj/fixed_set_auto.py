"""Durable DJ 1 maintenance, executed only by the serial background controller.

The controller scans metadata. One bounded native child prepares missing source
cuts and local pulse observations, then atomically publishes a complete version.
Playback queues retain their own frozen manifest until the next explicit start.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time

from .config import Settings, atomic_json
from .fixed_set_blend import artist_keys
from .fixed_set_prepare import source_rows
from .fixed_sets import signature, validate, publish

CONFIG = 'dj1_auto_config'
STATUS = 'dj1_auto_status'
VERSION = 1


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def get(db, key):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))


def folder(settings):
    return settings.state/'fixed-sets'/'dj1-auto'


def snapshot(db, playlist_id):
    if not db.execute('SELECT 1 FROM playlists WHERE id=?', (playlist_id,)).fetchone():
        raise ValueError('AUTO_PLAYLIST_MISSING')
    stored = db.execute("SELECT payload FROM dj_fixed_sets WHERE id='dj1'").fetchone()
    baseline = json.loads(stored[0]) if stored else None
    if baseline and baseline['playlistId'] != playlist_id:
        raise ValueError('AUTO_PLAYLIST_CHANGED')
    songs = list(source_rows(db, playlist_id))
    digests = {s['md5'] for s in songs}
    votes = {}
    for p in db.execute('''SELECT p.md5,p.point_ms,p.roles,v.value FROM dj_boundary_points p
                          JOIN dj_boundary_votes v ON v.point_id=p.id ORDER BY p.id'''):
        if p['md5'] in digests:
            votes.setdefault(p['md5'], []).append([p['point_ms'], p['roles'], p['value']])
    # Source metadata, exact manual crops, owner votes, and the currently served
    # plan form the compare-and-swap token. Evidence is loaded one song at a time.
    members = [{k: s[k] for k in ('id', 'md5', 'bytes', 'duration', 'artist', 'title', 'remote_path',
                                  'allowed_start', 'allowed_end')} for s in songs]
    return {'playlistId': playlist_id, 'baseline': baseline, 'songs': songs, 'votes': votes,
            'inputHash': fingerprint([playlist_id, baseline, members, votes])}


def rejected(song, snap):
    return [p[0]/1000 for p in snap['votes'].get(song['md5'], []) if p[2] == -1]


def usable(row, song, snap):
    if not isinstance(row, dict) or row.get('trackId') != song['id'] or row.get('md5') != song['md5']:
        return False
    values = [row.get(k) for k in ('start', 'end', 'fadeOut')]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        return False
    start, end, fade = values
    return (song['allowed_start'] <= start < end <= min(song['duration'], song['allowed_end'])+.001
            and end-start >= 10.5 and 0 <= fade <= 4
            and not any(abs(t-start) < .35 or abs(t-end) < .35 for t in rejected(song, snap)))


def cache_path(settings, song):
    return folder(settings)/'tracks'/(song['id']+'.json')


def cached(settings, song, row, snap):
    path = cache_path(settings, song)
    if not path.is_file() or path.stat().st_size > 1024**2:
        return None
    try:value = json.loads(path.read_text())
    except (OSError, ValueError):return None
    if not isinstance(value, dict):return None
    if (value.get('version') != VERSION or value.get('md5') != song['md5'] or
            value.get('bytes') != song['bytes'] or value.get('duration') != song['duration'] or
            not usable(value.get('row'), song, snap)):
        return None
    if row and any(value['row'][k] != row[k] for k in ('start', 'end', 'fadeOut')):
        return None
    return value


def artist_limit(rows, assets):
    keys = [artist_keys(assets[r['trackId']]) for r in rows]
    return all(keys) and not any(a & b & c for a, b, c in zip(keys, keys[1:], keys[2:]))


def insert(rows, new, prepared, assets):
    """Choose the least costly two adjacent changes; keep the existing opening."""
    def tempo(row, role):
        p = prepared[row['trackId']]
        n = p['observations'].get(role, {}).get('estimatedBpm') or p['features'].get('bpm')
        return n if type(n) in (int, float) and math.isfinite(n) and 60 <= n <= 210 else None
    def distance(a, b):
        x, y = tempo(a, 'exit'), tempo(b, 'entry')
        cost = abs(math.log(x/y))*12 if x and y else 2.
        x = prepared[a['trackId']]['features'].get('chroma')
        y = prepared[b['trackId']]['features'].get('chroma')
        if x and y and len(x) == len(y) == 12:
            nx, ny = math.sqrt(sum(v*v for v in x)), math.sqrt(sum(v*v for v in y))
            if nx and ny:cost += (1-sum(v*w for v, w in zip(x, y))/(nx*ny))*.18
        return cost
    if not rows:return [new]
    choices = []
    for at in range(1, len(rows)+1):
        candidate = rows[:at]+[new]+rows[at:]
        if not artist_limit(candidate, assets):continue
        score = distance(rows[at-1], new)
        if at < len(rows):
            score += distance(new, rows[at])-distance(rows[at-1], rows[at])
        choices.append((score, at))
    if not choices:return None
    _, at = min(choices)
    return rows[:at]+[new]+rows[at:]


def seed(settings, db, work):
    """Explicitly enable from the already published, source-pinned release."""
    plan = json.loads((work/'plan.json').read_text())
    config = {'enabled': True, 'version': VERSION, 'playlistId': plan['playlistId']}
    snap = snapshot(db, config['playlistId'])
    if snap['baseline'] != plan:raise ValueError('AUTO_SEED_STALE')
    sources = {s['id']: s for s in snap['songs']}
    observations = json.loads((work/'observation-manifest.json').read_text())
    features = json.loads((work/'feature-manifest.json').read_text())
    for row in plan['tracks']:
        song = sources[row['trackId']]
        if not usable(row, song, snap):raise ValueError('AUTO_SEED_BOUNDS')
        p = observations[song['id']];path = Path(p['file'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != p['sha256']:raise ValueError('AUTO_SEED_OBSERVATIONS')
        obs = json.loads(path.read_text())
        if (obs['md5'] != song['md5'] or obs['sourceStart'] != row['start'] or
                obs['sourceEnd'] != row['end'] or obs['status'] != 'complete'):
            raise ValueError('AUTO_SEED_SOURCE')
        feature = {}
        if song['id'] in features:
            p = features[song['id']];path = Path(p['file'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != p['sha256']:raise ValueError('AUTO_SEED_FEATURES')
            value = json.loads(path.read_text())
            if value['md5'] != song['md5']:raise ValueError('AUTO_SEED_SOURCE')
            feature = {k: value.get(k) for k in ('bpm', 'chroma')}
        atomic_json(cache_path(settings, song), {'version': VERSION, 'md5': song['md5'],
                    'bytes': song['bytes'], 'duration': song['duration'], 'row': row,
                    'observations': obs, 'features': feature})
    db.execute('BEGIN IMMEDIATE')
    if snapshot(db, config['playlistId'])['inputHash'] != snap['inputHash']:
        db.rollback();raise ValueError('AUTO_SEED_STALE')
    put(db, CONFIG, config)
    put(db, STATUS, {'state': 'complete', 'inputHash': snap['inputHash'], 'revision': plan['revision'],
                     'tracks': len(plan['tracks']), 'updated': time.time()})
    db.commit()


def ready(db, songs):
    for song in songs:
        if not 0 < song['bytes'] <= 40*1024**2 or not 10.5 <= song['duration'] <= 1800:
            raise ValueError('AUTO_SOURCE_LIMIT')
        row = db.execute('''SELECT json_extract(e.payload,'$.fullComplete'),l.status FROM dj_evidence e
                            JOIN audio_loudness l ON l.md5=e.md5 WHERE e.md5=?''', (song['md5'],)).fetchone()
        if not row or row[0] != 1 or row[1] != 'complete':
            failed = db.execute("SELECT 1 FROM dj_batch_jobs WHERE md5=? AND state='failed' LIMIT 1", (song['md5'],)).fetchone()
            if failed:raise ValueError('AUTO_PREREQUISITE_FAILED')
            return False
    return True


def process_next(settings, db, analysis_python, decoder, stopped, runner=None):
    """Metadata-only scan under dj-worker.lock; launch at most one bounded child.

    A failed fingerprint is not retried unchanged. New inputs or explicit retry
    permit another attempt; interruptions are resumable. No extra heavy daemon.
    """
    config = get(db, CONFIG)
    if not config.get('enabled'):return False
    try:snap = snapshot(db, config['playlistId'])
    except ValueError:
        put(db, STATUS, {'state': 'blocked', 'error': 'AUTO_PLAYLIST_MISSING', 'updated': time.time()});db.commit();return False
    status = get(db, STATUS)
    if status.get('inputHash') == snap['inputHash'] and status.get('state') in ('complete', 'failed'):
        return False
    if status.get('inputHash') == snap['inputHash'] and status.get('state') == 'working':
        put(db, STATUS, {**status, 'state': 'failed', 'error': 'AUTO_ABANDONED_REVIEW', 'updated': time.time()})
        db.commit();return False
    try:is_ready = ready(db, snap['songs'])
    except ValueError as error:
        state = 'blocked' if str(error) == 'AUTO_PREREQUISITE_FAILED' else 'failed'
        if status.get('state') != state or status.get('error') != str(error) or status.get('inputHash') != snap['inputHash']:
            put(db, STATUS, {'state': state, 'inputHash': snap['inputHash'], 'error': str(error), 'updated': time.time()});db.commit()
        return False
    if not is_ready:
        if status.get('state') != 'waiting' or status.get('inputHash') != snap['inputHash']:
            put(db, STATUS, {'state': 'waiting', 'inputHash': snap['inputHash'], 'updated': time.time()});db.commit()
        return False
    if shutil.disk_usage(settings.state).free < 768*1024**2:return False
    from .dj_remote import run_analysis, RemoteError
    runner = runner or run_analysis
    put(db, STATUS, {'state': 'working', 'inputHash': snap['inputHash'], 'updated': time.time()});db.commit()
    scratch = settings.state/'work'/('fixed-auto-'+snap['inputHash'][:20])
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, AIDJ_STATE_DIR=str(settings.state), OMP_NUM_THREADS='1',
               OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMBA_NUM_THREADS='1')
    try:
        result = runner([str(analysis_python), '-m', 'aidj.fixed_set_auto', 'run', '--input-hash',
                         snap['inputHash'], '--ffmpeg', str(decoder)], env, stopped, threading.Event(),
                        timeout=900)
        if get(db, STATUS).get('state') != 'complete':raise ValueError('AUTO_CHILD_INCOMPLETE')
        print('DJ_FIXED_AUTO_COMPLETE', json.dumps(result), flush=True)
    except Exception as error:
        state = 'queued' if isinstance(error, RemoteError) and error.kind == 'INTERRUPTED' else 'failed'
        # The worker may have committed before an interrupted acknowledgement.
        current = get(db, STATUS)
        if current.get('state') != 'complete':
            put(db, STATUS, {**current, 'state': state, 'inputHash': snap['inputHash'],
                             'error': current.get('error') or (error.kind if isinstance(error, RemoteError) else type(error).__name__),
                             'updated': time.time()});db.commit()
        print('DJ_FIXED_AUTO_STOPPED', state, type(error).__name__, flush=True)
    finally:shutil.rmtree(scratch, ignore_errors=True)
    return True


def update(settings, db, expected, decoder, prepare=None, planner=None):
    """Prepare privately, then compare all inputs again inside the write lock."""
    from .fixed_set_uniform import uniform_program
    from .fixed_set_auto_audio import prepare_song
    prepare = prepare or prepare_song;planner = planner or uniform_program
    config = get(db, CONFIG)
    if not config.get('enabled'):raise ValueError('AUTO_DISABLED')
    snap = snapshot(db, config['playlistId'])
    if snap['inputHash'] != expected:raise ValueError('AUTO_INPUT_CHANGED')
    if not ready(db, snap['songs']):raise ValueError('AUTO_NOT_READY')
    original = snap['baseline'] or {'id': 'dj1', 'playlistId': config['playlistId'], 'tracks': []}
    old = {r['trackId']: r for r in original['tracks']}
    assets = {s['id']: s for s in snap['songs']}
    prepared = {};changed = []
    scratch = settings.state/'work'/('fixed-auto-'+expected[:20]);scratch.mkdir(parents=True, exist_ok=True)
    for song in snap['songs']:
        row = old.get(song['id'])
        if not usable(row, song, snap):row = None
        value = cached(settings, song, row, snap)
        if value is None:
            value = prepare(settings, db, song, snap, scratch, decoder, row)
            if not usable(value.get('row'), song, snap):raise ValueError('AUTO_PREPARED_BOUNDS')
            atomic_json(cache_path(settings, song), value);changed.append(song['id'])
        prepared[song['id']] = value
    rows = [prepared[r['trackId']]['row'] for r in original['tracks'] if r['trackId'] in prepared]
    additions = [prepared[s['id']]['row'] for s in snap['songs'] if s['id'] not in old]
    reorder = not artist_limit(rows, assets)
    for row in additions:
        placed = None if reorder else insert(rows, row, prepared, assets)
        if placed is None:rows.append(row);reorder = True
        else:rows = placed
    result = None
    if rows:
        # An old final row may now be interior. Canonicalize kinds before strict
        # validation, then verify all phases using the final constant speeds.
        rows = [dict(r, transitionSeconds=5. if i<len(rows)-1 else 0.,
                     transitionKind='crossfade' if i<len(rows)-1 else 'clean-cut') for i, r in enumerate(rows)]
        value = {**original, 'tracks': rows, 'revision': signature(rows)}
        result = planner(value, assets, {k: v['observations'] for k, v in prepared.items()},
                         {s['id']: (s['allowed_start'], s['allowed_end']) for s in snap['songs']},
                         {k: v['features'] for k, v in prepared.items()},
                         preserve_order=not reorder, opening_id=rows[0]['trackId'])
        plan = result['plan']
        validate(plan, assets, {s['id']: (s['allowed_start'], s['allowed_end']) for s in snap['songs']})
        if (not artist_limit(plan['tracks'], assets) or len(plan['tracks']) != len(snap['songs']) or
                any(not 4.75 <= r['transitionSeconds'] <= 5.25 for r in plan['tracks'][:-1])):
            raise ValueError('AUTO_PLAN_INVALID')
    db.execute('BEGIN IMMEDIATE')
    try:
        if get(db, CONFIG) != config or snapshot(db, config['playlistId'])['inputHash'] != expected:
            raise ValueError('AUTO_INPUT_CHANGED')
        atomic_json(folder(settings)/'previous-plan.json', snap['baseline'])
        if result:
            atomic_json(folder(settings)/'latest-audit.json', result)
            saved = publish(db, result['plan'], commit=False)
        else:
            db.execute("DELETE FROM dj_fixed_sets WHERE id='dj1'");saved = None
        current = snapshot(db, config['playlistId'])
        put(db, STATUS, {'state': 'complete', 'inputHash': current['inputHash'],
                        'revision': saved['revision'] if saved else None, 'tracks': len(rows),
                        'preparedTracks': changed, 'addedTracks': [r['trackId'] for r in additions],
                        'updated': time.time()})
        db.commit()
    except BaseException:db.rollback();raise


def main():
    parser = argparse.ArgumentParser();parser.add_argument('action', choices=['enable', 'run', 'retry', 'status'])
    parser.add_argument('--seed', type=Path);parser.add_argument('--input-hash');parser.add_argument('--ffmpeg')
    args = parser.parse_args();settings = Settings();settings.prepare()
    from .store import connect
    db = connect(settings, dj_enrol=False)
    try:
        if args.action == 'enable':
            if not args.seed:parser.error('enable requires --seed')
            seed(settings, db, args.seed)
        elif args.action == 'run':
            if not args.input_hash or not args.ffmpeg:parser.error('run requires input hash and decoder')
            update(settings, db, args.input_hash, args.ffmpeg)
        elif args.action == 'retry':
            put(db, STATUS, {'state': 'queued', 'updated': time.time()});db.commit()
        print(json.dumps(get(db, STATUS)), flush=True)
    except Exception as error:
        db.rollback()
        safe = str(error) if isinstance(error, ValueError) and str(error).startswith('AUTO_') else type(error).__name__
        if args.action == 'run':
            current = get(db, STATUS)
            if current.get('state') == 'working' and current.get('inputHash') == args.input_hash:
                put(db, STATUS, {**current, 'state': 'failed', 'error': safe, 'updated': time.time()});db.commit()
        print('DJ_FIXED_AUTO_FAILED', safe, flush=True)
        raise SystemExit(1)
    finally:db.close()


if __name__ == '__main__':main()
