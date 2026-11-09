"""Bounded additional contexts for exit-unresolved DJ-set tracks only.

Reuses every source-pinned numeric window before at most two new20s probes per
round. A valid outgoing point and an independently checked incoming breath are
paired at45–100s. No order optimiser and no relaxation of vocal-energy limits.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import replace

from .config import Settings, atomic_json
from .fixed_set_repair import covered, entries_for, selection_error as exit_error, word_clear
from .fixed_set_resolve import read_json, restore_evidence
from .fixed_set_entry_review import check_entry, exemption
from .fixed_set_vocal_review import (CachedProbe, MODEL_SHA256, SAMPLE_RATE, VERSION as CACHE_VERSION,
    _point, _vectors, check_endpoint, finite, gain_db, quantile, source_timing)
from .vocal_probe import verify_model

VERSION = 1
MAX_PROBES = 2
MAX_CONTEXT_SECONDS = 20
MAX_TRACK_SECONDS = 180


def measure_source_timing(source, song, decoder, folder):
    """Actual EOF from one full MD5-verified source, not a metadata assumption."""
    pcm = Path(folder)/'hard-source-timing.f32'; maximum = 16000*1800*8
    try:
        try:
            child = subprocess.run([decoder,'-nostdin','-v','error','-threads','1',
                '-filter_threads','1','-i',str(source),'-vn','-sn','-dn','-ac','2',
                '-ar','16000','-f','f32le','-fs',str(maximum+8),'-y',str(pcm)],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError('HARD_REVIEW_TIMING_TIMEOUT') from None
        if child.returncode or not pcm.exists() or not 0<pcm.stat().st_size<maximum or pcm.stat().st_size%8:
            raise RuntimeError('HARD_REVIEW_TIMING_DECODE')
        return {'decodedDuration':pcm.stat().st_size/8/16000,
            'metadataDuration':song.get('metadataDuration',song['duration']),
            'sourceMD5':song['md5'],'manualEnd':bool(song.get('manualEnd')),
            'sourceTimingMethod':'full-verified-decode'}
    finally:
        pcm.unlink(missing_ok=True)


def references(*groups):
    """Preserve the complete deduplicated source-evidence union, never[:12]."""
    result = {}
    for group in groups:
        for ref in group:
            key = ref.get('cacheKey') if isinstance(ref, dict) else None
            if isinstance(key, str) and re.fullmatch('[a-f0-9]{64}', key):
                result.setdefault(key, {k: ref[k] for k in
                    ('cacheKey', 'sourceOffsetSeconds', 'durationSeconds', 'modelSha256') if k in ref})
    return list(result.values())


def all_existing(probe, refs):
    values = {}
    # The base adapter intentionally limits its small first-pass list. A
    # one-reference call preserves validation while avoiding that list cap.
    for ref in references(refs):
        for value in probe.existing([ref]):
            values.setdefault(value['cacheKey'], value)
    return list(values.values())


def _ref(value):
    return {k: value[k] for k in ('cacheKey', 'sourceOffsetSeconds', 'durationSeconds', 'modelSha256') if k in value}


def load_hints(path, baseline_revision):
    """Private ranking inputs, never a boundary verdict or model evidence."""
    if path is None:
        return None
    value = read_json(path)
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1 or not isinstance(value.get('tracks'), dict):
        raise RuntimeError('HARD_REVIEW_HINTS_VERSION')
    if value.get('baselineRevision') != baseline_revision:
        raise RuntimeError('HARD_REVIEW_HINTS_REVISION')
    return value


def hint_windows(document, song):
    """Ignore stale source/range suggestions rather than treating them as cuts."""
    if document is None:
        return []
    row = document['tracks'].get(song['id'])
    if not isinstance(row, dict) or row.get('sourceMD5') != song['md5']:
        return []
    allowed = row.get('allowedRange'); duration = row.get('sourceDuration')
    original_hi = song.get('originalAllowedEnd', song['hi'])
    metadata_duration = song.get('metadataDuration', song['duration'])
    if not isinstance(allowed, (list, tuple)) or len(allowed) != 2 or not all(finite(v) for v in allowed) or not finite(duration):
        return []
    if abs(allowed[0]-song['lo']) > .001 or abs(allowed[1]-original_hi) > .001 or abs(duration-metadata_duration) > .001:
        return []
    windows = row.get('windows')
    if not isinstance(windows, list):
        return []
    clean = []
    for window in windows:
        if not isinstance(window, dict) or any(not finite(window.get(k)) for k in ('start', 'end', 'candidateEndpoint', 'rankScore')):
            continue
        start, end, at = window['start'], window['end'], window['candidateEndpoint']
        if not 8-1e-7 <= end-start <= 20+1e-7 or start < song['lo']-.0001 or end > min(original_hi, metadata_duration)+.0001 or not start+.25 <= at-1.5 < at+1.5 <= end-.25:
            continue
        # Only bounded explanatory data survives; arbitrary indicators never
        # become accepted=True, a confidence score, or acoustic evidence.
        clean.append({'start': start, 'end': end, 'candidateEndpoint': at,
            'rankScore': window['rankScore'],
            'baselineRevision': document['baselineRevision'], 'sourceMD5': song['md5'],
            'allowedRange': list(allowed), 'sourceDuration': duration,
            'kind': window.get('kind', 'curated-context')[:80] if isinstance(window.get('kind', 'curated-context'), str) else 'curated-context',
            'reasons': [text[:180] for text in window.get('reasons', [])[:3] if isinstance(text, str)] if isinstance(window.get('reasons', []), list) else []})
    return sorted(clean, key=lambda w: (-w['rankScore'], w['start'], w['end']))


def measured_interiors(song, vectors):
    rows = []
    for value in vectors:
        if value.get('md5') != song['md5']:
            continue
        prepared = _vectors(value, allow_warnings=True)
        if prepared is None:
            continue
        begin = prepared[0]+.25; end = prepared[0]+prepared[2]-.25
        if begin < end:
            rows.append((begin, end))
    # Actual cached intervals are unioned. Two old adjacent contexts can cover
    # a proposed window even when neither context contains it on its own.
    merged = []
    for begin, end in sorted(rows):
        if merged and begin <= merged[-1][1]+1e-8:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return merged


def context_novelty(begin, end, interiors, candidate=None):
    lo, hi = begin+.25, end-.25
    overlap = sum(max(0, min(hi, b)-max(lo, a)) for a, b in interiors)
    seconds = max(0, hi-lo-overlap)
    core_covered = candidate is not None and covered(interiors, candidate-1.5, candidate+1.5)
    return seconds, core_covered


def _endpoint(vocal, point, words, gain, reference, source_proof):
    check = check_endpoint(vocal, point, words, gain, reference,
        **({'source_proof': source_proof} if source_proof is not None else {}))
    if not check.get('accepted'):
        return None
    maximum = 20*math.log10(max(1e-10, check['mixRms']))+gain
    p95 = 20*math.log10(max(1e-10, check['mixP95Rms']))+gain
    # These guards remain local too, so old cached code cannot turn a very
    # short fade into a loud instrumental interruption.
    if point['fade'] < .7 and maximum > -35:
        return None
    if p95 > -30 and point['fade'] < 2:
        return None
    return {**check, 'normalizedMixMaxDbfs': maximum, 'normalizedMixP95Dbfs': p95}


def scan_endpoints(old, song, evidence, values, rejected=(), source_proof=None):
    found = []; seen = set(); gain = gain_db(song)
    for vocal in values:
        prepared = _vectors(vocal)
        if prepared is None or vocal.get('md5') != song['md5']:
            continue
        offset, hop, duration, rows = prepared
        reference = max(.00001, quantile(rows[0], .75))
        long_candidate = False
        for width, fade, lead in ((2.8, 2.4, .2), (1.5, .8, .3), (1.6, 1.2, .2), (.7, .3, .2)):
            if fade < .7 and long_candidate:
                continue
            n = round(width/hop)
            for first in range(25, len(rows[0])-25-n+1, 1 if fade < .7 else 10):
                begin = offset+first*hop; end = round(begin+lead+fade, 4)
                if end-song['lo'] < 45 or end > song['hi'] or any(abs(end-t) < .35 for t in rejected):
                    continue
                key = (round(end, 4), fade)
                if key in seen:
                    continue
                point = {'time': end, 'fade': fade, 'fadeStart': round(end-fade, 4),
                         'actualEOF': bool(source_proof and source_proof['decodedDuration']-2.5 <= end <= source_proof['decodedDuration']-.45)}
                if not word_clear(evidence.get('words', []), point['fadeStart'], end, .3) and not point['actualEOF']:
                    continue
                # Discard obvious leading vocals before the more expensive
                # source/model/frame-map replay of the complete endpoint gate.
                a = max(0, math.floor((point['fadeStart']-.2-offset)/hop+1e-8))
                b = min(len(rows[1]), math.ceil((end+.2-offset)/hop-1e-8))
                if a >= b or 20*math.log10(max(1e-10, max(rows[1][a:b])))+gain > -37:
                    continue
                mix_max = 20*math.log10(max(1e-10, max(rows[0][a:b])))+gain
                if fade < .7 and mix_max > -35:
                    continue
                check = _endpoint(vocal, point, evidence.get('words', []), gain, reference, source_proof)
                if check is None:
                    continue
                if fade >= .7:long_candidate = True
                seen.add(key)
                found.append({'point': point, 'check': check, 'vector': vocal,
                    'score': abs(end-old['end'])/200+max(0, check['normalizedMixP95Dbfs']+40)/20})
    # Keep temporally diverse endpoints, preserving alternatives after a vocal
    # guard rejects one. Do not collapse every useful section to a single cue.
    result = []
    for candidate in sorted(found, key=lambda c: c['score']):
        if any(abs(candidate['point']['time']-v['point']['time']) < .35 and candidate['point']['fade'] == v['point']['fade'] for v in result):
            continue
        result.append(candidate)
        if len(result) >= 64:
            break
    return result


def scanned_entries(song, evidence, values, owner=(), rejected=()):
    """Model-derived breaths plus actual ASR coverage, not an absent transcript."""
    result = {}; gain = gain_db(song)
    for vocal in values:
        prepared = _vectors(vocal)
        if prepared is None or vocal.get('md5') != song['md5']:
            continue
        offset, hop, duration, _ = prepared
        first = max(song['lo'], offset+.4); last = min(song['hi'], offset+duration-.3)
        last_selected = -math.inf
        for index in range(math.ceil(first/.02), math.floor(last/.02)+1):
            at = round(index*.02, 4)
            if at-last_selected < .099:
                continue
            if any(abs(at-t) < .35 for t in rejected):
                continue
            if not covered(evidence.get('coverage', []), at-.3, at+.3) or not word_clear(evidence.get('words', []), at-.1, at+.1, .2):
                continue
            checked = check_entry(vocal, at, evidence.get('words', []), gain, song['md5'],
                                  coverage=evidence.get('coverage', []))
            if checked['accepted']:
                last_selected = at
                result.setdefault(at, {'time': at, 'kind': 'vocal-free-entry-breath',
                    'penalty': .6, 'fade': .1, 'check': checked, 'vector': vocal})
    return list(result.values())


def pair(old, song, evidence, endpoints, vectors, owner=(), rejected=(), *, certified_only=False):
    entries = entries_for(old, song['lo'], song['hi'], evidence.get('words', []),
                          evidence.get('coverage', []), [], owner)
    def select(candidates):
        best = None
        for entry in candidates:
            at = entry['time']
            if any(abs(at-t) < .35 for t in rejected):continue
            exempt = exemption(at, song, owner)
            observed = covered(evidence.get('coverage', []), at-.3, at+.3)
            clear = word_clear(evidence.get('words', []), at-.1, at+.1, .2)
            if not (exempt or observed and clear and (not certified_only or 'check' in entry)):continue
            for endpoint in endpoints:
                end = endpoint['point']['time']; span = end-at
                if 45 <= span <= 100:
                    score = endpoint['score']+entry['penalty']+abs(span-62)/45+abs(at-old['start'])/200
                    if best is None or score < best[0]:best = score,entry,endpoint
        return best
    known = select(entries)
    if known:return known
    # Source/owner/already guarded entries take priority. Deriving hundreds of
    # new model points is needed only when no existing eligible pair exists.
    return select(scanned_entries(song, evidence, vectors, owner, rejected))


def fresh_context(song, vectors, *, target=None, round_number=1, hints=(), details=False):
    """Choose an unmeasured central/quarter section, or a needed entry window."""
    interiors = measured_interiors(song, vectors)
    def selected(begin, end, reason):
        return {'start': begin, 'end': end, 'reason': reason} if details else (begin, end)
    if target is None:
        for hint in sorted(hints, key=lambda row: (-row['rankScore'], row['start'])):
            begin = hint['start']; end = min(song['duration'], song['hi'], hint['end'])
            at = hint['candidateEndpoint']
            if end-begin < 8-1e-7 or not begin+.25 <= at-1.5 < at+1.5 <= end-.25:
                continue
            new_seconds, core_covered = context_novelty(begin, end, interiors, at)
            if new_seconds < 8-1e-7 or core_covered:
                continue
            return selected(begin, end, {'source': 'curated-context-hint',
                'kind': hint['kind'], 'rankScore': hint['rankScore'],
                'candidateEndpoint': at, 'reasons': hint.get('reasons', []),
                'baselineRevision': hint.get('baselineRevision'), 'sourceMD5': hint.get('sourceMD5'),
                'actualNewUsefulSeconds': round(new_seconds, 6),
                'treatedAsProof': False})
    if target is not None:
        centres = [target, target-20, target+15]
    else:
        span = song['hi']-song['lo']
        centres = [song['lo']+span*f for f in (.5, .25, .75, .375, .625, .125, .875)]
        shift = (max(1, round_number)-1) % len(centres)
        centres = centres[shift:]+centres[:shift]
    known = []
    for value in vectors:
        prepared = _vectors(value, allow_warnings=True)
        if prepared is not None:
            known.append((prepared[0], prepared[0]+prepared[2]))
    for centre in centres:
        begin = max(0., min(song['duration']-MAX_CONTEXT_SECONDS, centre-MAX_CONTEXT_SECONDS/2))
        end = min(song['duration'], begin+MAX_CONTEXT_SECONDS)
        if end-begin < 8:
            continue
        # Refuse a nominally "new" context whose useful interior was already
        # observed. Small shifting of identical windows is not new evidence.
        if any(a <= begin+.5 and b >= end-.5 for a,b in known):
            continue
        if target is None:
            new_seconds, core_covered = context_novelty(begin, end, interiors, centre)
            if new_seconds < 8-1e-7 or core_covered:
                continue
        else:
            new_seconds, _ = context_novelty(begin, end, interiors)
        return selected(begin, end, {'source': 'targeted-entry' if target is not None else 'fraction-fallback',
            'actualNewUsefulSeconds': round(new_seconds, 6), 'treatedAsProof': False})
    return None


def review(old, song, checkpoint, evidence, probe_window, *, owner=(), rejected=(),
           existing=(), round_number=1, source_proof=None, max_seconds=MAX_TRACK_SECONDS,
           entry_only=False, ensure_source_timing=None, hints=()):
    result = copy.deepcopy(checkpoint); started = time.monotonic()
    vectors = {v['cacheKey']: v for v in existing if isinstance(v, dict) and v.get('cacheKey')}
    new_probes = 0; attempts = []; selected = None; timing_attempted = False
    while True:
        if time.monotonic()-started > max_seconds:
            raise RuntimeError('HARD_REVIEW_TRACK_TIMEOUT')
        values = list(vectors.values())
        if entry_only:
            previous = {**checkpoint, 'status':'ready'}
            if exit_error(previous):
                raise RuntimeError('HARD_REVIEW_PREVIOUS_EXIT_INVALID')
            endpoints = [{'point':checkpoint['exit'],'fixedExit':True,'score':0}]
        else:
            endpoints = scan_endpoints(old, song, evidence, values, rejected, song.get('sourceProof',source_proof))
        selected = pair(old, song, evidence, endpoints, values, owner, rejected, certified_only=entry_only)
        if selected:
            break
        if not entry_only and not endpoints and not song.get('sourceProof',source_proof) and not timing_attempted and ensure_source_timing is not None:
            timing_attempted = True
            ensure_source_timing()
            continue  # Reuse the same cached vocals with newly verified EOF.
        if new_probes >= MAX_PROBES:
            break
        # An independently quiet endpoint needs an entry, not another noisy
        # ending search. Otherwise inspect a genuinely unmeasured music section.
        target = endpoints[0]['point']['time']-62 if endpoints else None
        context = fresh_context(song, values, target=target, round_number=round_number, hints=hints, details=True)
        if context is None:
            break
        begin, end = context['start'], context['end']
        vocal = probe_window(begin, end); new_probes += 1
        if time.monotonic()-started > max_seconds:
            raise RuntimeError('HARD_REVIEW_TRACK_TIMEOUT')
        key = vocal.get('cacheKey')
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key) or vocal.get('md5') != song['md5']:
            raise RuntimeError('HARD_REVIEW_SOURCE_PROOF')
        vectors[key] = vocal
        attempts.append({'start': begin, 'end': end, 'cacheKey': key, 'valid': _vectors(vocal) is not None,
                         'contextReason': context['reason']})
    if selected:
        score, entry, candidate = selected; point = candidate['point']; check = candidate.get('check')
        outgoing = copy.deepcopy(checkpoint['exit']) if entry_only else _point(check, point, 'vocal-isolated-fade')
        if not entry_only and check['normalizedMixP95Dbfs'] > -30:
            outgoing['rampFacts'] = {'kind':'generated-release','seconds':point['fade'],
                'sourceFadeStart':point['fadeStart'],'sourceFadeEnd':point['time'],
                'normalizedMixMaxDbfs':check['normalizedMixMaxDbfs'],
                'normalizedMixP95Dbfs':check['normalizedMixP95Dbfs'],
                'naturalSilenceClaimed':False,'gainAtStart':.7,'gainAtEnd':0.}
        row = {**(checkpoint['row'] if entry_only else old), 'start': max(song['lo'], round(entry['time'],4)),
            'end': min(song['hi'], round(point['time'],4)), 'fadeIn': entry.get('fade',.1),
            'fadeOut': round(point['fade'],4), 'transitionKind':'clean-cut',
            'transitionSeconds':0,'rate':1}
        result.update(status='ready',trackId=old['trackId'],md5=old['md5'],artist=song['artist'],
            title=song['title'],row=row,entry={k:v for k,v in entry.items() if k not in ('vector','check')},
            exit=outgoing,score=score,referenceRms=checkpoint.get('referenceRms',.1) if entry_only else check['referenceRms'],old=old)
        if entry_only:
            result['entryReviewOnly'] = False
        if 'check' in entry:
            result['hardEntryCandidate'] = {**entry['check'],'cacheKey':entry['vector']['cacheKey']}
        for key in ('stereoEvidence','stereoVersion','entryProof','entryReviewVersion','entryReviewInputHash'):
            result.pop(key,None)
        result['hardReviewStatus'] = 'ready'
    else:
        result.update(status='needs-review',hardReviewStatus='needs-review',
                      hardReviewReason='no-independently-quiet-exit-and-safe-entry-pair')
    values = list(vectors.values())
    result.update(hardReviewVersion=VERSION,hardReviewRound=round_number,
        hardReviewAttempts=attempts,
        hardReviewBudget={'newContexts':new_probes,'maximumNewContexts':MAX_PROBES,
                          'maximumNewAudioSeconds':MAX_PROBES*MAX_CONTEXT_SECONDS,
                          'cachedVectors':len(existing),'maximumTrackSeconds':max_seconds},
        vocalEvidence=references(checkpoint.get('vocalEvidence',[]),[_ref(v) for v in values]),
        entryVocalEvidence=references(checkpoint.get('entryVocalEvidence',[]),[_ref(v) for v in values]),
        observedWords=copy.deepcopy(evidence.get('words',[])),
        observedCoverage=copy.deepcopy(evidence.get('coverage',[])),
        repairProbes=copy.deepcopy(evidence.get('repairProbes',[])))
    return result


def run(settings, work, decoder, model_path, limit=None, retry=False, round_number=1, hints_path=None):
    from .disk import Disk
    decoder = shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file() or not os.access(decoder,os.X_OK):raise RuntimeError('HARD_REVIEW_DECODER')
    if (limit is not None and limit<0) or type(round_number) is not int or not 1<=round_number<=20:raise RuntimeError('HARD_REVIEW_LIMIT')
    plan = read_json(work/'baseline.json'); curation=settings.state/'research/dj1-curation'
    hint_document = load_hints(hints_path, plan['revision'])
    verify_model(model_path)
    owner=read_json(curation/'owner-entries.json');bad=read_json(curation/'owner-rejected.json')
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(replace(settings,max_bytes=min(settings.max_bytes,40*1024**2)));count=0
    try:
        for position,old in enumerate(plan['tracks'],1):
            path=work/'tracks'/(old['trackId']+'.json')
            if not path.exists():continue
            previous=read_json(path)
            entry_only = bool(previous.get('entryReviewOnly'))
            if previous.get('status')=='ready':continue
            if previous.get('hardReviewVersion')==VERSION and previous.get('hardReviewRound',0)>=round_number and not retry:continue
            if limit is not None and count>=limit:break
            asset=db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,l.integrated_lufs AS lufs,
                l.true_peak_dbtp AS peak,l.method AS loudnessMethod,l.status AS loudnessStatus
                FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''',(old['trackId'],)).fetchone()
            if not asset or asset['md5']!=old['md5']:raise RuntimeError('HARD_REVIEW_SOURCE_CHANGED')
            song=dict(asset);song['manualStart']=song['lo'] is not None;song['manualEnd']=song['hi'] is not None and song['hi']<song['duration']-.001
            song['lo']=song['lo'] or 0.;song['hi']=song['duration'] if song['hi'] is None else song['hi']
            song=source_timing(song,previous)
            preferred_hints = hint_windows(hint_document, song)
            if not 0<song['bytes']<=40*1024**2 or not 0<song['duration']<=1800:raise RuntimeError('HARD_REVIEW_SOURCE_LIMIT')
            if shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('HARD_REVIEW_DISK')
            raw=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(old['md5'],)).fetchone()
            if raw is None:raise RuntimeError('HARD_REVIEW_ASR_MISSING')
            evidence=restore_evidence(json.loads(raw[0]),previous)
            timing=None
            try:
                with tempfile.TemporaryDirectory(prefix='hard-original-',dir=work) as directory:
                    source=Path(directory)/('source'+Path(song['remote_path']).suffix)
                    adapter=CachedProbe(source,song,work/'vocal-evidence',decoder,model_path)
                    refs=references(previous.get('vocalEvidence',[]),previous.get('entryVocalEvidence',[]))
                    existing=all_existing(adapter,refs)
                    downloaded=False;working=copy.deepcopy(previous)
                    def source_ready():
                        nonlocal downloaded
                        if not downloaded:
                            disk.download(song['remote_path'],source,expected_md5=old['md5'])
                            if source.stat().st_size!=song['bytes']:raise RuntimeError('HARD_REVIEW_SOURCE_SIZE')
                            downloaded=True
                    def ensure_timing():
                        nonlocal timing
                        source_ready()
                        timing=measure_source_timing(source,song,decoder,directory)
                        working.update(md5=old['md5'],eofEvidence=timing)
                        song.update(source_timing(song,working))
                    def probe(begin,end):
                        a=round(begin*SAMPLE_RATE)/SAMPLE_RATE;b=min(song['duration'],round(end*SAMPLE_RATE)/SAMPLE_RATE)
                        key=hashlib.sha256(json.dumps([CACHE_VERSION,song['md5'],a,b,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
                        cached=all_existing(adapter,[{'cacheKey':key}])
                        if cached:return cached[0]
                        source_ready()
                        return adapter(begin,end)
                    result=review(old,song,working,evidence,probe,
                        owner=owner.get(old['trackId'],[]),rejected=bad.get(old['trackId'],[]),
                        existing=existing,round_number=round_number,source_proof=song.get('sourceProof'),entry_only=entry_only,
                        ensure_source_timing=ensure_timing,hints=preferred_hints)
                    if timing:result.update(md5=old['md5'],eofEvidence=timing)
            except Exception as exc:
                code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith(('HARD_REVIEW_','VOCAL_REVIEW_')) else 'HARD_REVIEW_'+type(exc).__name__.upper()
                result={**previous,'status':'needs-review','hardReviewVersion':VERSION,
                        'hardReviewRound':round_number,'hardReviewError':code}
            if timing:result.update(md5=old['md5'],eofEvidence=timing)
            result['position']=position;atomic_json(path,result);count+=1
            print('HARD_REVIEWED',position,result['status'],old['trackId'],
                  result.get('exit',{}).get('method','none'),flush=True)
    finally:db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--ffmpeg',required=True);parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--limit',type=int);parser.add_argument('--retry',action='store_true')
    parser.add_argument('--round',type=int,default=1);parser.add_argument('--hints',type=Path);args=parser.parse_args()
    run(Settings(),args.work,args.ffmpeg,args.model,args.limit,args.retry,args.round,args.hints)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith(('HARD_REVIEW_','VOCAL_REVIEW_')) else type(exc).__name__
        print('HARD_REVIEW_FAILED',code,flush=True);raise SystemExit(1)
