"""Explicit approximate short OUT between retained observed words.

One frozen 200 ms low-UVR body, corroborated by measured canonical stereo MIX.
A 50 ms fade may finish the clip while music is still audible. Original stereo
RMS is mandatory and reported; it does not establish quiet music or a natural
ending. Existing strong outgoing methods and defaults are separate contracts.
"""
import math
from pathlib import Path
import re

from .fixed_set_repair import covered
from .fixed_set_resolve import read_json
from .fixed_set_soft_entry import (
    _source_valid, _observations_valid, _validated_vocal, _validated_mix,
    source_signature, centered_mix_bounds, digest,
)
from .fixed_set_relative_entry import validate_rms, _rms_region
from .fixed_set_vocal_review import gain_db, quantile, finite
from .vocal_probe import MODEL_SHA256

VERSION = 1
METHOD = 'approximate-quiet-vocal-short-out'
PROOF_KIND = 'approximate-short-OUT-raw-UVR-ASR-MIX-RMS'
PARAMETERS = {
    'bodyStartSeconds': -.05, 'bodyEndSeconds': .15,
    'vocalMaxDbfs': -30., 'vocalP95Dbfs': -33.,
    'asrCoverageBeforeSeconds': .35, 'asrCoverageAfterSeconds': .3,
    'asrCrossingMarginSeconds': .08, 'lastObservedWordMinimumDelaySeconds': .1,
    'lastWordRequired': False, 'relativeVoicePostStartSeconds': .02,
    'relativeVoicePostEndSeconds': .15, 'relativeVoiceSearchStartSeconds': -.35,
    'relativeVoiceSearchEndSeconds': .02, 'relativeRiseDb': 8.,
    'lastRelativeLoudFrameENDMinimumDelaySeconds': .1,
    'relativeVoiceObservationRequired': False, 'maximumFadeOutSeconds': .05,
    'minimumFadeOutSeconds': .005, 'fadeStartsAfterWordAndVoiceEndSeconds': .03,
    'vocalContextGuardSeconds': .25, 'canonicalMIXSeconds': 6.,
    'mixWindowPolicy': 'point-centered6s-shift-at-source-edges-no-context-selection',
    'mixWarmupSeconds': 1., 'mixEndGuardSeconds': .25,
    'mixSpeechMax': .05, 'negativeOwnerRadiusSeconds': .35,
    'actualOriginalStereoRMSRequired': True, 'quietMusicP95Dbfs': -30.,
    'quietMusicDecisionRole': 'diagnostic-only',
}


def check_out(vocal, mix, rms, point, song, words, coverage, *, rejected=(), fade_out=.05):
    """Replay one fixed raw triple; this function never chooses or writes a cut."""
    reject = lambda reason: {'accepted': False, 'method': METHOD, 'reason': reason}
    if (not _source_valid(song) or not finite(point) or not song['lo'] <= point < song['hi'] or
            not finite(fade_out) or not .005 <= fade_out <= .05):
        return reject('invalid-source-point-or-fade')
    if not isinstance(rejected, (list, tuple)) or any(not finite(t) for t in rejected):
        return reject('invalid-owner-rejections')
    if any(abs(point-t) < .35 for t in rejected):
        return reject('owner-rejected-outpoint')
    if not _observations_valid(words, coverage):
        return reject('invalid-retained-ASR')
    if not covered(coverage, point-.35, point+.3):
        return reject('ASR-coverage-unknown')
    if any(a < point+.08-1e-7 and b > point-.08+1e-7 for a, b, _ in words):
        return reject('known-word-crosses-outpoint-guard')
    previous = sorted((list(w) for w in words if w[1] <= point+1e-7),
        key=lambda w: (w[1], w[0]), reverse=True)
    last = previous[0] if previous else None
    word_delay = point-last[1] if last is not None else None
    same = [w for w in previous if abs(w[1]-last[1]) < 1e-7] if previous else []
    if word_delay is not None and word_delay < .1-1e-7:
        return reject('observed-word-postroll-under100ms')
    prepared = _validated_vocal(vocal, song)
    if prepared is None:
        return reject('invalid-raw-vocal-evidence')
    offset, hop, duration, values = prepared
    if (point-.35 < max(song['lo'], offset+.25)-1e-7 or
            point+.3 > min(song['hi'], offset+duration-.25)+1e-7):
        return reject('incomplete-vocal-context')

    def region(a, b):
        first = math.floor((a-offset)/hop+1e-8)
        last_index = math.ceil((b-offset)/hop-1e-8)
        if not 0 <= first < last_index <= len(values[1]):
            return None
        return first, last_index

    a, b = point-.05, point+.15
    body = region(a, b); post = region(point+.02, point+.15)
    search = region(point-.35, point+.02)
    if None in (body, post, search):
        return reject('incomplete-vocal-frames')
    level = gain_db(song); db = lambda value: 20*math.log10(max(value, 1e-10))+level
    maximum = db(max(values[1][body[0]:body[1]]))
    p95 = db(quantile(values[1][body[0]:body[1]], .95))
    if maximum > -30 or p95 > -33:
        return reject('vocal-above-short-out-ceiling')
    post_p95 = db(quantile(values[1][post[0]:post[1]], .95)); threshold = post_p95+8
    loud = next((i for i in range(search[1]-1, search[0]-1, -1)
        if db(values[1][i]) >= threshold), None)
    loud_end = offset+(loud+1)*hop if loud is not None else None
    voice_delay = point-loud_end if loud_end is not None else None
    if voice_delay is not None and voice_delay < .1-1e-7:
        return reject('observed-voice-postroll-under100ms')
    cap = min(.05, word_delay-.03 if word_delay is not None else .05,
        voice_delay-.03 if voice_delay is not None else .05)
    fade = round(min(fade_out, cap), 4)
    if fade > cap+1e-10:
        fade = round(fade-.0001, 4)
    if fade < .005:
        return reject('fade-cannot-start-after-word-or-voice')
    speech = _validated_mix(mix, song)
    if speech is None:
        return reject('invalid-raw-stereo-mix-evidence')
    begin, end = centered_mix_bounds(point, song['duration'])
    if (mix['identity']['sourceOffsetSeconds'] != begin or
            mix['identity']['requestedEndSeconds'] != end):
        return reject('mix-window-not-canonical-centered6s')
    if a < speech['sourceOffsetSeconds']+1-1e-7 or b > speech['sourceEndSeconds']-.25+1e-7:
        return reject('mix-warmup-or-end-edge')
    ids = [i for i, (lo, hi) in enumerate(zip(speech['frameStartSeconds'], speech['frameEndSeconds']))
        if lo < b-1e-8 and hi > a+1e-8]
    if (not ids or speech['frameStartSeconds'][ids[0]] > a+1e-7 or
            speech['frameEndSeconds'][ids[-1]] < b-1e-7 or
            ids[-1] == len(speech['frameStartSeconds'])-1 and speech['lastFrameIncludesPadding']):
        return reject('incomplete-real-mix-frames')
    left = max(speech['leftSpeechProbability'][i] for i in ids)
    right = max(speech['rightSpeechProbability'][i] for i in ids)
    if max(left, right) > .05:
        return reject('raw-mix-speech-at-outpoint')
    stats = validate_rms(rms, song, point)
    if stats is None:
        return reject('invalid-independent-original-stereo-rms')
    if rms['sourcePCMInputSHA256'] != mix['decode']['pcmSHA256']:
        return reject('MIX-and-RMS-original-PCM-hashes-differ')
    span = _rms_region(stats, a, b)
    if span is None:
        return reject('incomplete-original-stereo-rms-body')
    energy = stats['combinedStereoRms'][span[0]:span[1]]
    rms_db = lambda value: 20*math.log10(max(value, 1e-10))
    music_max = rms_db(max(energy)); music_p95 = rms_db(quantile(energy, .95))
    return {'accepted': True, 'method': METHOD, 'version': VERSION, 'reason': 'accepted',
        'parameters': dict(PARAMETERS), 'sourceMD5': song['md5'], 'sourceId': song['id'],
        'sourceEnd': point, 'sourceSignature': source_signature(song),
        'observationsHash': digest([words, coverage]), 'negativeOwnerInputHash': digest(list(rejected)),
        'source': {k: song.get(k) for k in ('id','md5','bytes','duration','lo','hi','manualStart',
            'lufs','peak','loudnessMethod','loudnessStatus')},
        'proofStart': a, 'proofEnd': b, 'vocalSourceOffsetSeconds': offset,
        'vocalSourceEndSeconds': offset+duration, 'vocalFrameFirst': body[0],
        'vocalFrameLastExclusive': body[1], 'normalizedVocalMaxDbfs': maximum,
        'normalizedVocalP95Dbfs': p95, 'playbackGainDb': level,
        'uvrModelSha256': MODEL_SHA256, 'postVocalP95Dbfs': post_p95,
        'relativeLoudThresholdDbfs': threshold, 'lastRelativeLoudFrame': loud,
        'lastRelativeLoudFrameEndSeconds': loud_end, 'lastObservedVoiceDelaySeconds': voice_delay,
        'noObservedRelativeVoiceBranch': loud_end is None, 'lastObservedWord': last,
        'lastObservedWordsAtLastEnd': same, 'lastObservedWordDelaySeconds': word_delay,
        'lastObservedWordConfident': all(w[2] for w in same) if same else None,
        'noObservedLastWordBranch': last is None, 'fadeOutCapSeconds': cap,
        'fadeOutSeconds': fade, 'fadeStartsAtSeconds': point-fade,
        'mixFrameFirst': ids[0], 'mixFrameLastExclusive': ids[-1]+1,
        'leftMixSpeechMax': left, 'rightMixSpeechMax': right,
        'mixModelSHA256': speech['modelSHA256'], 'mixRequestedStartSeconds': begin,
        'mixRequestedEndSeconds': end, 'mixWindowPolicy': PARAMETERS['mixWindowPolicy'],
        'mixNormalizedInputSHA256': mix['normalizedInputSHA256'],
        'sourcePCMInputSHA256': rms['sourcePCMInputSHA256'],
        'rmsNormalizedInputSHA256': rms['normalizedInputSHA256'],
        'rmsFrameFirst': span[0], 'rmsFrameLastExclusive': span[1],
        'actualOriginalStereoRMSMaxDbfs': music_max, 'actualOriginalStereoRMSP95Dbfs': music_p95,
        'quietMusicP95AtMostMinus30': music_p95 <= -30.,
        'originalMusicEnergyDecisionRole': 'diagnostic-only', 'actualOriginalStereoRMSRequired': True,
        'asrCoverageStart': point-.35, 'asrCoverageEnd': point+.3,
        'asrCoverageComplete': True, 'allConfidenceASRVeto': True,
        'ASRWordsErased': False, 'asrOverrideApplied': False,
        'confidenceKind': 'approximate-short-low-vocal-cut-between-observed-words',
        'verifiedWholeWordBoundary': False, 'naturalSilenceClaimed': False,
        'voiceAbsenceProven': False, 'quietMusicClaimed': False,
        'speechModelCanMissSinging': True}


def exit_point(proof, *, vocal_key, mix_key, rms_key):
    """Build a distinct typed exit; no old strong proof is reinterpreted."""
    if proof.get('accepted') is not True or proof.get('method') != METHOD:
        raise ValueError('short-out proof is not accepted')
    return {'method': METHOD, 'validated': True, 'proof': PROOF_KIND,
        'naturalSilenceClaimed': False, 'verifiedWholeWordBoundary': False, 'quietMusicClaimed': False,
        'time': proof['sourceEnd'], 'fade': proof['fadeOutSeconds'],
        'fadeStart': proof['fadeStartsAtSeconds'],
        'shortOutProof': {**proof, 'vocalCacheKey': vocal_key, 'mixCacheKey': mix_key,
            'rmsCacheKey': rms_key}}


def saved_selection_error(item):
    """Structural gate only; assembly must also call current-source raw replay."""
    try:
        point = item['exit']; proof = point['shortOutProof']; row = item['row']
        if (point['method'] != METHOD or point.get('validated') is not True or
                point.get('proof') != PROOF_KIND or
                any(point.get(k) is not False for k in ('naturalSilenceClaimed','verifiedWholeWordBoundary','quietMusicClaimed')) or
                proof.get('method') != METHOD or
                proof.get('accepted') is not True or type(proof.get('version')) is not int or
                proof['version'] != VERSION or proof.get('parameters') != PARAMETERS):
            return 'short-out-policy-version-or-parameters'
        for name in ('time', 'fade', 'fadeStart'):
            if not finite(point.get(name)):
                return 'short-out-invalid-exit'
        if (row['trackId'] != item['trackId'] or row['md5'] != item['md5'] or
                proof['sourceId'] != item['trackId'] or proof['sourceMD5'] != item['md5'] or
                row['end'] != point['time'] or row['fadeOut'] != point['fade'] or
                point['time'] != proof['sourceEnd'] or point['fade'] != proof['fadeOutSeconds'] or
                point['fadeStart'] != proof['fadeStartsAtSeconds'] or
                abs(point['fadeStart']-(point['time']-point['fade'])) > 1e-8 or
                not .005 <= point['fade'] <= .05):
            return 'short-out-row-or-source-mismatch'
        from .fixed_set_duration_exception import maximum_span
        source = proof['source']
        span = row['end']-row['start']
        if (source_signature(source) != proof['sourceSignature'] or
                not source['lo'] <= row['start'] < row['end'] <= source['hi']+.0001 or
                not 45 <= span <= maximum_span(item, source) or
                not finite(row.get('fadeIn')) or row['fadeIn'] < 0 or
                row['fadeIn']+row['fadeOut'] >= span):
            return 'short-out-row-duration-range-or-fades'
        if not all(re.fullmatch('[a-f0-9]{64}', proof[k]) for k in
                ('vocalCacheKey','mixCacheKey','rmsCacheKey')):
            return 'short-out-raw-cache-key'
        return None
    except (TypeError, ValueError, KeyError, AttributeError):
        return 'short-out-proof-unavailable'


def selection_error(item, song, *, evidence_folder, rejected=()):
    """Reopen all three actual caches and compare every current raw fact."""
    try:
        failure = saved_selection_error(item)
        if failure:
            return failure
        proof = item['exit']['shortOutProof']; folder = Path(evidence_folder)
        vocal_key, mix_key, rms_key = (proof[k] for k in ('vocalCacheKey','mixCacheKey','rmsCacheKey'))
        files = ((folder/(vocal_key+'.json'), 2*1024**2),
            (folder.parent/'mix-vad-probes'/(mix_key+'.json'), 2*1024**2),
            (folder.parent/'mix-rms-probes'/(rms_key+'.json'), 1024**2))
        if any(not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= size
                for path, size in files):
            return 'short-out-evidence-size-or-kind'
        vocal = read_json(folder/(vocal_key+'.json'))
        mix = read_json(folder.parent/'mix-vad-probes'/(mix_key+'.json'))
        rms = read_json(folder.parent/'mix-rms-probes'/(rms_key+'.json'))
        if (vocal.get('cacheKey') != vocal_key or mix.get('cacheKey') != mix_key or
                rms.get('cacheKey') != rms_key):
            return 'short-out-raw-key-mismatch'
        actual = check_out(vocal, mix, rms, item['row']['end'], song,
            item.get('observedWords', []), item.get('observedCoverage', []),
            rejected=rejected, fade_out=item['row']['fadeOut'])
        if not actual['accepted']:
            return 'short-out-'+actual['reason']
        expected = {**actual, 'vocalCacheKey': vocal_key, 'mixCacheKey': mix_key, 'rmsCacheKey': rms_key}
        if digest(proof) != digest(expected):
            return 'short-out-current-raw-proof-mismatch'
        return None
    except (OSError, TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return 'short-out-raw-evidence-unavailable'
