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


def revise_boundary(value, correction, assets, ranges=None):
    """Correct one reviewed cut without running the song-order selector again.

    Quiet proof describes a continuously measured low-energy window, not the
    minimum of a single FFT frame. The whole fade and a breath before it must
    fit inside that window. No absence-of-ASR claim is inferred here.
    """
    value=validate(value,assets,ranges)
    if not isinstance(correction,dict):raise ValueError('FIXED_CORRECTION')
    proof=correction.get('quietWindow')
    if not isinstance(proof,dict):raise ValueError('FIXED_CORRECTION_QUIET')
    fields=('start','end','maxRelativeRms','lastVocalEnd')
    if any(type(proof.get(k)) not in (int,float) or not math.isfinite(proof[k]) for k in fields):raise ValueError('FIXED_CORRECTION_QUIET')
    if not (proof['end']-proof['start']>=.7 and 0<=proof['maxRelativeRms']<=.08):raise ValueError('FIXED_CORRECTION_QUIET')
    result={**value,'tracks':[dict(r) for r in value['tracks']]}
    matches=[r for r in result['tracks'] if r['trackId']==correction.get('trackId')]
    if len(matches)!=1:raise ValueError('FIXED_CORRECTION_TRACK')
    row=matches[0]
    if row['md5']!=correction.get('md5') or row['end']!=correction.get('previousEnd'):raise ValueError('FIXED_CORRECTION_STALE')
    end,fade=correction.get('end'),correction.get('fadeOut')
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in (end,fade)):raise ValueError('FIXED_CORRECTION_NUMBER')
    if not (proof['start']<=end-fade-.15 and end<=proof['end'] and proof['lastVocalEnd']+.35<=end-fade):raise ValueError('FIXED_CORRECTION_VOCAL_OR_FADE')
    row.update(end=end,fadeOut=fade)
    result['revision']=signature(result['tracks'])
    return validate(result,assets,ranges)


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
