"""Verify quiet boundaries using stereo energy, never a cancelling mono sum.

Run serially after model passes, inside the externally limited 1 GiB job.
One original and one stereo PCM file exist at a time. Only short numerical
proof envelopes survive; no raw audio or full-song RMS arrays are persisted.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from .config import Settings, atomic_json
from .fixed_set_repair import HOP, SR, choose, covered, merge, selection_error, word_clear
from .fixed_set_resolve import read_json, restore_evidence
from .quiet_boundaries import short_digital_eof, short_natural_release

VERSION = 1
LIMITS = {'strict-quiet': (.08, .8, .3), 'soft-quiet': (.18, 1.2, .4),
          'instrumental-fade': (.35, 1.6, .5),
          'owner-listened-quiet': (.081, .7, .3)}
PHYSICAL = frozenset((*LIMITS, 'digital-silence', 'short-digital-EOF', 'short-natural-release'))


def stereo_energy_envelope(interleaved, frames_per_bin=160):
    """RMS = sqrt(mean(L² + R²)/2), with bounded vectorized scratch space.

    The small standard-library fallback also permits dependency-free tests.
    It has the same stereo-energy definition and never averages channel signs.
    """
    if type(frames_per_bin) is not int or frames_per_bin <= 0 or len(interleaved) % 2:
        raise ValueError('STEREO_PCM')
    bins = len(interleaved)//(frames_per_bin*2)
    try:
        import numpy as np
    except ImportError:
        width = frames_per_bin*2
        return [math.sqrt(math.fsum(float(x)**2 for x in interleaved[i*width:(i+1)*width])/width)
                for i in range(bins)]
    result = np.empty(bins, dtype=np.float32)
    for first in range(0, bins, 1000):
        last = min(bins, first+1000)
        block = np.asarray(interleaved[first*frames_per_bin*2:last*frames_per_bin*2], dtype=np.float32).reshape(-1, frames_per_bin, 2)
        result[first:last] = np.sqrt(np.mean(block*block, axis=(1, 2)))
    return result


def reference_rms(rms, lo, hi):
    first = math.ceil(lo/HOP); last = math.floor(min(hi, len(rms)*HOP)/HOP)
    levels = sorted(float(v) for v in rms[first:last])
    if not levels or any(not math.isfinite(v) or v < 0 for v in levels):
        raise ValueError('STEREO_REFERENCE')
    at = (len(levels)-1)*.75; lower = math.floor(at); upper = math.ceil(at)
    return max(.00001, levels[lower]+(levels[upper]-levels[lower])*(at-lower))


def input_hash(item, song, evidence):
    payload = [VERSION, song['id'], song['md5'], song['duration'], song['lo'], song['hi'],
               item.get('row'), item.get('exit', {}).get('method'), evidence.get('words', []),
               merge(evidence.get('coverage', [])), evidence.get('repairProbes', [])]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def proof_hash(snapshot):
    value = {key: value for key, value in snapshot.items() if key != 'proofHash'}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def needs_verification(item, retry=False):
    return item.get('exit', {}).get('method') in PHYSICAL and (
        item.get('status') == 'ready' or bool(retry and item.get('stereoEvidence')))


def verify(item, song, evidence, rms, decoded_duration):
    """Independently verify this selected fade against the actual stereo source."""
    row = item.get('row', {}); point = item.get('exit', {}); method = point.get('method')
    reference = reference_rms(rms, song['lo'], song['hi'])
    snapshot = {'version': VERSION, 'sourceMD5': song['md5'],
        'sourceDuration': decoded_duration, 'metadataDuration': song['duration'],
        'allowedStart': song['lo'], 'allowedEnd': song['hi'], 'referenceRms': reference,
        'method': method, 'hop': HOP, 'sourceChannels': 2,
        'energyFormula': 'sqrt(mean(channel_squares))', 'accepted': False}

    def reject(reason):
        snapshot['reason'] = reason
        return snapshot

    if method not in PHYSICAL:
        return reject('not-physical-method')
    if row.get('trackId') != song['id'] or row.get('md5') != song['md5'] or item.get('md5') != song['md5']:
        return reject('source-identity-changed')
    fields = ('start', 'end', 'fadeOut')
    if any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) for k in fields):
        return reject('invalid-row')
    if not song['lo'] <= row['start'] < row['end'] <= min(song['hi'], decoded_duration)+.0001:
        return reject('range-changed')
    if row['fadeOut'] <= 0 or row['end']-row['start'] < 8:
        return reject('invalid-fade')
    if any(type(point.get(k)) not in (int, float) or not math.isfinite(point[k]) for k in ('time', 'fade', 'fadeStart', 'regionStart', 'regionEnd')):
        return reject('invalid-previous-proof')
    if abs(row['end']-point['time']) > .001 or abs(row['fadeOut']-point['fade']) > .001:
        return reject('different-cut')
    actual_start = row['end']-row['fadeOut']
    if abs(actual_start-point['fadeStart']) > .001:
        return reject('different-fade')
    guard = .15
    words = evidence.get('words', []); coverage = evidence.get('coverage', [])
    if method in ('short-digital-EOF', 'short-natural-release'):
        if song.get('manualEnd') or song['hi'] < song['duration']-.001 or abs(decoded_duration-song['duration']) > .05:
            return reject('not-original-EOF')
        guard = .025; minimum = .25 if method == 'short-digital-EOF' else .3
        renewed = None
        if method == 'short-digital-EOF':
            renewed = short_digital_eof(rms, HOP, source_duration=decoded_duration)
        else:
            for probe in reversed(evidence.get('repairProbes', [])):
                renewed = short_natural_release(rms, HOP, source_duration=decoded_duration,
                    reference=reference, words=words, probe=probe)
                if renewed:
                    break
        if renewed is None:
            return reject('EOF-not-quiet-in-stereo')
        if abs(row['end']-min(renewed['time'], song['hi'])) > .001 or row['fadeOut'] > renewed['fade']+.001:
            return reject('different-EOF-cut')
        # Retain only the minimum tail needed to prove the selected fade, not
        # an arbitrarily long silence preceding it.
        finish = min(renewed['regionEnd'], row['end']+guard)
        begin = max(renewed['regionStart'], min(actual_start-guard, finish-minimum))
        absolute_limit = .0001 if method == 'short-digital-EOF' else .0005
        relative_limit = None if method == 'short-digital-EOF' else .18
        padding = 0 if method == 'short-digital-EOF' else .3
        snapshot.update(actualEOF=True, guardSeconds=guard,
                        unmeasuredTail=renewed['unmeasuredTail'])
    else:
        relative_limit, minimum, padding = LIMITS.get(method, (None, 1.2, 0))
        absolute_limit = .0001 if method == 'digital-silence' else .0005
        begin = point['regionStart']
        # Some soft/digital regions need 1.2s proof even though the fade
        # finishes after .95s; keep exactly that minimum, not a long outro.
        finish = min(point['regionEnd'], max(row['end']+guard, begin+minimum))
    first = max(0, math.floor(begin/HOP+1e-7))
    last = min(len(rms), math.ceil(finish/HOP-1e-7))
    proof_start = first*HOP; proof_end = last*HOP
    levels = [float(v) for v in rms[first:last]]
    snapshot.update(proofStart=proof_start, proofEnd=proof_end, frameFirst=first,
                    frameLast=last, rms=levels, rowStart=row['start'], rowEnd=row['end'],
                    fadeOut=row['fadeOut'], wordPadding=padding, minimumSeconds=minimum)
    if not levels or any(not math.isfinite(v) or v < 0 for v in levels):
        return reject('invalid-stereo-measurement')
    measured_max = max(levels)
    gain = min(12, -16-song['lufs'], -1-song['peak']) if song.get('lufs') is not None else 0.
    snapshot.update(measuredMax=measured_max, relativeMax=measured_max/reference,
                    absoluteSilence=measured_max <= absolute_limit,
                    coverageComplete=covered(coverage, proof_start, proof_end),
                    playbackGainDb=gain,
                    normalizedMaxDbfs=20*math.log10(max(1e-10, measured_max))+gain+20*math.log10(.7))
    if proof_end-proof_start < minimum-1e-6:
        return reject('brief-stereo-valley')
    if proof_start > actual_start-guard+.00011 or proof_end < row['end']+guard-.00011:
        return reject('fade-outside-stereo-proof')
    if proof_start < point['regionStart']-.0101 and method not in ('short-digital-EOF', 'short-natural-release'):
        return reject('different-quiet-region')
    if relative_limit is None:
        if measured_max > absolute_limit+1e-12:
            return reject('not-digital-in-stereo')
    elif measured_max > max(reference*relative_limit, absolute_limit)+1e-10:
        return reject('music-not-quiet-in-stereo')
    if method not in ('digital-silence', 'short-digital-EOF'):
        if not word_clear(words, proof_start, proof_end, padding):
            return reject('vocal-over-stereo-proof')
        if method != 'owner-listened-quiet' and not snapshot['coverageComplete']:
            # A mixed proof remains valid only when each uncovered actual bin
            # is physically near-silent, not because another ASR window exists.
            for i, level in enumerate(levels, first):
                if level > absolute_limit and not covered(coverage, i*HOP, (i+1)*HOP):
                    return reject('unknown-vocals-in-stereo')
    snapshot.update(accepted=True, reason='accepted', inputHash=input_hash(item, song, evidence),
                    coverage=merge(coverage))
    snapshot['proofHash'] = proof_hash(snapshot)
    return snapshot


def attach(item, snapshot):
    """Replace numeric mono claims with their independently saved stereo proof."""
    result = copy.deepcopy(item); result['stereoEvidence'] = snapshot
    if not snapshot['accepted']:
        result.update(status='needs-review', stereoError=snapshot['reason'])
        return result
    point = result['exit']
    point.update(maxRms=snapshot['measuredMax'], relativeMax=snapshot['relativeMax'],
                 regionStart=snapshot['proofStart'], regionEnd=snapshot['proofEnd'],
                 coverageComplete=snapshot['coverageComplete'],
                 absoluteSilence=snapshot['absoluteSilence'], stereoVerified=True,
                 maxNormalizedDbfs=snapshot['normalizedMaxDbfs'])
    result.pop('stereoError', None)
    return result


def stereo_error(item, song, evidence):
    """Replay the persisted proof before assembly; never trust accepted alone."""
    point = item.get('exit', {}); method = point.get('method')
    if method not in PHYSICAL:
        return None
    check = item.get('stereoEvidence', {}); row = item.get('row', {})
    if check.get('accepted') is not True or check.get('version') != VERSION:
        return 'missing-stereo-proof'
    if check.get('proofHash') != proof_hash(check):
        return 'changed-stereo-proof'
    if check.get('sourceMD5') != song['md5'] or row.get('md5') != song['md5'] or row.get('trackId') != song['id']:
        return 'stereo-source-mismatch'
    if check.get('method') != method or check.get('sourceChannels') != 2 or check.get('energyFormula') != 'sqrt(mean(channel_squares))':
        return 'invalid-stereo-method'
    required = ('referenceRms', 'proofStart', 'proofEnd', 'measuredMax', 'relativeMax',
                'sourceDuration', 'metadataDuration', 'allowedStart', 'allowedEnd',
                'rowStart', 'rowEnd', 'fadeOut', 'hop')
    if any(type(check.get(key)) not in (int, float) or not math.isfinite(check[key]) for key in required):
        return 'invalid-stereo-numbers'
    if check['referenceRms'] <= 0 or abs(check['hop']-HOP) > 1e-9:
        return 'invalid-stereo-reference'
    if abs(check['allowedStart']-song['lo']) > 1e-8 or abs(check['allowedEnd']-song['hi']) > 1e-8 or abs(check['metadataDuration']-song['duration']) > .001:
        return 'stereo-range-mismatch'
    if any(abs(row.get(k, math.inf)-check[v]) > .00011 for k, v in (('start', 'rowStart'), ('end', 'rowEnd'), ('fadeOut', 'fadeOut'))):
        return 'stereo-cut-changed'
    if check.get('inputHash') != input_hash(item, song, evidence):
        return 'stereo-observations-changed'
    levels = check.get('rms')
    if not isinstance(levels, list) or not levels or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in levels):
        return 'invalid-stereo-envelope'
    first, last = check.get('frameFirst'), check.get('frameLast')
    if type(first) is not int or type(last) is not int or first < 0 or last-first != len(levels):
        return 'invalid-stereo-frame-map'
    if abs(first*HOP-check['proofStart']) > 1e-8 or abs(last*HOP-check['proofEnd']) > 1e-8:
        return 'invalid-stereo-time-map'
    peak = max(levels); relative = peak/check['referenceRms']
    if abs(peak-check['measuredMax']) > 1e-10 or abs(relative-check['relativeMax']) > 1e-8:
        return 'different-stereo-measurement'
    if abs(point.get('maxRms', math.inf)-peak) > 1e-8 or abs(point.get('relativeMax', math.inf)-relative) > 1e-8:
        return 'stale-mono-measurement'
    if abs(point.get('regionStart', math.inf)-check['proofStart']) > .001 or abs(point.get('regionEnd', math.inf)-check['proofEnd']) > .001:
        return 'different-stereo-proof-window'
    eof = method in ('short-digital-EOF', 'short-natural-release')
    guard = .025 if eof else .15
    if check['proofStart'] > row['end']-row['fadeOut']-guard+.00011 or check['proofEnd'] < row['end']+guard-.00011:
        return 'stereo-fade-not-contained'
    if eof:
        minimum = .25 if method == 'short-digital-EOF' else .3
        if check.get('actualEOF') is not True or song.get('manualEnd') or song['hi'] < song['duration']-.001 or abs(check['sourceDuration']-song['duration']) > .05:
            return 'stereo-not-original-EOF'
        if abs(row['end']-check['sourceDuration']) > .05:
            return 'stereo-not-at-EOF'
        relative_limit = .18 if method == 'short-natural-release' else None
        absolute = .0005 if method == 'short-natural-release' else .0001
    else:
        relative_limit, minimum, _ = LIMITS.get(method, (None, 1.2, 0))
        absolute = .0001 if method == 'digital-silence' else .0005
    if check['proofEnd']-check['proofStart'] < minimum-1e-6:
        return 'brief-stereo-proof'
    if peak > (absolute if relative_limit is None else max(absolute, check['referenceRms']*relative_limit))+1e-10:
        return 'stereo-not-quiet'
    if method not in ('digital-silence', 'short-digital-EOF'):
        padding = .3 if eof else LIMITS[method][2]
        if not word_clear(evidence.get('words', []), check['proofStart'], check['proofEnd'], padding):
            return 'stereo-vocal-conflict'
        if method != 'owner-listened-quiet':
            for i, level in enumerate(levels, first):
                if level > absolute and not covered(evidence.get('coverage', []), i*HOP, (i+1)*HOP):
                    return 'stereo-unknown-vocals'
    return None


def run(settings, work, decoder, limit=None, retry=False):
    import numpy as np
    from .disk import Disk
    decoder = shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file() or not os.access(decoder, os.X_OK):
        raise RuntimeError('STEREO_DECODER')
    if limit is not None and limit < 0:
        raise RuntimeError('STEREO_LIMIT')
    plan = read_json(work/'baseline.json')
    curation = settings.state/'research/dj1-curation'
    owner = read_json(curation/'owner-entries.json'); bad = read_json(curation/'owner-rejected.json')
    gold = {r['trackId']: r for r in read_json(curation/'curator-map.json').get('reviewedBoundaries', [])}
    disk = Disk(settings); count = 0
    db = sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro', uri=True); db.row_factory = sqlite3.Row
    try:
        for position, old in enumerate(plan['tracks'], 1):
            target = work/'tracks'/(old['trackId']+'.json')
            if not target.exists():
                continue
            item = read_json(target); method = item.get('exit', {}).get('method')
            if not needs_verification(item, retry):
                continue
            if limit is not None and count >= limit:
                break
            asset = db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,
                l.integrated_lufs AS lufs,l.true_peak_dbtp AS peak FROM web_assets a
                LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''', (old['trackId'],)).fetchone()
            if not asset or asset['md5'] != old['md5']:
                raise RuntimeError('STEREO_SOURCE_CHANGED')
            song = dict(asset)
            song['manualEnd'] = song['hi'] is not None and song['hi'] < song['duration']-.001
            song['lo'] = song['lo'] or 0.; song['hi'] = song['duration'] if song['hi'] is None else song['hi']
            raw = db.execute('SELECT payload FROM dj_evidence WHERE md5=?', (old['md5'],)).fetchone()
            if raw is None:
                raise RuntimeError('STEREO_EVIDENCE_MISSING')
            evidence = restore_evidence(json.loads(raw[0]), item)
            fingerprint = input_hash(item, song, evidence)
            if not retry and item.get('stereoEvidence', {}).get('inputHash') == fingerprint:
                continue
            if shutil.disk_usage(work).free < 768*1024**2:
                raise RuntimeError('STEREO_DISK')
            if not 0 < song['bytes'] <= 40*1024**2 or not 0 < song['duration'] <= 1800:
                raise RuntimeError('STEREO_SOURCE_LIMIT')
            result = copy.deepcopy(item)
            try:
                with tempfile.TemporaryDirectory(prefix='stereo-source-', dir=work) as directory:
                    src = Path(directory)/('source'+Path(song['remote_path']).suffix); pcm = Path(directory)/'stereo.f32'
                    try:
                        disk.download(song['remote_path'], src, expected_md5=old['md5'])
                    except Exception:
                        raise RuntimeError('STEREO_DOWNLOAD') from None
                    try:
                        child = subprocess.run([decoder, '-nostdin', '-v', 'error', '-threads', '1',
                            '-filter_threads', '1', '-i', str(src), '-t', '1800', '-vn', '-sn', '-dn',
                            '-ac', '2', '-ar', str(SR), '-f', 'f32le', '-y', str(pcm)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError('STEREO_DECODE_TIMEOUT') from None
                    if child.returncode or not pcm.exists() or not 0 < pcm.stat().st_size <= SR*1800*2*4 or pcm.stat().st_size % 8:
                        raise RuntimeError('STEREO_DECODE')
                    y = np.memmap(pcm, dtype='<f4', mode='r')
                    try:
                        decoded_duration = len(y)/2/SR
                        rms = stereo_energy_envelope(y)
                        proof = verify(item, song, evidence, rms, decoded_duration)
                        if proof['accepted']:
                            result = attach(item, proof)
                        else:
                            # A stereo failure is not permission to switch to a
                            # loud ASR-only fade. Rechoose only another physical
                            # quiet point and reverify its actual measured bins.
                            replacement = choose(old, song, evidence, rms,
                                owner.get(old['trackId'], []), bad.get(old['trackId'], []), gold.get(old['trackId']))
                            if replacement.get('status') == 'ready' and replacement.get('exit', {}).get('method') in PHYSICAL:
                                replacement.update(observedWords=evidence.get('words', []),
                                    observedCoverage=evidence.get('coverage', []), repairProbes=evidence.get('repairProbes', []),
                                    inputHash=item.get('inputHash'), position=position)
                                revised = verify(replacement, song, evidence, rms, decoded_duration)
                                if revised['accepted']:
                                    result = attach(replacement, revised)
                                    result['stereoPreviousCut'] = item['row']
                                else:
                                    result = attach(item, proof)
                            else:
                                result = attach(item, proof)
                    finally:
                        y._mmap.close()
            except Exception as exc:
                code = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('STEREO_') else 'STEREO_'+type(exc).__name__.upper()
                result.update(status='needs-review', stereoError=code,
                              stereoEvidence={'version': VERSION, 'sourceMD5': old['md5'],
                                  'accepted': False, 'reason': code, 'inputHash': fingerprint})
            if result.get('status') == 'ready':
                error = selection_error(result)
                if error:
                    result.update(status='needs-review', stereoError='STEREO_PROOF_'+error)
            result['stereoVersion'] = VERSION
            atomic_json(target, result); count += 1
            print('STEREO_VERIFIED', position, result['status'], old['trackId'],
                  result.get('exit', {}).get('method', 'none'), result.get('stereoError', ''), flush=True)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True); parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--limit', type=int); parser.add_argument('--retry', action='store_true')
    args = parser.parse_args(); run(Settings(), args.work, args.ffmpeg, args.limit, args.retry)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('STEREO_') else type(exc).__name__
        print('STEREO_VERIFY_FAILED', code, flush=True)
        raise SystemExit(1)
