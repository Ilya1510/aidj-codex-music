"""Pure replay of a lower-confidence, jointly corroborated incoming boundary.

UVR's less quiet 200 ms interval must agree with independently measured raw
stereo MIX speech probabilities. Every known ASR span remains a veto, including
low-confidence spans. This module neither runs models nor selects/publishes a
cut. Flat low model output is explicitly an approximate observation, never a
claim that a word boundary or absence of singing has been proved.

Version 1 remains the default with a .01 MIX cap. Explicit version 2 uses .05
with every other condition unchanged. Explicit version 3 shortens only the
UVR/MIX body before the point from .15 to .1 seconds; the earlier rise context
remains -.15 to -.02 seconds. Its small owner-label calibration is
descriptive evidence, not a guarantee about future boundaries or singing.
"""
import hashlib
import json
import math
from pathlib import Path
import re

from .fixed_set_instrumental_entry import validate_mix
from .fixed_set_repair import covered
from .fixed_set_resolve import read_json
from .fixed_set_vocal_review import (
    VERSION as VOCAL_CACHE_VERSION, _vectors, finite, gain_db, quantile,
)
from .vocal_probe import MODEL_SHA256
from .vocal_speech import INPUT_RATE, MODEL_RATE, WINDOW

VERSION = 1
METHOD = 'low-voice-corroborated-entry'
PARAMETERS = {
    'beforeSeconds': .15, 'afterSeconds': .05,
    'vocalMaxDbfs': -30., 'vocalP95Dbfs': -33., 'mixSpeechMax': .01,
    'vocalContextGuardSeconds': .25, 'mixWarmupSeconds': 1.,
    'mixEndGuardSeconds': .25, 'asrCrossingMarginSeconds': .08,
    'mixWindowSeconds': 6., 'mixWindowPointLeadSeconds': 3.,
    'mixWindowPolicy': 'point-centered6s-shift-at-source-edges-no-context-selection',
    'asrCoverageBeforeSeconds': .3, 'asrCoverageAfterSeconds': .35,
    'risePreStartSeconds': -.15, 'risePreEndSeconds': -.02,
    'riseSearchStartSeconds': -.02, 'riseSearchEndSeconds': .35,
    'relativeRiseDb': 8., 'minimumObservedPrerollSeconds': .1,
    'maximumFadeInSeconds': .05, 'minimumFadeInSeconds': .005,
    'finishBeforeWordOrRiseSeconds': .03, 'negativeOwnerRadiusSeconds': .35,
}
VERSION_V2 = 2
PARAMETERS_V2 = {**PARAMETERS, 'mixSpeechMax': .05}
VERSION_V3 = 3
PARAMETERS_V3 = {**PARAMETERS_V2, 'beforeSeconds': .1}


def _policy(version):
    """Only three named contracts exist; no caller-supplied runtime threshold."""
    if type(version) is not int:
        return None
    if version == VERSION:
        return PARAMETERS
    if version == VERSION_V2:
        return PARAMETERS_V2
    if version == VERSION_V3:
        return PARAMETERS_V3
    return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def source_signature(song):
    fields = ('id', 'md5', 'bytes', 'duration', 'lo', 'hi', 'manualStart',
              'lufs', 'peak', 'loudnessMethod', 'loudnessStatus')
    return digest({key: song.get(key) for key in fields})


def centered_mix_bounds(point, duration):
    """The same single 6 s context used in owner calibration, sample-rounded.

    Recurrent VAD scores depend on preceding audio. A favorable score from a
    different overlapping excerpt must never substitute for this fixed input.
    """
    if not finite(point) or not finite(duration) or not 0 <= point < duration <= 1800:
        raise ValueError('invalid-centered-mix-bounds')
    begin = round(max(0., min(duration-6., point-3.))*INPUT_RATE)/INPUT_RATE
    end = round(min(duration, begin+6.)*INPUT_RATE)/INPUT_RATE
    return begin, end


def _key(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _source_valid(song):
    return (isinstance(song, dict) and isinstance(song.get('id'), str) and
        isinstance(song.get('md5'), str) and re.fullmatch('[a-f0-9]{32}', song['md5']) and
        type(song.get('bytes')) is int and 0 < song['bytes'] <= 40*1024**2 and
        all(finite(song.get(k)) for k in ('duration', 'lo', 'hi')) and
        all(song.get(k) is None or finite(song[k]) for k in ('lufs', 'peak')) and
        0 <= song['lo'] < song['hi'] <= song['duration']+.0001 and song['duration'] <= 1800)


def _observations_valid(words, coverage):
    return (isinstance(words, (list, tuple)) and isinstance(coverage, (list, tuple)) and
        all(isinstance(w, (list, tuple)) and len(w) == 3 and
            all(finite(v) for v in w[:2]) and 0 <= w[0] <= w[1] and type(w[2]) is bool
            for w in words) and
        all(isinstance(w, (list, tuple)) and len(w) == 2 and
            all(finite(v) for v in w) and 0 <= w[0] <= w[1] for w in coverage))


def _validated_vocal(vocal, song):
    if not isinstance(vocal, dict) or vocal.get('md5') != song['md5']:
        return None
    prepared = _vectors(vocal)
    if prepared is None:
        return None
    begin, _, duration, _ = prepared
    end = vocal.get('requestedEndSeconds')
    if (not finite(end) or not begin < end <= song['duration']+.0001 or
            abs(begin+duration-end) > .05+1e-7):
        return None
    expected = hashlib.sha256(json.dumps(
        [VOCAL_CACHE_VERSION, song['md5'], begin, end, MODEL_SHA256],
        separators=(',', ':')).encode()).hexdigest()
    return prepared if vocal.get('cacheKey') == expected else None


def _validated_mix(mix, song):
    try:
        speech = validate_mix(mix, song)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError):
        return None
    if speech is None or mix.get('audioRetained') is not False:
        return None
    # The canonical validator checks the complete real-frame and padding count.
    # Keep the final partial frame's explicit status consistent as well.
    last = speech['frameEndSeconds'][-1]-speech['frameStartSeconds'][-1]
    partial = last < WINDOW/MODEL_RATE-1e-12
    if (not finite(speech.get('lastFrameSeconds')) or
            abs(speech['lastFrameSeconds']-last) > 1e-8 or
            speech.get('lastFramePartial') is not partial or
            speech.get('lastFrameIncludesPadding') is not partial):
        return None
    return speech


def check_entry(vocal, mix, point, song, words, coverage, *, rejected=(), fade_in=.05):
    """Default version 1; existing proofs and callers retain the .01 MIX cap."""
    return _check_entry(vocal, mix, point, song, words, coverage,
        rejected=rejected, fade_in=fade_in, version=VERSION)


def check_entry_v2(vocal, mix, point, song, words, coverage, *, rejected=(), fade_in=.05):
    """Explicit version 2: .05 MIX cap, all UVR/ASR/preroll guards unchanged."""
    return _check_entry(vocal, mix, point, song, words, coverage,
        rejected=rejected, fade_in=fade_in, version=VERSION_V2)


def check_entry_v3(vocal, mix, point, song, words, coverage, *, rejected=(), fade_in=.05):
    """Explicit version 3: 100 ms prebody, otherwise exact version 2 guards."""
    return _check_entry(vocal, mix, point, song, words, coverage,
        rejected=rejected, fade_in=fade_in, version=VERSION_V3)


def _check_entry(vocal, mix, point, song, words, coverage, *, rejected, fade_in, version):
    """Reconstruct both raw proofs; do not infer text, erase words or run audio."""
    reject = lambda reason: {'accepted': False, 'method': METHOD, 'reason': reason}
    parameters = _policy(version)
    if parameters is None:
        return reject('unknown-soft-policy-version')
    if (not _source_valid(song) or not finite(point) or
            not song['lo'] <= point < song['hi'] or
            not finite(fade_in) or fade_in < .005):
        return reject('invalid-source-point-or-fade')
    if (not isinstance(rejected, (list, tuple)) or any(not finite(t) for t in rejected)):
        return reject('invalid-owner-rejections')
    if any(abs(point-t) < .35 for t in rejected):
        return reject('owner-rejected-entry')
    if not _observations_valid(words, coverage):
        return reject('invalid-retained-ASR')
    if not covered(coverage, point-.3, point+.35):
        return reject('ASR-coverage-unknown')
    if any(a < point+.08-1e-7 and b > point-.08+1e-7 for a, b, _ in words):
        return reject('known-word-crosses-entry-guard')
    prepared = _validated_vocal(vocal, song)
    if prepared is None:
        return reject('invalid-raw-vocal-evidence')
    offset, hop, duration, values = prepared
    if (point-.3 < max(song['lo'], offset+.25)-1e-7 or
            point+.35 > min(song['hi'], offset+duration-.25)+1e-7):
        return reject('incomplete-vocal-context')
    a, b = point-parameters['beforeSeconds'], point+parameters['afterSeconds']

    def region(lo, hi):
        first = math.floor((lo-offset)/hop+1e-8)
        last = math.ceil((hi-offset)/hop-1e-8)
        if (not 0 <= first < last <= len(values[0]) or
                offset+first*hop > lo+1e-7 or offset+last*hop < hi-1e-7):
            return None
        return first, last

    measured = region(a, b)
    before = region(point-.15, point-.02)
    rising = region(point-.02, point+.35)
    if None in (measured, before, rising):
        return reject('incomplete-vocal-frames')
    first, last = measured
    level = gain_db(song)
    db = lambda value: 20*math.log10(max(1e-10, value))+level
    voice = values[1][first:last]
    maximum, p95 = db(max(voice)), db(quantile(voice, .95))
    if maximum > -30 or p95 > -33:
        return reject('vocal-above-soft-ceiling')
    pre_p95 = db(quantile(values[1][before[0]:before[1]], .95))
    threshold = pre_p95+8
    rise = next((i for i in range(*rising) if db(values[1][i]) >= threshold), None)
    rise_time = offset+rise*hop if rise is not None else None
    rise_delay = rise_time-point if rise_time is not None else None
    if rise_delay is not None and rise_delay < .1-1e-7:
        return reject('observed-voice-preroll-under100ms')
    future = sorted((w for w in words if w[0] >= point-1e-7), key=lambda w: (w[0], w[1]))
    next_word = list(future[0]) if future else None
    same_start = [list(w) for w in future if abs(w[0]-next_word[0]) < 1e-7] if future else []
    next_delay = next_word[0]-point if next_word is not None else None
    if next_delay is not None and next_delay < .1-1e-7:
        return reject('observed-word-preroll-under100ms')
    cap = min(.05, rise_delay-.03 if rise_delay is not None else .05,
              next_delay-.03 if next_delay is not None else .05)
    fade = round(min(fade_in, cap), 4)
    if fade > cap+1e-10:
        fade = round(fade-.0001, 4)
    if fade < .005:
        return reject('fade-cannot-finish-before-word-or-rise')
    speech = _validated_mix(mix, song)
    if speech is None:
        return reject('invalid-raw-stereo-mix-evidence')
    mix_begin, mix_end = centered_mix_bounds(point, song['duration'])
    if (mix['identity']['sourceOffsetSeconds'] != mix_begin or
            mix['identity']['requestedEndSeconds'] != mix_end):
        return reject('mix-window-not-canonical-centered6s')
    if a < speech['sourceOffsetSeconds']+1-1e-7 or b > speech['sourceEndSeconds']-.25+1e-7:
        return reject('mix-warmup-or-end-edge')
    indexes = [i for i, (lo, hi) in enumerate(zip(
        speech['frameStartSeconds'], speech['frameEndSeconds'])) if lo < b-1e-8 and hi > a+1e-8]
    if (not indexes or speech['frameStartSeconds'][indexes[0]] > a+1e-7 or
            speech['frameEndSeconds'][indexes[-1]] < b-1e-7 or
            indexes[-1] == len(speech['frameStartSeconds'])-1 and speech['lastFrameIncludesPadding']):
        return reject('incomplete-real-mix-frames')
    left = max(speech['leftSpeechProbability'][i] for i in indexes)
    right = max(speech['rightSpeechProbability'][i] for i in indexes)
    if max(left, right) > parameters['mixSpeechMax']:
        return reject('raw-mix-speech-at-entry')
    return {'accepted': True, 'method': METHOD, 'reason': 'accepted', 'version': version,
        'parameters': dict(parameters), 'sourceMD5': song['md5'], 'sourceStart': point,
        'sourceSignature': source_signature(song), 'observationsHash': digest([words, coverage]),
        'negativeOwnerInputHash': digest(list(rejected)), 'proofStart': a, 'proofEnd': b,
        'sourceOffsetSeconds': offset, 'sourceEndSeconds': offset+duration,
        'vocalFrameFirst': first, 'vocalFrameLastExclusive': last,
        'normalizedVocalMaxDbfs': maximum, 'normalizedVocalP95Dbfs': p95,
        'vocalRmsGeometry': 'sqrt-mean-stereo-channel-squares',
        'perChannelVocalMaxUpperBoundDbfs': maximum+10*math.log10(2),
        'perChannelVocalP95UpperBoundDbfs': p95+10*math.log10(2),
        'vocalToMixP95Db': quantile(values[3][first:last], .95),
        'normalizedMixMaxDbfs': db(max(values[0][first:last])),
        'normalizedMixP95Dbfs': db(quantile(values[0][first:last], .95)),
        'playbackGainDb': level, 'uvrModelSha256': MODEL_SHA256,
        'preVocalP95Dbfs': pre_p95, 'risingThresholdDbfs': threshold,
        'risingVoiceFrame': rise, 'risingVoiceTime': rise_time,
        'risingVoiceDelaySeconds': rise_delay, 'riseObservedWithin350ms': rise is not None,
        'prerollEvidenceKind': 'observed-relative-voice-rise' if rise is not None else 'no-relative-voice-rise-observed',
        'nextObservedWord': next_word, 'nextObservedWordDelaySeconds': next_delay,
        'nextObservedWordsAtFirstStart': same_start,
        'nextObservedWordConfident': all(w[2] for w in same_start) if same_start else None,
        'fadeInCapSeconds': cap, 'fadeInSeconds': fade,
        'mixFrameFirst': indexes[0], 'mixFrameLastExclusive': indexes[-1]+1,
        'leftMixSpeechMax': left, 'rightMixSpeechMax': right,
        'mixModelSHA256': speech['modelSHA256'], 'mixSourceOffsetSeconds': speech['sourceOffsetSeconds'],
        'mixSourceEndSeconds': speech['sourceEndSeconds'],
        'mixRequestedStartSeconds': mix_begin, 'mixRequestedEndSeconds': mix_end,
        'mixPointLeadSeconds': point-mix_begin, 'mixPointTrailSeconds': mix_end-point,
        'mixWindowPolicy': parameters['mixWindowPolicy'],
        'mixNormalizedInputSHA256': mix['normalizedInputSHA256'],
        'mixPCMInputSHA256': mix['decode']['pcmSHA256'],
        'asrCoverageStart': point-.3, 'asrCoverageEnd': point+.35,
        'asrCoverageComplete': True, 'allConfidenceASRVeto': True,
        'asrOverrideApplied': False, 'ASRWordsErased': False,
        'confidenceKind': 'approximate-joint-low-voice-between-observed-words',
        'verifiedWordBoundary': False, 'naturalSilenceClaimed': False,
        'voiceAbsenceProven': False, 'speechModelCanMissSinging': True,
        'speechContextDependenceKnown': True}


def selection_error(item, song, *, owner=(), evidence_folder, rejected=None):
    """Public assembly gate: reload canonical caches and compare every raw fact."""
    try:
        from .fixed_set_entry_review import VERSION as ENTRY_VERSION, fingerprint
        if not isinstance(item, dict) or not _source_valid(song):
            return 'soft-entry-source-mismatch'
        folder = Path(evidence_folder)
        proof = item.get('entryProof', {})
        row = item.get('row', {})
        if not isinstance(proof, dict) or not isinstance(row, dict):
            return 'soft-entry-input-changed'
        if (item.get('entryReviewVersion') != ENTRY_VERSION or proof.get('method') != METHOD or
                not proof.get('accepted') or item.get('entryReviewInputHash') != fingerprint(item, song, owner)):
            return 'soft-entry-input-changed'
        parameters = _policy(proof.get('version'))
        if parameters is None or proof.get('parameters') != parameters:
            return 'soft-entry-policy-version-or-parameters'
        if row.get('trackId') != song['id'] or row.get('md5') != song['md5'] or item.get('md5') != song['md5']:
            return 'soft-entry-source-mismatch'
        if rejected is None:
            owner_map = read_json(folder.parent.parent/'dj1-curation'/'owner-rejected.json')
            if not isinstance(owner_map, dict):
                return 'soft-entry-owner-rejections'
            rejected = owner_map.get(item['trackId'], [])
        key, mixkey = proof.get('cacheKey'), proof.get('mixCacheKey')
        if not _key(key) or not _key(mixkey):
            return 'soft-entry-key'
        raw = read_json(folder/(key+'.json'))
        mix = read_json(folder.parent/'mix-vad-probes'/(mixkey+'.json'))
        if raw.get('cacheKey') != key or mix.get('cacheKey') != mixkey:
            return 'soft-entry-cache-key'
        check = _check_entry(raw, mix, row.get('start'), song,
            item.get('observedWords', []), item.get('observedCoverage', []),
            rejected=rejected, fade_in=row.get('fadeIn'), version=proof['version'])
        if not check['accepted']:
            return 'soft-entry-'+check['reason']
        if proof != {**check, 'cacheKey': key, 'mixCacheKey': mixkey}:
            return 'soft-entry-proof-mismatch'
        span = row['end']-row['start']
        if (not 45 <= span <= 100 or row['fadeIn'] != check['fadeInSeconds'] or
                not song['lo'] <= row['start'] < row['end'] <= song['hi']+.0001 or
                not finite(row.get('fadeOut')) or row['fadeOut'] < 0 or
                row['fadeIn']+row['fadeOut'] >= span):
            return 'soft-entry-row'
        return None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, OverflowError):
        return 'soft-entry-evidence-unavailable'
