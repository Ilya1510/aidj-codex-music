"""Second serial pass over unresolved frozen-set boundary checkpoints only.

Run after fixed_set_repair has finished, inside the same separately limited
1 GiB process budget. Keeps song order and successful checkpoints unchanged.
One original/PCM exists at a time; a local cached ASR model is loaded lazily.
Unknown boundaries stay unresolved instead of becoming invented quiet exits.
"""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time

from .config import Settings, atomic_json
from .fixed_set_repair import (HOP, SR, choose, entries_for, probe_supports,
                              selection_error, word_clear)
from .quiet_boundaries import (boundary_points, detect_quiet_regions,
                               short_digital_eof, short_natural_release)

VERSION = 1


def read_json(path):
    if path.stat().st_size > 4*1024**2:
        raise RuntimeError('RESOLVE_METADATA_LIMIT')
    return json.loads(path.read_text())


def restore_evidence(raw, checkpoint):
    """Preserve numerical observations made during the first repair pass."""
    evidence = copy.deepcopy(raw)
    if 'observedWords' in checkpoint:
        evidence['words'] = copy.deepcopy(checkpoint['observedWords'])
    probes = copy.deepcopy(checkpoint.get('repairProbes', []))
    evidence['repairProbes'] = probes
    evidence['coverage'] = [*evidence.get('coverage', []),
                            *checkpoint.get('observedCoverage', []),
                            *[[p['start'], p['end']] for p in probes]]
    return evidence


class LocalObserver:
    """One cached local model; only short numerical observations survive."""
    def __init__(self, settings):
        self.settings = settings
        self.model = None

    def observe(self, evidence, pcm, begin, end):
        import numpy as np
        from .dj_worker import numeric_words, replace_observed_words
        begin = max(0., float(begin)); end = min(len(pcm)/SR, float(end))
        if not 0 < end-begin <= 28.001:
            raise RuntimeError('RESOLVE_PROBE_BOUNDS')
        for previous in evidence.get('repairProbes',[]):
            if abs(previous['start']-begin)<.001 and abs(previous['end']-end)<.001:
                reused={**previous,'resolver':VERSION,'reused':True}
                evidence.setdefault('repairProbes',[]).append(reused)
                return reused
        if self.model is None:
            try:
                from faster_whisper import WhisperModel
                self.model = WhisperModel('base', device='cpu', compute_type='int8',
                    cpu_threads=3, num_workers=1, local_files_only=True,
                    download_root=str(self.settings.state/'dj-models'))
            except Exception:
                raise RuntimeError('RESOLVE_LOCAL_MODEL') from None
        sample = np.array(pcm[int(begin*SR):int(end*SR)], dtype=np.float32, copy=True)
        started = time.monotonic(); fresh = []
        try:
            segments, _ = self.model.transcribe(sample, beam_size=3, temperature=0.,
                word_timestamps=True, condition_on_previous_text=False, vad_filter=False)
            for segment in segments:
                fresh.extend(numeric_words(segment, begin))
                if time.monotonic()-started > 90:
                    raise RuntimeError('RESOLVE_ASR_TIMEOUT')
            if time.monotonic()-started > 90:
                raise RuntimeError('RESOLVE_ASR_TIMEOUT')
        except RuntimeError as exc:
            if str(exc) == 'RESOLVE_ASR_TIMEOUT':
                raise
            raise RuntimeError('RESOLVE_ASR') from None
        except Exception:
            raise RuntimeError('RESOLVE_ASR') from None
        confident = sum(bool(w[2]) for w in fresh)
        # A failed/empty recognition never erases an existing vocal span.
        if confident >= 3:
            evidence['words'] = replace_observed_words(evidence.get('words', []), fresh, begin, end)
        else:
            evidence['words'] = [*evidence.get('words', []), *fresh]
        probe = {'start': begin, 'end': end, 'words': len(fresh),
                 'confident': confident, 'resolver': VERSION}
        evidence.setdefault('repairProbes', []).append(probe)
        evidence.setdefault('coverage', []).append([begin, end])
        return probe


def certify_reobserved(result, evidence):
    """A previous local probe can certify only this actual selected fade."""
    if result.get('status') != 'needs-probe':
        return result
    point = result['exit']
    matching = [p for p in evidence.get('repairProbes', []) if probe_supports(p, point)]
    if matching and word_clear(evidence.get('words', []), point['fadeStart']-.15, point['time']+.1, .4):
        result['status'] = 'ready'
        point.update(validated=True, method='reobserved-instrumental-fade',
                     proof='long-gap-reobserved-and-guarded')
    return result


def quiet_regions(song, evidence, rms):
    import numpy as np
    first = math.ceil(song['lo']/HOP)
    last = math.floor(min(song['hi'], len(rms)*HOP)/HOP)
    levels = rms[first:last]
    if not len(levels):
        raise RuntimeError('RESOLVE_EMPTY_AUDIO_RANGE')
    reference = max(.00001, float(np.quantile(levels, .75)))
    regions = detect_quiet_regions(levels, HOP, first*HOP,
        words=evidence.get('words', []), coverage=evidence.get('coverage', []),
        reference=reference, max_relative=.18, min_seconds=.8,
        absolute_silence=.0005, word_padding=.3)
    return regions, reference


def eof_points(song, evidence, rms, decoded_duration, reference, *, natural=True):
    """Only actual original EOF, with explicit codec-duration reconciliation."""
    duration = song['duration']
    # A manual crop cannot acquire EOF privileges. The decoded duration may
    # differ slightly due to container/codec padding, not arbitrary missing audio.
    if song.get('manualEnd') or song['hi'] < duration-.001 or abs(decoded_duration-duration) > .05:
        return []
    points = []
    digital = short_digital_eof(rms, HOP, source_duration=decoded_duration)
    if digital:
        points.append(digital)
    if natural:
        for probe in reversed(evidence.get('repairProbes', [])):
            if probe.get('resolver') != VERSION:
                continue
            point = short_natural_release(rms, HOP, source_duration=decoded_duration,
                reference=reference, words=evidence.get('words', []), probe=probe)
            if point:
                points.append(point)
                break
    result = []
    for point in points:
        # The playable range still comes from the current catalogue. A few
        # codec-padding milliseconds beyond its end are not playable content.
        end = min(point['time'], song['hi'])
        fade = min(point['fade'], end-point['regionStart']-.025)
        if fade <= 0 or end < song['lo']:
            continue
        point = {**point, 'time': end, 'fade': fade, 'fadeStart': end-fade,
                 'method': point['proof'], 'actualEOF': True,
                 'sourceDuration': decoded_duration, 'metadataDuration': duration,
                 'relativeMax': point['maxRms']/reference,
                 'wordPadding': 0 if point['asrIgnored'] else .3,
                 'guardSeconds': .025}
        if point['method'] == 'short-natural-release' and not word_clear(evidence.get('words', []), point['fadeStart'], point['time'], .3):
            continue
        result.append(point)
    return result


def select_eof(old, song, evidence, regions, points, owner=(), rejected=()):
    """Pair an independently justified EOF with an already safe entry."""
    entries = entries_for(old, song['lo'], song['hi'], evidence.get('words', []),
                          evidence.get('coverage', []), regions, owner)
    candidates = []
    for point in points:
        for entry in entries:
            span = point['time']-entry['time']
            if not 45 <= span <= 100:
                continue
            if any(min(abs(entry['time']-t), abs(point['time']-t)) < .35 for t in rejected):
                continue
            score = entry['penalty']+abs(span-62)/45+abs(entry['time']-old['start'])/200
            candidates.append((score, entry, point))
    if not candidates:
        return None
    score, entry, point = min(candidates, key=lambda row: row[0])
    row = {**old, 'start': max(song['lo'], round(entry['time'], 4)),
           'end': min(song['hi'], round(point['time'], 4)),
           'fadeIn': entry['fade'], 'fadeOut': round(point['fade'], 4),
           'transitionSeconds': 0, 'transitionKind': 'clean-cut', 'rate': 1}
    gain = min(12, -16-song['lufs'], -1-song['peak']) if song.get('lufs') is not None else 0
    point = dict(point)
    point['maxNormalizedDbfs'] = round(20*math.log10(max(1e-10, point['maxRms']))+gain+20*math.log10(.7), 2)
    return {'status': 'ready', 'trackId': old['trackId'], 'md5': old['md5'],
            'artist': song['artist'], 'title': song['title'], 'row': row,
            'entry': entry, 'exit': point, 'score': score,
            'candidateCount': len(candidates), 'old': old}


def resolve(old, song, evidence, rms, pcm, observer, *, owner=(), rejected=(), gold=None):
    """Attempt bounded local observations; never manufacture success."""
    decoded_duration = len(pcm)/SR
    regions, reference = quiet_regions(song, evidence, rms)
    result = certify_reobserved(choose(old, song, evidence, rms, owner, rejected, gold), evidence)
    if result['status'] == 'ready':
        return result
    # Sustained physical digital padding is sufficient; do not load an ASR
    # model merely to repeat hallucinated words in inaudible end padding.
    digital = eof_points(song, evidence, rms, decoded_duration, reference, natural=False)
    eof = select_eof(old, song, evidence, regions, digital, owner, rejected)
    if eof:
        return eof
    if result['status'] == 'needs-probe':
        point = result['exit']
        if not any(probe_supports(p, point) for p in evidence.get('repairProbes', [])):
            begin = max(0., point['time']-14)
            observer.observe(evidence, pcm, begin, min(decoded_duration, begin+28))
            result = certify_reobserved(choose(old, song, evidence, rms, owner, rejected, gold), evidence)
        if result['status'] == 'ready':
            return result
        # A covered but vocal-conflicting instrumental fill is left for the
        # independent vocal-stem pass rather than repeatedly erasing evidence.
        if result['status'] == 'needs-probe':
            return result
    # A fresh EOF observation can distinguish a natural release from old ASR
    # timestamps stretched into the tail. This step does not override words.
    begin = max(0., decoded_duration-28)
    observer.observe(evidence, pcm, begin, decoded_duration)
    result = certify_reobserved(choose(old, song, evidence, rms, owner, rejected, gold), evidence)
    if result['status'] == 'ready':
        return result
    regions, reference = quiet_regions(song, evidence, rms)
    points = eof_points(song, evidence, rms, decoded_duration, reference)
    eof = select_eof(old, song, evidence, regions, points, owner, rejected)
    if eof:
        return eof
    # A good exit can exist while the preceding entry window was not observed.
    # Inspect that window once, preserving the sequence and source identity.
    exits = [p['time'] for p in points]
    exits.extend(p['time'] for p in boundary_points(regions)['exits'])
    exits = [at for at in exits if at-song['lo'] >= 45]
    if exits:
        at = min(exits, key=lambda at: abs(at-old['end']))
        begin = max(song['lo'], at-75)
        end = min(decoded_duration, song['hi'], begin+28, at-45)
        if end-begin >= 8:
            observer.observe(evidence, pcm, begin, end)
            result = certify_reobserved(choose(old, song, evidence, rms, owner, rejected, gold), evidence)
            if result['status'] == 'ready':
                return result
            regions, reference = quiet_regions(song, evidence, rms)
            points = eof_points(song, evidence, rms, decoded_duration, reference)
            eof = select_eof(old, song, evidence, regions, points, owner, rejected)
            if eof:
                return eof
    return result


def run(settings, work, decoder, limit=None, retry=False):
    import numpy as np
    from .disk import Disk
    if limit is not None and limit < 0:
        raise RuntimeError('RESOLVE_LIMIT')
    decoder = shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file() or not os.access(decoder, os.X_OK):
        raise RuntimeError('RESOLVE_DECODER')
    plan = read_json(work/'baseline.json')
    if not isinstance(plan.get('tracks'), list) or len(plan['tracks']) > 2000:
        raise RuntimeError('RESOLVE_BASELINE')
    curation = settings.state/'research/dj1-curation'
    owner = read_json(curation/'owner-entries.json')
    rejected = read_json(curation/'owner-rejected.json')
    gold = {g['trackId']: g for g in read_json(curation/'curator-map.json').get('reviewedBoundaries', [])}
    db = sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    observer = LocalObserver(settings); disk = Disk(settings); count = 0
    try:
        for position, old in enumerate(plan['tracks'], 1):
            target = work/'tracks'/(old['trackId']+'.json')
            if not target.exists():
                continue  # The first pass owns missing/unstarted tracks.
            previous = read_json(target)
            eof_without_raw=(previous.get('exit',{}).get('method') in ('short-digital-EOF','short-natural-release') and not previous.get('eofEvidence'))
            if previous.get('status') == 'ready' and not eof_without_raw:
                continue
            if previous.get('resolutionVersion') == VERSION and not retry and not eof_without_raw:
                continue  # An unchanged failed ASR attempt is not a retry loop.
            if limit is not None and count >= limit:
                break
            asset = db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,
                l.integrated_lufs AS lufs,l.true_peak_dbtp AS peak
                FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''', (old['trackId'],)).fetchone()
            if not asset or asset['md5'] != old['md5']:
                raise RuntimeError('RESOLVE_SOURCE_CHANGED')
            song = dict(asset)
            song['manualEnd'] = song['hi'] is not None and song['hi'] < song['duration']-.001
            song['lo'] = song['lo'] or 0.
            song['hi'] = song['hi'] if song['hi'] is not None else song['duration']
            raw = db.execute('SELECT payload FROM dj_evidence WHERE md5=?', (old['md5'],)).fetchone()
            if raw is None:
                raise RuntimeError('RESOLVE_EVIDENCE_MISSING')
            evidence = restore_evidence(json.loads(raw[0]), previous)
            if shutil.disk_usage(work).free < 768*1024**2:
                raise RuntimeError('RESOLVE_DISK')
            if not 0 < song['duration'] <= 1800 or not 0 < song['bytes'] <= 40*1024**2:
                raise RuntimeError('RESOLVE_SOURCE_LIMIT')
            result = dict(previous); error = None
            try:
                with tempfile.TemporaryDirectory(prefix='resolve-source-', dir=work) as directory:
                    src = Path(directory)/('source'+Path(song['remote_path']).suffix)
                    pcm_path = Path(directory)/'audio.f32'
                    try:
                        disk.download(song['remote_path'], src, expected_md5=old['md5'])
                    except Exception:
                        raise RuntimeError('RESOLVE_DOWNLOAD') from None
                    try:
                        child = subprocess.run([decoder, '-nostdin', '-v', 'error', '-threads', '1',
                            '-filter_threads', '1', '-i', str(src), '-t', '1800', '-vn', '-sn', '-dn',
                            '-ac', '1', '-ar', str(SR), '-f', 'f32le', '-y', str(pcm_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError('RESOLVE_DECODE_TIMEOUT') from None
                    if child.returncode or not pcm_path.exists() or not 0 < pcm_path.stat().st_size <= SR*1800*4:
                        raise RuntimeError('RESOLVE_DECODE')
                    y = np.memmap(pcm_path, dtype='<f4', mode='r')
                    try:
                        n = len(y)//160; rms = np.empty(n, dtype=np.float32)
                        for first in range(0, n, 1000):
                            last = min(n, first+1000)
                            block = np.asarray(y[first*160:last*160]).reshape(-1, 160)
                            rms[first:last] = np.sqrt(np.mean(block*block, axis=1))
                        result = resolve(old, song, evidence, rms, y, observer,
                            owner=owner.get(old['trackId'], []), rejected=rejected.get(old['trackId'], []),
                            gold=gold.get(old['trackId']))
                        tail_first=max(0,n-500)
                        result['eofEvidence']={'startSeconds':tail_first*HOP,'hopSeconds':HOP,
                            'rms':[float(v) for v in rms[tail_first:]],'decodedDuration':len(y)/SR,
                            'metadataDuration':song['duration'],'unmeasuredTail':len(y)/SR-n*HOP,
                            'manualEnd':song['manualEnd']}
                    finally:
                        y._mmap.close()
            except Exception as exc:
                # No raw network/model exception text, signed URLs, or media
                # paths enter logs/checkpoints. Keep the original unresolved cut.
                error = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('RESOLVE_') else 'RESOLVE_'+type(exc).__name__.upper()
            result.update(position=position, resolutionVersion=VERSION,
                inputHash=previous.get('inputHash'), repairProbes=evidence.get('repairProbes', []),
                observedWords=evidence.get('words', []), observedCoverage=evidence.get('coverage', []))
            if error:
                result['resolutionError'] = error
            if result.get('status') == 'ready':
                proof_error = selection_error(result)
                if proof_error:
                    result.update(status='needs-review', resolutionError='RESOLVE_PROOF_'+proof_error)
            atomic_json(target, result)
            count += 1
            print('RESOLVED', position, result['status'], old['trackId'],
                  result.get('exit', {}).get('method', 'none'), error or '', flush=True)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--retry', action='store_true')
    args = parser.parse_args()
    run(Settings(), args.work, args.ffmpeg, args.limit, args.retry)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('RESOLVE_') else type(exc).__name__
        print('QUIET_RESOLVE_FAILED', code, flush=True)
        raise SystemExit(1)
