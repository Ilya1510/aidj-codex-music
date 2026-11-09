"""Conservative numeric quiet boundaries, never a claim of human listening.

RMS values describe consecutive, non-overlapping time bins. A low-energy bin
alone is not a usable boundary: the whole region must last long enough, avoid
known vocals, and have observed ASR coverage or near-absolute physical silence.
No NumPy/model dependency is needed; one source's small RMS envelope is enough.
"""
import bisect
import math


def _number(value, label):
    if isinstance(value, bool):
        raise ValueError(label)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(label) from None
    if not math.isfinite(value):
        raise ValueError(label)
    return value


def _intervals(rows, padding=0):
    result = []
    for row in rows:
        if len(row) < 2:
            raise ValueError('QUIET_INTERVAL')
        start, end = _number(row[0], 'QUIET_INTERVAL'), _number(row[1], 'QUIET_INTERVAL')
        if end < start:
            raise ValueError('QUIET_INTERVAL')
        if end > start:
            result.append((start-padding, end+padding))
    merged = []
    for start, end in sorted(result):
        if merged and start <= merged[-1][1]+1e-9:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _quantile75(values):
    ordered = sorted(values)
    if not ordered:
        return 0.
    at = (len(ordered)-1)*.75
    lo = math.floor(at); hi = math.ceil(at)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(at-lo)


def detect_quiet_regions(rms, hop_seconds=.01, start_seconds=0, *, words=(),
                         coverage=(), reference=None, max_relative=.08,
                         min_seconds=.7, absolute_silence=.001, word_padding=.2):
    """Return continuous quiet regions and explicit evidence qualifications.

    ``words`` are numeric ASR spans, including low-confidence recognitions;
    ``coverage`` contains intervals actually analysed. Missing coverage never
    means no vocals. A bin below the absolute silence threshold may instead be
    supported by its physical level. That exception also handles silent files
    whose own 75th-percentile reference is zero or close to zero.

    ``validated`` means this conservative numeric rule passed, not that a
    person listened or that a musical phrase was recognised. Uncovered,
    non-absolute quiet runs remain in the result with ``validated=False``.
    """
    hop = _number(hop_seconds, 'QUIET_HOP')
    start = _number(start_seconds, 'QUIET_START')
    relative = _number(max_relative, 'QUIET_RELATIVE')
    minimum = _number(min_seconds, 'QUIET_MINIMUM')
    absolute = _number(absolute_silence, 'QUIET_ABSOLUTE')
    padding = _number(word_padding, 'QUIET_PADDING')
    if hop <= 0 or start < 0 or not 0 < relative <= 1 or minimum <= 0 or absolute < 0 or padding < 0:
        raise ValueError('QUIET_SETTINGS')
    # Non-finite/negative observations are missing evidence, never silence.
    values = []
    for value in rms:
        try:
            level = _number(value, 'QUIET_RMS')
        except ValueError:
            level = None
        values.append(level if level is not None and level >= 0 else None)
    if not values:
        return []
    reference = _quantile75([v for v in values if v is not None]) if reference is None else _number(reference, 'QUIET_REFERENCE')
    if reference < 0:
        raise ValueError('QUIET_REFERENCE')
    threshold = reference*relative
    vocals = _intervals(words, padding)
    observed = _intervals(coverage)
    vocal_ends = [row[1] for row in vocals]
    observed_starts = [row[0] for row in observed]
    runs = []; run = None

    def finish(end):
        nonlocal run
        if run is not None and end-run['start'] >= minimum-1e-8:
            run['end'] = end
            run['duration'] = end-run['start']
            run['relativeMax'] = run['maxRms']/reference if reference else 0.
            run['absoluteSilence'] = run['maxRms'] <= absolute
            if not run['validated']:
                run['proof'] = 'unknown-vocal-coverage'
            elif run['absoluteSilence']:
                run['proof'] = 'physical-near-silence'
            elif run['coverageComplete']:
                run['proof'] = 'observed-vocal-clear-quiet'
            else:
                run['proof'] = 'observed-or-physical-quiet'
            runs.append(run)
        run = None

    for i, level in enumerate(values):
        lo, hi = start+i*hop, start+(i+1)*hop
        at = bisect.bisect_right(vocal_ends, lo+1e-9)
        occupied = at < len(vocals) and vocals[at][0] < hi-1e-9
        if level is None or occupied or level > max(threshold, absolute):
            finish(lo)
            continue
        at = bisect.bisect_right(observed_starts, lo+1e-9)-1
        covered = at >= 0 and observed[at][1] >= hi-1e-9
        valid = covered or level <= absolute
        if run is not None and run['validated'] != valid:
            finish(lo)
        if run is None:
            run = {'start': lo, 'maxRms': level, 'coverageComplete': covered,
                   'validated': valid, 'referenceRms': reference,
                   'wordPadding': padding}
        else:
            run['maxRms'] = max(run['maxRms'], level)
            run['coverageComplete'] = run['coverageComplete'] and covered
    finish(start+len(values)*hop)
    return runs


def boundary_points(regions, *, guard=.15, max_fade=.8, include_unknown=False):
    """Place entries and exit fade envelopes wholly inside quiet regions.

    ``time`` is a source timestamp. For exits ``fadeStart=time-fade``. The
    default never produces actionable points from unknown vocal coverage.
    """
    guard = _number(guard, 'QUIET_GUARD'); max_fade = _number(max_fade, 'QUIET_FADE')
    if guard < 0 or max_fade <= 0:
        raise ValueError('QUIET_POINT_SETTINGS')
    entries = []; exits = []
    for region in regions:
        if not region.get('validated') and not include_unknown:
            continue
        lo = _number(region['start'], 'QUIET_REGION')+guard
        hi = _number(region['end'], 'QUIET_REGION')-guard
        if hi-lo < .1-1e-8:
            continue
        common = {'proof': region['proof'], 'validated': bool(region['validated']),
                  'regionStart': region['start'], 'regionEnd': region['end'],
                  'coverageComplete': bool(region['coverageComplete']),
                  'absoluteSilence': bool(region['absoluteSilence']),
                  'relativeMax': region['relativeMax']}
        # Enter just before the following attack, and leave soon after the
        # preceding music has become quiet; do not insert a long silent gap.
        entry_fade = min(.1, hi-lo)
        entries.append({**common, 'time': hi-entry_fade, 'fade': entry_fade})
        fade = min(max_fade, hi-lo)
        exits.append({**common, 'time': lo+fade, 'fade': fade, 'fadeStart': lo})
    return {'entries': entries, 'exits': exits}


def digital_silence_regions(rms, hop_seconds=.01, start_seconds=0, *,
                            maximum_rms=.0001, min_seconds=1.2):
    """Explicit physical-only exception for sustained near-digital silence.

    ASR can hallucinate words in a file's digital padding. This separate pass
    deliberately does not consult ASR, requires at least 1.2 seconds and at
    most -80 dBFS RMS, and labels that exception instead of inventing a word
    gap. Callers cannot accidentally loosen this override's safety thresholds.
    """
    maximum = _number(maximum_rms, 'QUIET_DIGITAL_LEVEL')
    minimum = _number(min_seconds, 'QUIET_DIGITAL_DURATION')
    if not 0 < maximum <= .0001 or minimum < 1.2:
        raise ValueError('QUIET_DIGITAL_SETTINGS')
    regions = detect_quiet_regions(rms, hop_seconds, start_seconds, reference=0,
                                  min_seconds=minimum, absolute_silence=maximum)
    for region in regions:
        region['proof'] = 'near-digital-silence-without-asr'
        region['asrIgnored'] = True
    return regions


def _eof_bounds(rms, hop_seconds, start_seconds, source_duration, allowed_end):
    hop = _number(hop_seconds, 'QUIET_HOP')
    start = _number(start_seconds, 'QUIET_START')
    duration = _number(source_duration, 'QUIET_DURATION')
    end = duration if allowed_end is None else _number(allowed_end, 'QUIET_END')
    if hop <= 0 or start < 0 or duration <= start:
        raise ValueError('QUIET_EOF_SETTINGS')
    # A manually shortened range is not the original ending, even if its own
    # final samples happen to be silent. At most one RMS bin can be partial.
    measured_end = start+len(rms)*hop
    if abs(end-duration) > 1e-8 or len(rms) == 0 or abs(measured_end-duration) > hop+1e-8:
        return None
    return hop, start, min(duration, measured_end), duration


def short_digital_eof(rms, hop_seconds=.01, start_seconds=0, *, source_duration,
                      allowed_end=None, min_seconds=.25, maximum_rms=.0001,
                      max_fade=.15, guard=.025):
    """A short physical-silence exception exclusively at the original EOF.

    ASR timestamps may extend into digital padding. Only a sustained measured
    <=-80 dBFS tail can override them here. An internal gap or manual range end
    cannot use this exception; its minimum does not change the 1.2-second
    internal ``digital_silence_regions`` rule. Unmeasured partial final bins
    are never included in the fade envelope.
    """
    minimum = _number(min_seconds, 'QUIET_EOF_MINIMUM')
    maximum = _number(maximum_rms, 'QUIET_EOF_LEVEL')
    fade_max = _number(max_fade, 'QUIET_FADE'); margin = _number(guard, 'QUIET_GUARD')
    if minimum < .15 or not 0 < maximum <= .0001 or fade_max <= 0 or margin < 0:
        raise ValueError('QUIET_EOF_SETTINGS')
    bounds = _eof_bounds(rms, hop_seconds, start_seconds, source_duration, allowed_end)
    if bounds is None:
        return None
    hop, start, end, duration = bounds
    first = len(rms); peak = 0.
    while first > 0:
        try:
            value = _number(rms[first-1], 'QUIET_RMS')
        except ValueError:
            break
        if value < 0 or value > maximum:
            break
        peak = max(peak, value); first -= 1
    begin = start+first*hop
    if end-begin < minimum-1e-8 or end-begin-2*margin < .05:
        return None
    fade = min(fade_max, end-begin-2*margin)
    time = end-margin
    return {'time': time, 'fade': fade, 'fadeStart': time-fade,
            'regionStart': begin, 'regionEnd': end, 'maxRms': peak,
            'validated': True, 'proof': 'short-digital-EOF',
            'absoluteSilence': True, 'coverageComplete': False,
            'asrIgnored': True, 'sourceEnd': duration,
            'unmeasuredTail': max(0, duration-end)}


def short_natural_release(rms, hop_seconds=.01, start_seconds=0, *, source_duration,
                           reference, words, probe, allowed_end=None,
                           min_seconds=.3, max_relative=.18, max_fade=.15,
                           guard=.025, word_padding=.3):
    """Quiet original tail with fresh observed vocal clearance, never override.

    ``probe`` must be the new recognition around this exact final region, with
    at least three confident words elsewhere in that window. Empty ASR output
    or a probe of another part of the song cannot certify this exception.
    """
    minimum = _number(min_seconds, 'QUIET_RELEASE_MINIMUM')
    relative = _number(max_relative, 'QUIET_RELATIVE')
    reference = _number(reference, 'QUIET_REFERENCE')
    fade_max = _number(max_fade, 'QUIET_FADE'); margin = _number(guard, 'QUIET_GUARD')
    padding = _number(word_padding, 'QUIET_PADDING')
    if minimum < .3 or not 0 < relative <= .35 or reference <= 0 or fade_max <= 0 or margin < 0 or padding < .3:
        raise ValueError('QUIET_RELEASE_SETTINGS')
    bounds = _eof_bounds(rms, hop_seconds, start_seconds, source_duration, allowed_end)
    if bounds is None:
        return None
    hop, start, end, duration = bounds
    threshold = reference*relative; first = len(rms); tail = []
    while first > 0:
        try:
            value = _number(rms[first-1], 'QUIET_RMS')
        except ValueError:
            break
        if value < 0 or value > threshold:
            break
        tail.append(value); first -= 1
    begin = start+first*hop
    if end-begin < minimum-1e-8 or end-begin-2*margin < .05:
        return None
    fade = min(fade_max, end-begin-2*margin); time = end-margin; fade_start = time-fade
    if any(lo < time and hi > fade_start for lo, hi in _intervals(words, padding)):
        return None
    if not probe or probe.get('confident', 0) < 3 or probe.get('start', math.inf) > begin-padding or probe.get('end', -math.inf) < duration-1e-8:
        return None
    # Values run backwards from EOF: the end must not grow louder than the
    # preceding part of this measured release. Flat quiet noise is acceptable.
    width = max(1, len(tail)//3)
    if sum(tail[:width])/width > sum(tail[-width:])/width*1.1+1e-9:
        return None
    peak = max(tail)
    return {'time': time, 'fade': fade, 'fadeStart': fade_start,
            'regionStart': begin, 'regionEnd': end, 'maxRms': peak,
            'relativeMax': peak/reference, 'validated': True,
            'proof': 'short-natural-release', 'coverageComplete': True,
            'absoluteSilence': False, 'asrIgnored': False,
            'wordPadding': padding, 'sourceEnd': duration,
            'unmeasuredTail': max(0, duration-end)}


def natural_ending(duration, *, words=(), allowed_start=0, allowed_end=None,
                   max_fade=.8, word_padding=.2):
    """Explicitly uncertain original-ending fallback, never a safe word gap.

    Protects recognised final vocals from the fade where possible. Unknown
    vocals are still possible; this result must not be labelled validated.
    A manual crop crossing a recognised word gets no invented safe fade.
    """
    duration = _number(duration, 'QUIET_DURATION')
    start = _number(allowed_start, 'QUIET_START')
    end = duration if allowed_end is None else _number(allowed_end, 'QUIET_END')
    maximum = _number(max_fade, 'QUIET_FADE'); padding = _number(word_padding, 'QUIET_PADDING')
    if not 0 <= start < end <= duration or maximum < 0 or padding < 0:
        raise ValueError('QUIET_ENDING_SETTINGS')
    vocals = _intervals(words)
    heard = [(lo, hi) for lo, hi in vocals if hi > start and lo < end]
    last = max((hi for _, hi in heard), default=start)
    fade = min(maximum, max(0, end-max(start, last+padding)))
    return {'time': end, 'fade': fade, 'fadeStart': end-fade,
            'validated': False, 'proof': 'natural-ending-unverified',
            'knownVocalProtected': last <= end,
            'lastKnownVocalEnd': last if heard else None}
