"""Optional serial vocal-stem review after the full RMS/ASR and EOF passes.

Thresholds are conservative design heuristics calibrated on the owner's Awoo
counterexample and approved tail; they are not model probabilities. Reverb may
be assigned entirely to vocals, so ratio alone cannot reject an inaudible tail.
No source, order, or original audio is changed. Unresolved cases stay explicit.
Run in one separate1GiB/time-limited process, never alongside another audio job.
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
import time
from dataclasses import replace

from .config import Settings, atomic_json
from .fixed_set_repair import entries_for, word_clear
from .fixed_set_resolve import read_json, restore_evidence
from .vocal_probe import MODEL_SHA256, SAMPLE_RATE, probe_vocals, verify_model

VERSION = 1
MAX_ENDPOINT_PROBES = 5
MAX_CONTEXT_PROBES = 1
MAX_TRACK_SECONDS = 180
PARAMETERS = {'quietP95Dbfs': -43., 'absoluteMaxDbfs': -37.,
              'suppressedP95Db': -30., 'fadeMarginSeconds': .2,
              'wordPaddingSeconds': .3, 'excerptGuardSeconds': .25,
              'searchMinSeconds': 1.5, 'searchFadeSeconds': .8,
              'searchMarginSeconds': .3}
METHODS = {'vocal-checked-instrumental-fade', 'vocal-isolated-fade'}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def quantile(values, probability):
    ordered = sorted(values)
    if not ordered:
        raise ValueError('Empty vocal evidence interval')
    at = (len(ordered)-1)*probability
    lo = math.floor(at); hi = math.ceil(at)
    return ordered[lo]*(hi-at)+ordered[hi]*(at-lo) if lo != hi else ordered[lo]


def gain_db(song):
    """Same constant per-track correction and baseline as native PlaybackLevel."""
    lufs, peak = song.get('lufs'), song.get('peak')
    valid = finite(lufs) and finite(peak) and -70 <= lufs <= 0 and -60 <= peak <= 6
    valid = valid and song.get('loudnessMethod','ebu-r128-v1') == 'ebu-r128-v1' and song.get('loudnessStatus','complete') == 'complete'
    correction = min(12., -16-lufs, -1-peak) if valid else 0.
    return correction+20*math.log10(.7)


def _vectors(evidence, *, allow_warnings=False):
    fields = ('mixRms', 'vocalRms', 'instrumentalRms', 'vocalToMixDb')
    values = [evidence.get(key) for key in fields]
    if any(not isinstance(row, list) or not 1 <= len(row) <= 3001 for row in values):
        return None
    if len({len(row) for row in values}) != 1 or any(not finite(v) for row in values for v in row):
        return None
    if any(v < 0 for row in values[:3] for v in row):
        return None
    offset = evidence.get('sourceOffsetSeconds', evidence.get('sourceStart'))
    hop = evidence.get('frameHopSeconds'); duration = evidence.get('durationSeconds')
    if not finite(offset) or offset < 0 or not finite(hop) or abs(hop-.01) > 1e-8 or not finite(duration) or not 0 < duration <= 30.001:
        return None
    if abs(len(values[0])*hop-duration) > hop+.0001:
        return None
    if evidence.get('modelSha256') != MODEL_SHA256 or evidence.get('warnings') and not allow_warnings:
        return None
    for mix,voice,ratio in zip(values[0],values[1],values[3]):
        expected = 20*math.log10(max(voice,1e-8)/max(mix,1e-8))
        if abs(ratio-expected) > .002:return None
    return offset, hop, duration, values


def check_endpoint(evidence, point, words, playback_gain_db, reference_rms):
    """Recheck the *whole* selected fade plus margins against actual10ms frames."""
    rejected = lambda reason: {'accepted': False, 'reason': reason}
    if not finite(playback_gain_db) or not finite(reference_rms) or reference_rms <= 0:
        return rejected('invalid-level-reference')
    if any(not finite(point.get(k)) for k in ('time', 'fade', 'fadeStart')) or not .025 <= point['fade'] <= 8:
        return rejected('invalid-fade')
    if abs(point['time']-point['fade']-point['fadeStart']) > .001:
        return rejected('inconsistent-fade')
    prepared = _vectors(evidence)
    if prepared is None:
        return rejected('invalid-vocal-evidence')
    offset, hop, duration, values = prepared
    a = point['fadeStart']-PARAMETERS['fadeMarginSeconds']
    b = point['time']+PARAMETERS['fadeMarginSeconds']
    guard = PARAMETERS['excerptGuardSeconds']
    if a < offset+guard-1e-7 or b > offset+duration-guard+1e-7:
        return rejected('incomplete-fade-context')
    # Include every frame touching the interval; this cannot hide a short vocal
    # by selecting only center timestamps inside the desired window.
    first = max(0, math.floor((a-offset)/hop+1e-8))
    last = min(len(values[0]), math.ceil((b-offset)/hop-1e-8))
    if first >= last or offset+first*hop > a+1e-6 or offset+last*hop < b-1e-6:
        return rejected('incomplete-fade-evidence')
    mix, voice, instrumental, ratio = [row[first:last] for row in values]
    maximum = max(voice); p95 = quantile(voice, .95)
    normalized_max = 20*math.log10(max(1e-10, maximum))+playback_gain_db
    normalized_p95 = 20*math.log10(max(1e-10, p95))+playback_gain_db
    ratio_p95 = quantile(ratio, .95)
    transient_ok = normalized_max <= PARAMETERS['absoluteMaxDbfs']
    criterion = ('absolute-quiet' if transient_ok and normalized_p95 <= PARAMETERS['quietP95Dbfs'] else
                 'suppressed-vocal' if transient_ok and ratio_p95 <= PARAMETERS['suppressedP95Db'] else None)
    physical_digital = max(mix) <= .0001 and b-a >= .7 and point.get('actualEOF') is True
    asr_clear = word_clear(words, point['fadeStart'], point['time'], PARAMETERS['wordPaddingSeconds'])
    # Internal digital dips never overrule known words. Actual EOF exceptions
    # additionally need a sustained physical interval and complete measurements.
    if not asr_clear and not physical_digital:
        criterion = None
    result = {'accepted': criterion is not None, 'reason': 'accepted' if criterion else 'vocal-over-fade' if not asr_clear else 'vocal-not-quiet',
              'criterion': criterion, 'parameters': dict(PARAMETERS), 'modelSha256': MODEL_SHA256,
              'proofStart': a, 'proofEnd': b, 'sourceOffsetSeconds': offset,
              'sourceEndSeconds': offset+duration, 'frameFirst': first, 'frameLastExclusive': last,
              'frameCount': last-first, 'playbackGainDb': playback_gain_db,
              'vocalMaxRms': maximum, 'vocalP95Rms': p95,
              'normalizedVocalMaxDbfs': normalized_max, 'normalizedVocalP95Dbfs': normalized_p95,
              'vocalToMixP95Db': ratio_p95, 'mixRms': max(mix),
              'mixP95Rms': quantile(mix, .95), 'instrumentalMaxRms': max(instrumental),
              'relativeMax': max(mix)/reference_rms, 'referenceRms': reference_rms,
              'asrClear': asr_clear, 'asrIgnored': bool(not asr_clear and physical_digital),
              'verifiedWordBoundary': False}
    return result


def _entry_valid(entry, old, song, evidence, owner):
    t = entry.get('time')
    if not finite(t) or not song['lo'] <= t < song['hi']:
        return False
    if abs(t-song['lo']) < .001:
        return True
    if entry.get('kind') == 'owner':
        return any(abs(t-at) < .001 for at in owner)
    return (entry.get('kind') in ('quiet-entry', 'kept-guarded', 'observed-word-gap') and
            word_clear(evidence.get('words', []), t-.1, t+.1, .2))


def _point(check, original, method):
    return {**original, 'method': method, 'validated': True,
            'proof': 'independent-vocal-stem-fade', 'regionStart': check['proofStart'],
            'regionEnd': check['proofEnd'], 'maxRms': check['mixRms'], 'mixRms': check['mixRms'],
            'relativeMax': check['relativeMax'], 'coverageComplete': True,
            'absoluteSilence': check['mixRms'] <= .0001, 'wordPadding': PARAMETERS['wordPaddingSeconds'],
            'maxNormalizedDbfs': 20*math.log10(max(1e-10, check['mixRms']))+check['playbackGainDb'],
            'vocalCheck': check, 'asrIgnored': check['asrIgnored']}


def _search_candidates(old, song, evidence, vocal, owner, rejected):
    """Find sustained low-vocal1.5s intervals, then pair only already safe entries."""
    prepared = _vectors(vocal)
    if prepared is None:
        return []
    offset, hop, duration, values = prepared
    entries = entries_for(old, song['lo'], song['hi'], evidence.get('words', []),
                          evidence.get('coverage', []), [], owner)
    gain = gain_db(song); candidate_points = []
    n = math.ceil(PARAMETERS['searchMinSeconds']/hop)
    # Step100ms; detailed endpoint proof still includes every10ms frame.
    for first in range(math.ceil(PARAMETERS['excerptGuardSeconds']/hop), len(values[0])-n, 10):
        a = offset+first*hop; b = a+n*hop
        if a < song['lo'] or b > min(song['hi'], offset+duration-PARAMETERS['excerptGuardSeconds']):
            continue
        voice = values[1][first:first+n]; ratio = values[3][first:first+n]
        maximum = 20*math.log10(max(1e-10, max(voice)))+gain
        quiet = 20*math.log10(max(1e-10, quantile(voice,.95)))+gain <= PARAMETERS['quietP95Dbfs']
        if maximum > PARAMETERS['absoluteMaxDbfs'] or not (quiet or quantile(ratio,.95) <= PARAMETERS['suppressedP95Db']):
            continue
        end = round(a+PARAMETERS['searchMarginSeconds']+PARAMETERS['searchFadeSeconds'], 4)
        point = {'time': end, 'fade': PARAMETERS['searchFadeSeconds'],
                 'fadeStart': round(end-PARAMETERS['searchFadeSeconds'], 4),
                 'regionStart': a, 'regionEnd': b}
        if any(abs(end-t) < .35 for t in rejected):
            continue
        check = check_endpoint(vocal, point, evidence.get('words', []), gain,
                               max(1e-5, quantile(values[0], .75)))
        if not check['accepted']:
            continue
        for entry in entries:
            span = end-entry['time']
            if not 45 <= span <= 100 or any(abs(entry['time']-t) < .35 for t in rejected):
                continue
            if not _entry_valid(entry, old, song, evidence, owner):
                continue
            score = entry['penalty']+abs(span-62)/45+abs(entry['time']-old['start'])/200+abs(end-old['end'])/100
            candidate_points.append({'score': score, 'entry': entry, 'exit': point})
    return sorted(candidate_points, key=lambda item:item['score'])[:32]


def review_checkpoint(old, song, checkpoint, evidence, probe_window, *, owner=(), rejected=(), search=True, max_seconds=MAX_TRACK_SECONDS):
    """probe_window(begin,end) returns cached numeric evidence in source coordinates."""
    result = copy.deepcopy(checkpoint); attempts = []; proofs = []
    deadline = time.monotonic()+max_seconds; measured = {}
    def bounded_probe(begin,end):
        key=(round(begin*SAMPLE_RATE),round(end*SAMPLE_RATE))
        if key in measured:return measured[key]
        if time.monotonic()>deadline:raise RuntimeError('VOCAL_REVIEW_TRACK_TIMEOUT')
        if len(measured)>=MAX_ENDPOINT_PROBES+MAX_CONTEXT_PROBES:raise RuntimeError('VOCAL_REVIEW_PROBE_BUDGET')
        value=probe_window(begin,end)
        if time.monotonic()>deadline:raise RuntimeError('VOCAL_REVIEW_TRACK_TIMEOUT')
        measured[key]=value
        return value
    current = {'entry': checkpoint.get('entry'), 'exit': checkpoint.get('exit'), 'score': checkpoint.get('score', 0)}
    candidates = [current, *checkpoint.get('alternatives', [])[:MAX_ENDPOINT_PROBES-1]]
    seen = set()
    reference = checkpoint.get('referenceRms', .1)
    reference = reference if finite(reference) and reference > 0 else .1
    existing_ready = checkpoint.get('status') == 'ready'

    def inspect(candidate, vocal=None):
        entry, point = candidate.get('entry'), candidate.get('exit')
        if not isinstance(entry, dict) or not isinstance(point, dict):return None
        if not _entry_valid(entry, old, song, evidence, owner):return None
        end = point.get('time'); fade = point.get('fade')
        if not finite(end) or not finite(fade) or not 45 <= end-entry['time'] <= 100 or end > song['hi']:
            return None
        if any(min(abs(entry['time']-t),abs(end-t)) < .35 for t in rejected):return None
        signature = (round(entry['time'],4),round(end,4),round(fade,4))
        if signature in seen:return None
        seen.add(signature)
        point = {**point, 'fadeStart': end-fade}
        if vocal is None:
            #5.4s centered0.8s before the endpoint gives2.4s fade lookbehind
            # and up to1s endpoint extension without losing real context.
            begin = max(0., end-3.5); finish = min(song['duration'], begin+5.4)
            vocal = bounded_probe(begin, finish)
        check = check_endpoint(vocal, point, evidence.get('words', []), gain_db(song), reference)
        proofs.append({k:vocal[k] for k in ('cacheKey','sourceOffsetSeconds','durationSeconds','modelSha256') if k in vocal})
        proposed = []
        loud_music = finite(check.get('mixRms')) and 20*math.log10(max(1e-10,check['mixRms']))+gain_db(song) > -35
        if loud_music and fade < 2.0:
            proposed.append({**point,'fade':2.4,'fadeStart':end-2.4})
            extra = min(1.,2.4-fade)
            proposed.append({**point,'time':end+extra,'fade':fade+extra})
        proposed.append(point)
        chosen = None
        for ramp in proposed:
            if ramp['time'] > song['hi'] or not 45 <= ramp['time']-entry['time'] <= 100:
                continue
            if any(abs(ramp['time']-t)<.35 for t in rejected):
                continue
            checked = check if ramp == point else check_endpoint(vocal,ramp,evidence.get('words',[]),gain_db(song),reference)
            attempts.append({'entry':entry['time'],'end':ramp['time'],'fade':ramp['fade'],**checked})
            if checked['accepted']:
                point,check,chosen = ramp,checked,True
                break
        if not chosen:return None
        method = 'vocal-checked-instrumental-fade' if existing_ready else 'vocal-isolated-fade'
        selected = _point(check, point, method)
        if loud_music:
            selected['rampFacts'] = {'kind':'generated-release','seconds':point['fade'],
                'sourceFadeStart':point['fadeStart'],'sourceFadeEnd':point['time'],
                'normalizedMixMaxDbfs':20*math.log10(max(1e-10,check['mixRms']))+gain_db(song),
                'normalizedMixP95Dbfs':20*math.log10(max(1e-10,check['mixP95Rms']))+gain_db(song),
                'gainAtStart':.7,'gainAtEnd':0.,'naturalSilenceClaimed':False}
        row = {**old, 'start': max(song['lo'], round(entry['time'],4)), 'end': round(point['time'],4),
               'fadeIn': entry.get('fade', .1), 'fadeOut': round(point['fade'],4),
               'transitionSeconds': 0, 'transitionKind': 'clean-cut', 'rate': 1}
        return {'status': 'ready', 'trackId': old['trackId'], 'md5': old['md5'],
                'artist': song['artist'], 'title': song['title'], 'old': old,
                'row': row, 'entry': entry, 'exit': selected, 'score': candidate.get('score',0),
                'referenceRms': reference, 'alternatives': checkpoint.get('alternatives', [])}

    selected = None
    for candidate in candidates:
        selected = inspect(candidate)
        if selected:break
    if selected is None and search and song['hi']-song['lo'] >= 45:
        # A single longer contextual probe, not a full-song separator pass.
        finish = min(song['duration'], song['hi'], old['end']+10)
        begin = max(0., song['lo'], finish-20)
        if finish-begin >= 1.5:
            vocal = bounded_probe(begin, finish)
            proofs.append({k:vocal[k] for k in ('cacheKey','sourceOffsetSeconds','durationSeconds','modelSha256') if k in vocal})
            for candidate in _search_candidates(old, song, evidence, vocal, owner, rejected):
                selected = inspect(candidate, vocal)
                if selected:break
    if selected is not None:
        result.update(selected)
    else:
        result.update(status='needs-review', vocalReviewReason='no-independent-vocal-free-fade')
    result.update(vocalReviewVersion=VERSION, vocalReviewParameters=dict(PARAMETERS),
                  vocalReviewAttempts=attempts, vocalEvidence=proofs,
                  vocalReviewBudget={'endpointLimit':MAX_ENDPOINT_PROBES,'contextLimit':MAX_CONTEXT_PROBES,
                      'distinctWindows':len(measured),'maxRequestedAudioSeconds':MAX_ENDPOINT_PROBES*5.4+20,
                      'maxTrackSeconds':max_seconds},
                  observedWords=copy.deepcopy(evidence.get('words', [])),
                  observedCoverage=copy.deepcopy(evidence.get('coverage', [])),
                  repairProbes=copy.deepcopy(evidence.get('repairProbes', [])))
    return result


class CachedProbe:
    """One downloaded original; one bounded decoded excerpt per cache miss."""
    def __init__(self, source, song, folder, decoder, model_path):
        self.source, self.song, self.folder = source, song, folder
        self.decoder, self.model_path = decoder, model_path
        folder.mkdir(parents=True, exist_ok=True)

    def __call__(self, begin, end):
        begin = round(begin*SAMPLE_RATE)/SAMPLE_RATE
        end = min(self.song['duration'], round(end*SAMPLE_RATE)/SAMPLE_RATE)
        if not 0 <= begin < end or not .1 <= end-begin <= 30:
            raise RuntimeError('VOCAL_REVIEW_PROBE_BOUNDS')
        key = hashlib.sha256(json.dumps([VERSION,self.song['md5'],begin,end,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
        target = self.folder/(key+'.json')
        if target.exists():
            value = read_json(target)
            if value.get('cacheKey') == key and value.get('md5') == self.song['md5'] and value.get('sourceOffsetSeconds') == begin and value.get('requestedEndSeconds') == end and _vectors(value,allow_warnings=True) is not None:
                return value
        with tempfile.TemporaryDirectory(prefix='vocal-excerpt-', dir=self.folder) as directory:
            pcm = Path(directory)/'excerpt.f32'
            try:
                child = subprocess.run([self.decoder, '-nostdin','-v','error','-threads','1','-filter_threads','1',
                    '-ss',str(begin),'-i',str(self.source),'-t',str(end-begin),'-vn','-sn','-dn',
                    '-ac','2','-ar',str(SAMPLE_RATE),'-f','f32le','-y',str(pcm)],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
            except subprocess.TimeoutExpired:
                raise RuntimeError('VOCAL_REVIEW_DECODE_TIMEOUT') from None
            if child.returncode or not pcm.exists() or not 0 < pcm.stat().st_size <= SAMPLE_RATE*30*8 or pcm.stat().st_size % 8:
                raise RuntimeError('VOCAL_REVIEW_DECODE')
            samples = pcm.stat().st_size//8
            if abs(samples/SAMPLE_RATE-(end-begin)) > .05:
                raise RuntimeError('VOCAL_REVIEW_DECODE_DURATION')
            import numpy as np
            wave = np.fromfile(pcm, dtype='<f4').reshape(-1,2).T
            value = probe_vocals(wave, SAMPLE_RATE, self.model_path,
                center_seconds=min(samples/SAMPLE_RATE,(end-begin)/2))
        value.update(sourceOffsetSeconds=begin, md5=self.song['md5'], cacheKey=key,
                     requestedEndSeconds=end, vocalReviewVersion=VERSION)
        if _vectors(value,allow_warnings=True) is None:raise RuntimeError('VOCAL_REVIEW_EVIDENCE')
        atomic_json(target, value)
        return value


def run(settings, work, decoder, model_path, limit=None, retry=False):
    from .disk import Disk
    if limit is not None and limit < 0:raise RuntimeError('VOCAL_REVIEW_LIMIT')
    decoder = shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file() or not os.access(decoder,os.X_OK):raise RuntimeError('VOCAL_REVIEW_DECODER')
    version = subprocess.run([decoder,'-version'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
    if version.returncode or not version.stdout.startswith(b'ffmpeg version'):raise RuntimeError('VOCAL_REVIEW_DECODER')
    verify_model(model_path)
    plan = read_json(work/'baseline.json')
    curation = settings.state/'research/dj1-curation'
    owner = read_json(curation/'owner-entries.json'); rejected = read_json(curation/'owner-rejected.json')
    db = sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk = Disk(replace(settings,max_bytes=min(settings.max_bytes,40*1024**2))); count = 0
    try:
        for position,old in enumerate(plan['tracks'],1):
            target = work/'tracks'/(old['trackId']+'.json')
            if not target.exists():continue
            previous = read_json(target)
            if previous.get('status') == 'ready' and previous.get('exit',{}).get('method') not in ('reobserved-instrumental-fade','instrumental-fade'):continue
            if previous.get('vocalReviewVersion') == VERSION and not retry:continue
            if limit is not None and count >= limit:break
            asset = db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,l.integrated_lufs AS lufs,l.true_peak_dbtp AS peak,l.method AS loudnessMethod,l.status AS loudnessStatus
                FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''',(old['trackId'],)).fetchone()
            if not asset or asset['md5'] != old['md5']:raise RuntimeError('VOCAL_REVIEW_SOURCE_CHANGED')
            song = dict(asset);song['lo'] = song['lo'] or 0.;song['hi'] = song['hi'] if song['hi'] is not None else song['duration']
            if not 0 < song['bytes'] <= 40*1024**2 or not 0 < song['duration'] <= 1800:raise RuntimeError('VOCAL_REVIEW_SOURCE_LIMIT')
            if shutil.disk_usage(work).free < 512*1024**2:raise RuntimeError('VOCAL_REVIEW_DISK')
            raw = db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(old['md5'],)).fetchone()
            if raw is None:raise RuntimeError('VOCAL_REVIEW_ASR_EVIDENCE')
            evidence = restore_evidence(json.loads(raw[0]),previous)
            try:
                with tempfile.TemporaryDirectory(prefix='vocal-original-',dir=work) as directory:
                    source = Path(directory)/('source'+Path(song['remote_path']).suffix)
                    disk.download(song['remote_path'],source,expected_md5=old['md5'])
                    if source.stat().st_size != song['bytes'] or source.stat().st_size > 40*1024**2:raise RuntimeError('VOCAL_REVIEW_SOURCE_SIZE')
                    probe = CachedProbe(source,song,work/'vocal-evidence',decoder,model_path)
                    result = review_checkpoint(old,song,previous,evidence,probe,
                        owner=owner.get(old['trackId'],[]),rejected=rejected.get(old['trackId'],[]))
            except Exception as exc:
                code = str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('VOCAL_REVIEW_') else 'VOCAL_REVIEW_'+type(exc).__name__.upper()
                result = {**previous,'status':'needs-review','vocalReviewVersion':VERSION,'vocalReviewError':code}
            result['position'] = position
            atomic_json(target,result);count += 1
            print('VOCAL_REVIEWED',position,result['status'],old['trackId'],result.get('exit',{}).get('method','none'),flush=True)
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--ffmpeg',required=True)
    parser.add_argument('--model',type=Path,required=True);parser.add_argument('--limit',type=int)
    parser.add_argument('--retry',action='store_true');args=parser.parse_args()
    run(Settings(),args.work,args.ffmpeg,args.model,args.limit,args.retry)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('VOCAL_REVIEW_') else type(exc).__name__
        print('VOCAL_REVIEW_FAILED',code,flush=True);raise SystemExit(1)
