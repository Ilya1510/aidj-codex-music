"""Conservative local beat matching of frozen cuts; no extrapolated global grid.

Input observations contain measured beats in original source seconds and actual
source coverage. This module does not detect vocals or certify an instrumental
intro: the caller must gate eligible pairs separately.
"""
import math
import statistics

VERSION = 1
DOMINANT_PULSE_VERSION = 2
MIN_RATE = .97
MAX_RATE = 1.03
MIN_BEATS = 12
MIN_SPAN = 6.
MEDIAN_ERROR = .025
MAX_ERROR = .05


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _reject(reason):
    return {'accepted': False, 'reason': reason}


def local_fit(observation, start, end):
    """Fit consecutive observed beat times, only inside real decoded coverage."""
    if not isinstance(observation, dict) or not all(_number(x) for x in (start, end)) or start >= end:
        return _reject('invalid-local-window')
    lo, hi = observation.get('sourceOffsetSeconds'), observation.get('sourceEndSeconds')
    beats = observation.get('beats')
    if (not all(_number(x) for x in (lo, hi)) or lo < 0 or hi <= lo or
            not isinstance(beats, list) or not 1 <= len(beats) <= 2000 or
            any(not _number(x) for x in beats) or
            any(b <= a for a, b in zip(beats, beats[1:])) or
            beats[0] < lo or beats[-1] > hi):
        return _reject('invalid-observed-beats')
    if start < lo-1e-9 or end > hi+1e-9:
        return _reject('outside-observed-coverage')
    selected = [b for b in beats if start <= b <= end]
    if len(selected) < MIN_BEATS or selected[-1]-selected[0] < MIN_SPAN:
        return _reject('insufficient-observed-beats')
    intervals = [b-a for a, b in zip(selected, selected[1:])]
    median_period = statistics.median(intervals)
    outliers = sum(abs(x/median_period-1) > .25 for x in intervals)
    if outliers/len(intervals) > .10:
        return _reject('unstable-beat-intervals')
    count = len(selected)
    mean_x = (count-1)/2
    mean_y = statistics.mean(selected)
    period = (sum((i-mean_x)*(b-mean_y) for i, b in enumerate(selected)) /
              sum((i-mean_x)**2 for i in range(count)))
    if period <= 0 or not 60 <= 60/period <= 210:
        return _reject('unsupported-local-tempo')
    intercept = mean_y-period*mean_x
    errors = [abs(b-(intercept+period*i)) for i, b in enumerate(selected)]
    if statistics.median(errors) > MEDIAN_ERROR or max(errors) > MAX_ERROR:
        return _reject('unstable-local-phase')
    return {'accepted': True, 'version': VERSION, 'bpm': 60/period,
            'periodSeconds': period, 'interceptSeconds': intercept,
            'medianErrorSeconds': statistics.median(errors), 'maxErrorSeconds': max(errors),
            'intervalOutlierFraction': outliers/len(intervals), 'beats': selected,
            'sourceOffsetSeconds': lo, 'sourceEndSeconds': hi,
            'windowStart': start, 'windowEnd': end}


def dominant_pulse_fit(observation, start, end):
    """Explicit V2: fit a dominant pulse while retaining every rejected onset.

    Missing pulses are recorded, never manufactured as observed beats. This
    can recognize a regular drum pulse despite occasional additional accents;
    it does not establish that every musical accent follows that pulse.
    """
    strict = local_fit(observation, start, end)
    if strict.get('reason') in ('invalid-local-window', 'invalid-observed-beats',
                               'outside-observed-coverage', 'insufficient-observed-beats'):
        return strict
    if (observation.get('onsetSupportedFraction') is not None and
            (not _number(observation['onsetSupportedFraction']) or
             not .8 <= observation['onsetSupportedFraction'] <= 1)):
        return _reject('insufficient-onset-supported-fraction')
    selected = [b for b in observation['beats'] if start <= b <= end]
    intervals = [b-a for a, b in zip(selected, selected[1:])]
    initial_period = statistics.median(intervals)
    if not 60-1e-8 <= 60/initial_period <= 210+1e-8:
        return _reject('unsupported-dominant-pulse-tempo')
    periods = []
    for i, a in enumerate(selected):
        for b in selected[i+1:]:
            span = b-a
            if span < MIN_SPAN:
                continue
            count = round(span/initial_period)
            if count and .92 <= (span/count)/initial_period <= 1.08:
                periods.append(span/count)
    if not periods:
        return _reject('insufficient-long-pulse-spans')
    periods.sort()
    # A bounded set of robust long-span hypotheses, independent of metadata BPM.
    candidates = sorted({periods[round((len(periods)-1)*q/10)] for q in range(11)})

    def choose(period, phase):
        pulse = {}
        for point in selected:
            index = round((point-phase)/period)
            error = abs(point-(phase+index*period))
            if error <= MAX_ERROR+1e-12 and (index not in pulse or error < pulse[index][1]):
                pulse[index] = (point, error)
        return pulse

    accepted = []
    for seed_period in candidates:
        for anchor in selected:
            period = seed_period
            phase = anchor
            pulse = choose(period, phase)
            for _ in range(3):
                if len(pulse) < MIN_BEATS:
                    break
                mean_x = statistics.mean(pulse)
                mean_y = statistics.mean(point for point, error in pulse.values())
                denom = sum((index-mean_x)**2 for index in pulse)
                period = sum((index-mean_x)*(point-mean_y) for index, (point, error) in pulse.items())/denom
                phase = mean_y-period*mean_x
                if not .92 <= period/initial_period <= 1.08:
                    pulse = {}
                    break
                pulse = choose(period, phase)
            if len(pulse) < MIN_BEATS or len(pulse)/len(selected) < .8 or not 60-1e-8 <= 60/period <= 210+1e-8:
                continue
            ordered = sorted(pulse)
            times = [pulse[index][0] for index in ordered]
            if times[-1]-times[0] < MIN_SPAN or max(b-a for a, b in zip(ordered, ordered[1:])) > 2:
                continue
            occupancy = len(pulse)/(ordered[-1]-ordered[0]+1)
            errors = [pulse[index][1] for index in ordered]
            if occupancy < .8 or statistics.median(errors) > MEDIAN_ERROR or max(errors) > MAX_ERROR:
                continue
            kept = set(times)
            discarded = []
            for point in selected:
                if point in kept:
                    continue
                index = round((point-phase)/period)
                predicted = phase+index*period
                discarded.append({'sourceSeconds': point, 'nearestPulseSeconds': predicted,
                                  'errorSeconds': abs(point-predicted),
                                  'reason': 'duplicate-pulse-accent' if index in pulse and abs(point-predicted) <= MAX_ERROR else 'outside-dominant-pulse'})
            accepted.append({'accepted': True, 'version': DOMINANT_PULSE_VERSION,
                             'method': 'dominant-observed-pulse', 'bpm': min(210., max(60., 60/period)),
                             'periodSeconds': period, 'interceptSeconds': phase,
                             'medianErrorSeconds': statistics.median(errors), 'maxErrorSeconds': max(errors),
                             'beats': times, 'observedBeats': selected,
                             'pulseIndices': ordered,
                             'missingPulseIndices': [index for index in range(ordered[0], ordered[-1]+1) if index not in pulse],
                             'discardedObservedBeats': discarded,
                             'observedSupportFraction': len(pulse)/len(selected),
                             'pulseOccupancyFraction': occupancy,
                             'maximumObservedPulseGap': max(b-a for a, b in zip(ordered, ordered[1:])),
                             'sourceOffsetSeconds': observation['sourceOffsetSeconds'],
                             'sourceEndSeconds': observation['sourceEndSeconds'],
                             'windowStart': start, 'windowEnd': end,
                             'allAccentsMatched': False})
    if not accepted:
        return _reject('no-stable-dominant-observed-pulse')
    return min(accepted, key=lambda fit: (-fit['observedSupportFraction'], -fit['pulseOccupancyFraction'],
                                           fit['medianErrorSeconds'], fit['maxErrorSeconds']))


def _role_fit(row, observation, role, *, robust=False):
    if not isinstance(observation, dict):
        return _reject('missing-local-observation')
    if role in observation:
        observation = observation[role]
    if not isinstance(observation, dict) or observation.get('md5') != row.get('md5'):
        return _reject('different-source-md5')
    lo, hi = observation.get('sourceOffsetSeconds'), observation.get('sourceEndSeconds')
    if not all(_number(x) for x in (lo, hi)):
        return _reject('invalid-observed-coverage')
    point = row['start'] if role == 'entry' else row['end']
    # Twenty-second native contexts can be asymmetric at source edges. Clip
    # the fitting window to what was decoded; never synthesize beat positions.
    start, end = (point-2, point+16) if role == 'entry' else (point-16, point+2)
    return (dominant_pulse_fit if robust else local_fit)(observation, max(lo, start), min(hi, end))


def _phase_candidate(left, right, left_fit, right_fit, left_rate, right_rate, target, tolerance):
    left_beats, right_beats = left_fit['beats'], right_fit['beats']
    incoming = [b for b in right_beats if right['start'] <= b <= right['start']+2*right_rate]
    candidates = []
    for right_beat in incoming:
        for left_beat in left_beats:
            overlap = ((left['end']-left_beat)/left_rate +
                       (right_beat-right['start'])/right_rate)
            if not target-tolerance <= overlap <= target+tolerance:
                continue
            if overlap > min(8., (left['end']-left['start'])/2,
                             (right['end']-right['start'])/2):
                continue
            left_start = left['end']-overlap*left_rate
            right_end = right['start']+overlap*right_rate
            if (left_start < left_fit['sourceOffsetSeconds']-1e-9 or
                    left['end'] > left_fit['sourceEndSeconds']+1e-9 or
                    right['start'] < right_fit['sourceOffsetSeconds']-1e-9 or
                    right_end > right_fit['sourceEndSeconds']+1e-9):
                continue
            a = [(b, (b-left_start)/left_rate) for b in left_beats if left_start <= b <= left['end']]
            b = [(x, (x-right['start'])/right_rate) for x in right_beats if right['start'] <= x <= right_end]
            if len(a) < 6 or len(b) < 6:
                continue
            if min(a[-1][1]-a[0][1], b[-1][1]-b[0][1]) < overlap-.9:
                continue
            # Every central observed beat must have a real counterpart. Only
            # the first/last50ms can differ because of interval endpoint rounding.
            central_a = [x for x in a if .05 <= x[1] <= overlap-.05]
            central_b = [x for x in b if .05 <= x[1] <= overlap-.05]
            matches = [(x[0], min(b, key=lambda y: abs(y[1]-x[1]))[0],
                        min(abs(y[1]-x[1]) for y in b)) for x in central_a]
            errors = [x[2] for x in matches]
            errors.extend(min(abs(y[1]-x[1]) for y in a) for x in central_b)
            if (len(matches) < 6 or not errors or max(errors) > MAX_ERROR or
                    statistics.median(errors) > MEDIAN_ERROR):
                continue
            candidates.append({'beatMatched': True, 'version': VERSION,
                               'transitionSeconds': overlap,
                               'leftAnchorSeconds': left_beat, 'rightAnchorSeconds': right_beat,
                               'leftRate': left_rate, 'rightRate': right_rate,
                               'leftOverlapStartSeconds': left_start,
                               'rightOverlapEndSeconds': right_end,
                               'matchedObservedBeats': matches,
                               'medianPhaseErrorSeconds': statistics.median(errors),
                               'maxPhaseErrorSeconds': max(errors),
                               'leftLocalBpm': left_fit['bpm'], 'rightLocalBpm': right_fit['bpm']})
    if not candidates:
        return {'beatMatched': False, 'reason': 'no-observed-phase-match-near-target'}
    return min(candidates, key=lambda p: (abs(p['transitionSeconds']-target),
                                         p['maxPhaseErrorSeconds'], p['medianPhaseErrorSeconds']))


def _dominant_phase_candidate(left, right, left_fit, right_fit, left_rate, right_rate, target, tolerance):
    """V2 pairs only real inlier pulses; unmatched pulses remain explicit."""
    candidates = []
    for right_beat in right_fit['beats']:
        if not right['start'] <= right_beat <= right['start']+2*right_rate:
            continue
        for left_beat in left_fit['beats']:
            overlap = ((left['end']-left_beat)/left_rate + (right_beat-right['start'])/right_rate)
            if not target-tolerance <= overlap <= target+tolerance:
                continue
            if overlap > min(8., (left['end']-left['start'])/2, (right['end']-right['start'])/2):
                continue
            left_start = left['end']-overlap*left_rate
            right_end = right['start']+overlap*right_rate
            if (left_start < left_fit['sourceOffsetSeconds']-1e-9 or
                    left['end'] > left_fit['sourceEndSeconds']+1e-9 or
                    right['start'] < right_fit['sourceOffsetSeconds']-1e-9 or
                    right_end > right_fit['sourceEndSeconds']+1e-9):
                continue
            a = [(b, (b-left_start)/left_rate) for b in left_fit['beats'] if left_start <= b <= left['end']]
            b = [(x, (x-right['start'])/right_rate) for x in right_fit['beats'] if right['start'] <= x <= right_end]
            central_a = [x for x in a if .05 <= x[1] <= overlap-.05]
            central_b = [x for x in b if .05 <= x[1] <= overlap-.05]
            if min(len(central_a), len(central_b)) < 6:
                continue
            matches = []
            for point, wall in central_a:
                partner = min(b, key=lambda x: abs(x[1]-wall))
                error = abs(partner[1]-wall)
                if error <= MAX_ERROR:
                    matches.append((point, partner[0], error))
            matched_a, matched_b = {x[0] for x in matches}, {x[1] for x in matches}
            a_fraction = len(matched_a)/len(central_a)
            b_fraction = sum(x[0] in matched_b for x in central_b)/len(central_b)
            if len(matches) < 6 or min(a_fraction, b_fraction) < .8:
                continue
            left_span = (max(matched_a)-min(matched_a))/left_rate
            right_span = (max(matched_b)-min(matched_b))/right_rate
            if min(left_span, right_span) < overlap-.9:
                continue
            errors = [x[2] for x in matches]
            if statistics.median(errors) > MEDIAN_ERROR or max(errors) > MAX_ERROR:
                continue
            candidates.append({'beatMatched': True, 'version': DOMINANT_PULSE_VERSION,
                               'method': 'dominant-observed-pulse-matching',
                               'transitionSeconds': overlap,
                               'leftAnchorSeconds': left_beat, 'rightAnchorSeconds': right_beat,
                               'leftRate': left_rate, 'rightRate': right_rate,
                               'leftOverlapStartSeconds': left_start, 'rightOverlapEndSeconds': right_end,
                               'matchedObservedBeats': matches,
                               'unmatchedLeftObservedBeats': [x[0] for x in central_a if x[0] not in matched_a],
                               'unmatchedRightObservedBeats': [x[0] for x in central_b if x[0] not in matched_b],
                               'leftMatchedPulseFraction': a_fraction, 'rightMatchedPulseFraction': b_fraction,
                               'leftMatchedSpanSeconds': left_span, 'rightMatchedSpanSeconds': right_span,
                               'medianPhaseErrorSeconds': statistics.median(errors), 'maxPhaseErrorSeconds': max(errors),
                               'leftLocalBpm': left_fit['bpm'], 'rightLocalBpm': right_fit['bpm'],
                               'allAccentsMatched': False})
    if not candidates:
        return {'beatMatched': False, 'reason': 'no-dominant-observed-pulse-match-near-target'}
    return min(candidates, key=lambda p: (abs(p['transitionSeconds']-target),
                                         p['maxPhaseErrorSeconds'], p['medianPhaseErrorSeconds']))


def plan_rhythm(rows, observations, target_seconds=5., tolerance_seconds=.4, *, eligible=None, robust=False):
    """Plan bounded constant track rates for adjacent eligible compatible runs.

    `eligible` may map outgoing track IDs to booleans. Without it, only outgoing
    rows already requesting >=4.5s are eligible. Unmatched pairs retain their
    original transition; the caller chooses its conservative fallback.
    """
    if (not isinstance(rows, list) or not rows or not isinstance(observations, dict) or
            eligible is not None and not isinstance(eligible, dict) or
            not _number(target_seconds) or not 1 <= target_seconds <= 8 or
            not _number(tolerance_seconds) or not 0 <= tolerance_seconds <= .5):
        raise ValueError('RHYTHM_PLAN_INPUT')
    result = [dict(row) for row in rows]
    ids = [row.get('trackId') for row in rows]
    if len(set(ids)) != len(ids) or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError('RHYTHM_PLAN_TRACK_IDS')
    if any(not all(_number(row.get(k)) for k in ('start', 'end', 'rate', 'transitionSeconds')) or
           row['start'] >= row['end'] or not MIN_RATE <= row['rate'] <= MAX_RATE for row in rows):
        raise ValueError('RHYTHM_PLAN_ROWS')
    if type(robust) is not bool:
        raise ValueError('RHYTHM_PLAN_POLICY')
    fits = [{role: _role_fit(row, observations.get(row['trackId']), role, robust=robust)
             for role in ('entry', 'exit')} for row in rows]
    allowed = [(eligible.get(row['trackId']) is True if eligible is not None
                else row['transitionSeconds'] >= 4.5) for row in rows[:-1]]
    pairs = [{'leftTrackId': a['trackId'], 'rightTrackId': b['trackId'],
              'beatMatched': False, 'reason': 'ineligible-overlap'} for a, b in zip(rows, rows[1:])]
    runs = []
    i = 0
    while i < len(rows)-1:
        if not allowed[i] or not fits[i]['exit']['accepted'] or not fits[i+1]['entry']['accepted']:
            if allowed[i]:
                pairs[i]['reason'] = 'missing-or-unstable-local-rhythm'
            i += 1
            continue
        begin = i
        tempos = [fits[i]['exit']['bpm'], fits[i+1]['entry']['bpm']]
        low = max(t*MIN_RATE for t in tempos)
        high = min(t*MAX_RATE for t in tempos)
        if low > high:
            pairs[i]['reason'] = 'incompatible-local-tempos'
            i += 1
            continue
        finish = i+1
        while finish < len(rows)-1 and allowed[finish] and fits[finish]['exit']['accepted'] and fits[finish+1]['entry']['accepted']:
            additional = [fits[finish]['exit']['bpm'], fits[finish+1]['entry']['bpm']]
            new_low = max(low, *(t*MIN_RATE for t in additional))
            new_high = min(high, *(t*MAX_RATE for t in additional))
            if new_low > new_high:
                pairs[finish]['reason'] = 'incompatible-with-preceding-rate-run'
                break
            low, high = new_low, new_high
            tempos.extend(additional)
            finish += 1
        target = min(high, max(low, statistics.median(tempos)))
        rates = {}
        for j in range(begin, finish+1):
            available = []
            if j > begin:
                available.append(fits[j]['entry']['bpm'])
            if j < finish:
                available.append(fits[j]['exit']['bpm'])
            rates[j] = min(MAX_RATE, max(MIN_RATE, target/statistics.mean(available)))
        used = set()
        for j in range(begin, finish):
            matcher = _dominant_phase_candidate if robust else _phase_candidate
            proof = matcher(rows[j], rows[j+1], fits[j]['exit'], fits[j+1]['entry'],
                                     rates[j], rates[j+1], target_seconds, tolerance_seconds)
            pairs[j].update(proof)
            if proof['beatMatched']:
                pairs[j].pop('reason', None)
                result[j]['transitionSeconds'] = proof['transitionSeconds']
                result[j]['transitionKind'] = 'crossfade'
                used.update((j, j+1))
        for j in used:
            result[j]['rate'] = rates[j]
        runs.append({'firstIndex': begin, 'lastIndex': finish, 'targetBpm': target,
                     'minimumBpm': low, 'maximumBpm': high, 'matchedPairs': sum(pairs[j]['beatMatched'] for j in range(begin, finish))})
        if finish < len(pairs) and allowed[finish] and pairs[finish]['reason'] == 'ineligible-overlap':
            pairs[finish]['reason'] = 'missing-or-unstable-local-rhythm'
        # The next incompatible boundary starts a new group, but a track has
        # one constant rate. Do not overwrite the shared terminal track's rate.
        i = finish+1
    return {'version': DOMINANT_PULSE_VERSION if robust else VERSION, 'tracks': result, 'pairs': pairs, 'runs': runs,
            'fits': {aid: value for aid, value in zip(ids, fits)}}
