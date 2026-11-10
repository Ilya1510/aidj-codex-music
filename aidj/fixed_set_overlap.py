"""Only extend a blend through a measured quiet side of the actual audio cut."""
import math

from .fixed_set_vocal_review import _vectors


def quiet_vocal_window(evidence, md5, start, end, gain_db, *, words=None, coverage=None):
    """Replay raw model frames, never treat missing recognizer words as silence.

    A low estimated vocal stem can miss singing. This is an acoustic selection
    criterion, not a verified semantic word boundary or a listening verdict.
    """
    if evidence.get('md5') != md5 or not 0 <= start < end or words is None or coverage is None:
        return None
    # A separator can suppress an actual singer. Any recognizer span, including
    # low-confidence spans, vetoes a long blend; no absent-coverage shortcut.
    if any(a-.08 < end and b+.08 > start for a, b, *_ in words):
        return None
    cursor = start
    for a, b in sorted(coverage):
        if a > cursor+1e-7:
            break
        if b > cursor:
            cursor = b
        if cursor >= end-1e-7:
            break
    if cursor < end-1e-7:
        return None
    values = _vectors(evidence)
    if values is None or not math.isfinite(gain_db):
        return None
    offset, hop, duration, arrays = values
    if start < offset+.25-1e-7 or end > offset+duration-.25+1e-7:
        return None
    first = math.floor((start-offset)/hop+1e-8)
    last = math.ceil((end-offset)/hop-1e-8)
    voice = sorted(arrays[1][first:last])
    if not voice:
        return None
    db = lambda v: 20*math.log10(max(v, 1e-10))+gain_db
    maximum = db(voice[-1])
    p95 = db(voice[math.ceil(.95*len(voice))-1])
    if maximum > -30 or p95 > -33:
        return None
    return {'method': 'measured-low-vocal-overlap-v1', 'start': start, 'end': end,
            'sourceMD5': md5, 'cacheKey': evidence.get('cacheKey'),
            'normalizedVocalMaxDbfs': maximum, 'normalizedVocalP95Dbfs': p95,
            'playbackGainDb': gain_db, 'frames': len(voice),
            'asrCoverageComplete': True, 'asrConflictVetoApplied': True,
            'verifiedWordBoundary': False, 'voiceAbsenceProven': False}


def fallback_overlap(checkpoint, maximum=3., rate=1.):
    """Blend only over the already reviewed release, not an active final phrase."""
    row = checkpoint['row']; exit = checkpoint.get('exit', {})
    region = exit.get('regionStart', row['end'])
    if type(region) not in (int, float) or not math.isfinite(region):
        return 0.
    # Keep100ms inside the measured region, independent of the short fade.
    if type(rate) not in (int, float) or not math.isfinite(rate) or not .97 <= rate <= 1.03:
        raise ValueError('OVERLAP_RATE')
    available = max(0., row['end']-region-.1)/rate
    return round(min(maximum, available), 4) if available >= .2 else 0.
