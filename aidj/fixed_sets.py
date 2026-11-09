"""Versioned, frozen DJ sets. Compiling a set is separate from serving it."""
import hashlib
import json
import math
import re


def validate(value, assets, ranges=None):
    ranges = ranges or {}
    if not isinstance(value, dict) or value.get('id') != 'dj1':
        raise ValueError('FIXED_SET_ID')
    if not isinstance(value.get('revision'), str) or not re.fullmatch('[a-f0-9]{20}', value['revision']):
        raise ValueError('FIXED_SET_REVISION')
    if type(value.get('playlistId')) is not int or not isinstance(value.get('tracks'), list) or not 1 <= len(value['tracks']) <= 2000:
        raise ValueError('FIXED_SET_TRACKS')
    out = {k: value[k] for k in ('id', 'revision', 'playlistId')}
    out.update(name='DJ 1', description='Фиксированный сет · около минуты из каждой песни', tracks=[])
    seen = set()
    for row in value['tracks']:
        if not isinstance(row, dict):
            raise ValueError('FIXED_SET_ROW')
        aid = row.get('trackId'); asset = assets.get(aid)
        if not asset or aid in seen or row.get('md5') != asset['md5']:
            raise ValueError('FIXED_SET_ASSET')
        seen.add(aid)
        numbers = [row.get(k) for k in ('start', 'end', 'fadeIn', 'fadeOut', 'transitionSeconds', 'rate')]
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in numbers):
            raise ValueError('FIXED_SET_NUMBER')
        start, end, fade_in, fade_out, overlap, rate = numbers
        lo, hi = ranges.get(aid, (0, asset['duration']))
        if not (0 <= lo <= start < end <= min(hi, asset['duration']) + .05 and end-start >= 8):
            raise ValueError('FIXED_SET_BOUNDS')
        if not (.97 <= rate <= 1.03 and 0 <= overlap <= 8 and 0 <= fade_in <= 4 and 0 <= fade_out <= 4 and fade_in+fade_out < end-start):
            raise ValueError('FIXED_SET_TRANSITION')
        kind = row.get('transitionKind')
        if kind not in ('crossfade', 'clean-cut') or (kind == 'clean-cut' and overlap != 0):
            raise ValueError('FIXED_SET_KIND')
        out['tracks'].append({k: row[k] for k in ('trackId', 'md5', 'start', 'end', 'fadeIn', 'fadeOut', 'transitionSeconds', 'transitionKind', 'rate')})
    if out['tracks'][-1]['transitionSeconds'] != 0:
        raise ValueError('FIXED_SET_LAST')
    if signature(out['tracks']) != out['revision']:
        raise ValueError('FIXED_SET_REVISION_CHANGED')
    out['duration'] = round(sum((r['end']-r['start'])/r['rate']-r['transitionSeconds'] for r in out['tracks']), 3)
    return out


def signature(tracks):
    return hashlib.sha256(json.dumps(tracks, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:20]


def records(db):
    # Old backups/fixtures need not contain a set. Never turn stale cuts into a
    # silently different set when a source or the owner's allowed range changes.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dj_fixed_sets'").fetchone():
        return []
    assets = {r['id']: dict(r) for r in db.execute('SELECT id,md5,duration FROM web_assets')}
    ranges = {r['track_id']: (r['start'], r['end']) for r in db.execute('SELECT * FROM playback_ranges')}
    result = []
    for row in db.execute('SELECT payload FROM dj_fixed_sets ORDER BY id'):
        try:
            value = validate(json.loads(row['payload']), assets, ranges)
            playlist = db.execute('SELECT 1 FROM playlists WHERE id=?', (value['playlistId'],)).fetchone()
            if playlist:result.append(value)
        except (ValueError, TypeError, KeyError):
            continue
    return result


def publish(db, value):
    assets = {r['id']: dict(r) for r in db.execute('SELECT id,md5,duration FROM web_assets')}
    ranges = {r['track_id']: (r['start'], r['end']) for r in db.execute('SELECT * FROM playback_ranges')}
    clean = validate(value, assets, ranges)
    canonical = {}
    for row in db.execute("SELECT a.id,a.md5 FROM playlist_tracks p JOIN web_assets a ON a.track_key=p.track_key WHERE p.playlist_id=? AND p.state='complete' ORDER BY p.position", (clean['playlistId'],)):
        canonical.setdefault(row['md5'], row['id'])
    actual = set(canonical.values())
    if actual != {r['trackId'] for r in clean['tracks']}:
        raise ValueError('FIXED_SET_INCOMPLETE')
    db.execute('INSERT INTO dj_fixed_sets(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (clean['id'], json.dumps(clean)))
    db.commit()
    return clean
