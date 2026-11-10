"""One five-second transition style for an entire frozen DJ set.

Confidence describes the available rhythm evidence. It never selects a shorter
transition style. Unknown and irregular music still receives a long blend;
approximate onset alignment is explicitly separate from a verified pulse fit.
"""
import collections
import math
import statistics

from .fixed_set_blend import artist_keys
from .fixed_set_rhythm import (MIN_RATE, MAX_RATE, _role_fit,
                               _dominant_phase_candidate)
from .fixed_sets import signature, validate

VERSION = 1


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _feature(row, features):
    value = features.get(row['trackId'], features.get(row['md5'], {}))
    return value if isinstance(value, dict) and value.get('md5', row['md5']) == row['md5'] else {}


def _observation(row, observations, role):
    value = observations.get(row['trackId'], {})
    if isinstance(value, dict) and role in value:
        value = value[role]
    return value if isinstance(value, dict) and value.get('md5') == row['md5'] else {}


def _tempo(row, role, fit, observations, assets, features):
    if fit.get('accepted'):
        return fit['bpm'], 'observed-local-pulse'
    observed = _observation(row, observations, role).get('estimatedBpm')
    if _number(observed) and 60 <= observed <= 210:
        return observed, 'local-estimate-only'
    # A global BPM helps order unknown local passages, but cannot certify phase.
    for value in (_feature(row, features).get('bpm'), assets[row['trackId']].get('bpm')):
        if _number(value) and 60 <= value <= 210:
            return value, 'global-ordering-estimate-only'
    return None, 'unknown'


def _chroma(row, features):
    value = _feature(row, features).get('chroma')
    if not isinstance(value, list) or len(value) != 12 or any(not _number(x) or x < 0 for x in value):
        return None
    norm = math.sqrt(sum(x*x for x in value))
    return [x/norm for x in value] if norm > 1e-10 else None


def _representative(tempos):
    values = [v[0] for v in tempos.values() if v[0] is not None]
    return math.exp(statistics.mean(math.log(v) for v in values)) if values else None


def _guide(rows, tempos, opening):
    """Walk the lower-tempo side out and back using alternating tracks.

    An ordinary ascending sort with a fixed interior opening produces one huge
    high-to-low wrap. Alternating the lower half keeps both legs gradual.
    """
    known = sorted((r for r in rows if _representative(tempos[r['trackId']]) is not None),
                   key=lambda r: (_representative(tempos[r['trackId']]), r['trackId']))
    unknown = [r for r in rows if _representative(tempos[r['trackId']]) is None]
    pivot = _representative(tempos[opening['trackId']])
    if pivot is None:
        return [r for r in known+unknown if r['trackId'] != opening['trackId']]
    lower = [r for r in known if r['trackId'] != opening['trackId'] and _representative(tempos[r['trackId']]) < pivot]
    upper = [r for r in known if r['trackId'] != opening['trackId'] and _representative(tempos[r['trackId']]) >= pivot]
    descending = lower[::-1]
    return descending[::2] + descending[1::2][::-1] + upper + unknown


def _order(rows, assets, tempos, fits, features, opening_id, lookahead):
    by_id = {r['trackId']: r for r in rows}
    opening = by_id.get(opening_id) if opening_id else rows[0]
    if opening is None:
        raise ValueError('UNIFORM_OPENING_TRACK')
    keys = {r['trackId']: artist_keys(assets[r['trackId']]) for r in rows}
    if any(not values for values in keys.values()):
        raise ValueError('UNIFORM_UNKNOWN_ARTIST')
    colors = {r['trackId']: _chroma(r, features) for r in rows}
    pending = _guide(rows, tempos, opening)
    ordered = [opening]
    counts = collections.Counter(k for r in pending for k in keys[r['trackId']])
    decisions = []
    while pending:
        last = ordered[-1]; left_id = last['trackId']
        blocked = keys[left_id] & keys[ordered[-2]['trackId']] if len(ordered) > 1 else set()
        remaining_count = len(pending)-1
        # Only near-dominant remaining artists can violate max-two feasibility.
        critical = [k for k, n in counts.items() if 3*n > 2*remaining_count]

        def permitted(row):
            current = keys[row['trackId']]
            if current & blocked:
                return False
            for key in critical:
                after = counts[key]-(key in current)
                first_capacity = (0 if key in keys[left_id] else 1) if key in current else 2
                if after > 2*(remaining_count-after)+first_capacity:
                    return False
            return True

        def distance(row, position):
            right_id = row['trackId']
            a, b = tempos[left_id]['exit'][0], tempos[right_id]['entry'][0]
            tempo = abs(math.log(a/b)) if a and b else .18
            left_c, right_c = colors[left_id], colors[right_id]
            harmony = 1-sum(x*y for x, y in zip(left_c, right_c)) if left_c and right_c else .5
            uncertainty = (not fits[left_id]['exit'].get('accepted')) + (not fits[right_id]['entry'].get('accepted'))
            left_group, right_group = _feature(last, features).get('group'), _feature(row, features).get('group')
            group_change = bool(left_group and right_group and left_group != right_group)
            # Tempo dominates ranking; global chroma is a gentle ordering cue.
            return (tempo*12 + harmony*.18 + uncertainty*.10 + group_change*.08 +
                    position*.003 + bool(keys[left_id] & keys[right_id])*.02)

        candidates = [(distance(row, i), i) for i, row in enumerate(pending[:lookahead]) if permitted(row)]
        if not candidates:
            candidates = [(distance(row, i), i) for i, row in enumerate(pending) if permitted(row)]
        if not candidates:
            raise ValueError('UNIFORM_ARTIST_LIMIT_IMPOSSIBLE')
        score, at = min(candidates)
        chosen = pending.pop(at)
        for key in keys[chosen['trackId']]:
            counts[key] -= 1
        ordered.append(chosen)
        decisions.append({'leftTrackId': left_id, 'rightTrackId': chosen['trackId'],
                          'guidePosition': at, 'score': score})
    return ordered, decisions


def _rates(rows, tempos):
    """Minimize actual adjacent exit/entry tempo error with one rate per track.

    A representative tempo band can pull the two sides of a band seam apart.
    Instead solve the bounded convex log-tempo objective on the actual ordered
    edges. A small preference for normal speed prevents unnecessary global drift.
    No rate varies during a song; phase is verified afterwards at final rates.
    """
    edges = []
    for left, right in zip(rows, rows[1:]):
        a = tempos[left['trackId']]['exit'][0]
        b = tempos[right['trackId']]['entry'][0]
        edges.append(math.log(a/b) if a and b else None)
    speeds = [0.] * len(rows)
    low, high, regularization = math.log(MIN_RATE), math.log(MAX_RATE), .01
    sweeps = 0
    for sweeps in range(2000):
        delta = 0.
        for indices in (range(len(rows)), range(len(rows)-1, -1, -1)):
            for i in indices:
                total, count = 0., regularization
                if i and edges[i-1] is not None:
                    total += speeds[i-1] + edges[i-1]
                    count += 1
                if i < len(edges) and edges[i] is not None:
                    total += speeds[i+1] - edges[i]
                    count += 1
                speed = min(high, max(low, total/count))
                delta = max(delta, abs(speed-speeds[i]))
                speeds[i] = speed
        if delta < 1e-11:
            break
    for row, speed in zip(rows, speeds):
        row['rate'] = min(MAX_RATE, max(MIN_RATE, math.exp(speed)))
    return [{'basis': 'bounded-adjacent-local-exit-entry-log-tempo',
             'regularization': regularization, 'sweeps': sweeps+1,
             'unitRateSquaredError': sum(d*d for d in edges if d is not None),
             'finalRateSquaredError': sum((d+speeds[i]-speeds[i+1])**2
                                          for i, d in enumerate(edges) if d is not None)}]


def _actual_onsets(row, observations, role):
    observation = _observation(row, observations, role)
    values = observation.get('beats')
    lo, hi = observation.get('sourceOffsetSeconds'), observation.get('sourceEndSeconds')
    if (not _number(lo) or not _number(hi) or lo < 0 or hi <= lo or
            not isinstance(values, list) or not values or len(values) > 2000 or
            any(not _number(v) for v in values) or
            any(b <= a for a, b in zip(values, values[1:])) or
            values[0] < lo or values[-1] > hi):
        return None
    return observation


def _approximate_phase(left, right, observations, target, tolerance):
    """Align real onset anchors without promoting them to verified beats."""
    a, b = _actual_onsets(left, observations, 'exit'), _actual_onsets(right, observations, 'entry')
    if a is None or b is None:
        return None
    candidates = []
    for incoming in b['beats']:
        if not right['start'] <= incoming <= right['start']+1.5*right['rate']:
            continue
        for outgoing in a['beats']:
            seconds = (left['end']-outgoing)/left['rate'] + (incoming-right['start'])/right['rate']
            if not target-tolerance <= seconds <= target+tolerance:
                continue
            left_start = left['end']-seconds*left['rate']
            right_end = right['start']+seconds*right['rate']
            if (left_start < a['sourceOffsetSeconds'] or left['end'] > a['sourceEndSeconds'] or
                    right['start'] < b['sourceOffsetSeconds'] or right_end > b['sourceEndSeconds']):
                continue
            left_times = [(x-left_start)/left['rate'] for x in a['beats'] if left_start <= x <= left['end']]
            right_times = [(x-right['start'])/right['rate'] for x in b['beats'] if right['start'] <= x <= right_end]
            if min(len(left_times), len(right_times)) < 3:
                continue
            errors = [min(abs(x-y) for y in right_times) for x in left_times]
            errors += [min(abs(x-y) for y in left_times) for x in right_times]
            median = statistics.median(errors)
            candidates.append({'transitionSeconds': seconds, 'leftObservedAnchorSeconds': outgoing,
                               'rightObservedAnchorSeconds': incoming,
                               'medianNearestObservedOnsetErrorSeconds': median,
                               'allOnsetsMatched': False})
    return min(candidates, key=lambda c: (c['medianNearestObservedOnsetErrorSeconds'] +
                                        abs(c['transitionSeconds']-target)*.05,
                                        abs(c['transitionSeconds']-target))) if candidates else None


def uniform_program(value, assets, observations, ranges=None, features=None, *, opening_id=None,
                    target_seconds=5., tolerance_seconds=.25, lookahead=16, preserve_order=False):
    """Use the selected long-blend style at every boundary, regardless of confidence.

    Source cuts and authored ending fades are immutable. Unknown tempo, weak
    rhythm, incompatible speeds, and EOF uncertainty remain explicit audit
    outcomes while the audible transition remains approximately five seconds.
    """
    original = validate(value, assets, ranges)
    if (not isinstance(observations, dict) or features is not None and not isinstance(features, dict) or not _number(target_seconds) or
            not 4.75 <= target_seconds <= 5.25 or not _number(tolerance_seconds) or
            not 0 <= tolerance_seconds <= .25 or target_seconds-tolerance_seconds < 4.75 or
            target_seconds+tolerance_seconds > 5.25 or type(lookahead) is not int or not 1 <= lookahead <= 64 or
            type(preserve_order) is not bool):
        raise ValueError('UNIFORM_PARAMETERS')
    rows = [dict(row) for row in original['tracks']]
    if any(row['end']-row['start'] < 2*(target_seconds+tolerance_seconds) for row in rows):
        raise ValueError('UNIFORM_EXCERPT_TOO_SHORT')
    features = features or {}
    fits = {row['trackId']: {role: _role_fit(row, observations.get(row['trackId']), role, robust=True)
                            for role in ('entry', 'exit')} for row in rows}
    tempos = {row['trackId']: {role: _tempo(row, role, fits[row['trackId']][role], observations, assets, features)
                              for role in ('entry', 'exit')} for row in rows}
    if preserve_order:
        keys = [artist_keys(assets[r['trackId']]) for r in rows]
        if any(not k for k in keys) or any(a & b & c for a, b, c in zip(keys, keys[1:], keys[2:])):
            raise ValueError('UNIFORM_ARTIST_LIMIT_IMPOSSIBLE')
        ordered, decisions = rows, []
    else:
        ordered, decisions = _order(rows, assets, tempos, fits, features, opening_id, lookahead)
    bands = _rates(ordered, tempos)
    pairs = []
    for i, row in enumerate(ordered):
        row['fadeIn'] = 5.
        row['transitionSeconds'] = target_seconds if i < len(ordered)-1 else 0.
        row['transitionKind'] = 'crossfade' if i < len(ordered)-1 else 'clean-cut'
        if i == len(ordered)-1:
            break
        right = ordered[i+1]
        left_fit, right_fit = fits[row['trackId']]['exit'], fits[right['trackId']]['entry']
        asset = assets[row['trackId']]
        eof_clock = abs(row['end']-asset['duration']) <= .001
        reason = ('decoder-eof-clock-unverified' if eof_clock else
                  'missing-or-unstable-local-rhythm' if not (left_fit.get('accepted') and right_fit.get('accepted'))
                  else 'no-confirmed-observed-phase-match')
        proof = None
        if not eof_clock and left_fit.get('accepted') and right_fit.get('accepted'):
            candidate = _dominant_phase_candidate(row, right, left_fit, right_fit,
                                                 row['rate'], right['rate'], target_seconds, tolerance_seconds)
            if candidate.get('beatMatched'):
                proof = candidate
        pair = {'leftTrackId': row['trackId'], 'rightTrackId': right['trackId'],
                'leftRate': row['rate'], 'rightRate': right['rate'],
                'leftTempoBasis': tempos[row['trackId']]['exit'][1],
                'rightTempoBasis': tempos[right['trackId']]['entry'][1],
                'beatMatched': proof is not None, 'verifiedWordBoundary': False,
                'quietSideRequired': False}
        if proof is not None:
            pair.update(proof)
            pair['confidence'] = 'confirmed-observed-pulse'
            row['transitionSeconds'] = proof['transitionSeconds']
        else:
            approximation = _approximate_phase(row, right, observations, target_seconds, tolerance_seconds)
            pair.update(confidence='best-effort', reason=reason,
                        phaseAdjustmentKind='real-observed-onset-approximation' if approximation else 'fixed-five-second-envelope')
            if approximation:
                row['transitionSeconds'] = approximation['transitionSeconds']
                pair['approximation'] = approximation
        pair['transitionSeconds'] = row['transitionSeconds']
        pairs.append(pair)
    plan = validate({**original, 'tracks': ordered, 'revision': signature(ordered)}, assets, ranges)
    return {'version': VERSION, 'plan': plan, 'pairs': pairs, 'fits': fits,
            'tempoBases': tempos, 'ordering': decisions, 'tempoBands': bands,
            'summary': {'tracks': len(ordered), 'longTransitions': len(pairs),
                        'confirmedObservedPulseTransitions': sum(p['beatMatched'] for p in pairs),
                        'bestEffortTransitions': sum(not p['beatMatched'] for p in pairs),
                        'shortFallbackTransitions': 0, 'maximumConsecutiveArtist': 2}}
