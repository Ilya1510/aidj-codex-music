"""Version a gentle playback envelope without changing reviewed audio cuts."""
import re
import unicodedata

from .fixed_sets import signature, validate


def artist_keys(asset):
    """Use the credited artist, extracting the artist from known upload channels.

    Collaborators count too: three consecutive Guetta collaborations are still
    three consecutive Guetta songs. A label/uploader is not a performer.
    """
    artist = asset.get('artist', '').strip()
    if artist.casefold() in {'ultra records', 'savichevamusic'}:
        pieces = re.split(r'\s+[—–-]\s+', asset.get('title', ''), maxsplit=1)
        if len(pieces) != 2:
            raise ValueError('BLEND_ARTIST_CREDIT_MISSING')
        artist = pieces[0]
    parts = re.split(r',|\s+(?:feat\.?|ft\.?|featuring|&|x|и)\s+', artist, flags=re.I)
    keys = {''.join(c for c in unicodedata.normalize('NFKC', p).casefold().replace('ё', 'е')
                    if c.isalnum()) for p in parts}
    return keys - {''}


def blend(value, assets, ranges=None, seconds=3.):
    """Keep every proven cut, only move a song when a third artist repeat occurs.

    This deliberately does not claim beat matching, new instrumental intros,
    or revalidate the old acoustic evidence under a new name.
    """
    original = validate(value, assets, ranges)
    if type(seconds) not in (int, float) or not 1 <= seconds <= 4:
        raise ValueError('BLEND_SECONDS')
    pending = [dict(row) for row in original['tracks']]
    keys = {r['trackId']: artist_keys(assets[r['trackId']]) for r in pending}
    if any(not v for v in keys.values()):
        raise ValueError('BLEND_ARTIST_UNKNOWN')
    ordered = []
    while pending:
        blocked = (keys[ordered[-1]['trackId']] & keys[ordered[-2]['trackId']]
                   if len(ordered) >= 2 else set())
        at = next((i for i, row in enumerate(pending) if not keys[row['trackId']] & blocked), None)
        if at is None:
            # Never silently weaken the requested maximum to finish a plan.
            raise ValueError('BLEND_ARTIST_LIMIT_IMPOSSIBLE')
        ordered.append(pending.pop(at))
    for i, row in enumerate(ordered):
        row['fadeIn'] = max(row['fadeIn'], seconds)
        row['transitionSeconds'] = seconds if i < len(ordered)-1 else 0.
        row['transitionKind'] = 'crossfade' if i < len(ordered)-1 else 'clean-cut'
    result = {**original, 'tracks': ordered, 'revision': signature(ordered)}
    return validate(result, assets, ranges)
