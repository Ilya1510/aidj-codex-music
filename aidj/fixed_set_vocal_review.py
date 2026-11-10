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
import re
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
REVIEW_POLICY_VERSION = 2
MAX_ENDPOINT_PROBES = 3
MAX_CONTEXT_PROBES = 2
MAX_TRACK_SECONDS = 180
PARAMETERS = {'quietP95Dbfs': -43., 'absoluteMaxDbfs': -37.,
              'suppressedP95Db': -30., 'fadeMarginSeconds': .2,
              'wordPaddingSeconds': .3, 'excerptGuardSeconds': .25,
              'searchMinSeconds': 1.5, 'searchFadeSeconds': .8,
              'searchMarginSeconds': .3, 'shortMixMaxDbfs':-35., 'loudMixP95Dbfs':-30.,
              'loudMinimumFadeSeconds':2., 'EOFVocalMaxDbfs':-43., 'EOFVocalP95Dbfs':-48.,
              'EOFMixMaxDbfs':-35., 'EOFMixP95Dbfs':-40., 'EOFProofMinSeconds':1.5,
              'EOFDistanceMinSeconds':.45, 'EOFDistanceMaxSeconds':2.5,
              'EOFMetadataToleranceSeconds':.05, 'strictFullDecodeMetadataToleranceSeconds':.25,
              'probeMetadataCapToleranceSeconds':.25}
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


STRICT_TIMING_METHOD='full-verified-decode-strict-v2'
STRICT_TIMING_FIELDS=('sourceTimingMethod','decoderErrorPolicy','completeSource','decoderExitCode',
                      'sourceMD5','sampleRate','channels','pcmBytes','pcmFrames')


def strict_decode_valid(record,digest):
    """Only an actual error-fatal full decode can use the250ms metadata window."""
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{32}',digest):return False
    method=record.get('decoderMethod',record.get('sourceTimingMethod'))
    if method!=STRICT_TIMING_METHOD or record.get('sourceTimingMethod')!=STRICT_TIMING_METHOD:return False
    if record.get('decoderErrorPolicy')!='xerror' or record.get('completeSource') is not True:return False
    if type(record.get('decoderExitCode')) is not int or record['decoderExitCode']!=0:return False
    if record.get('sourceMD5')!=digest:return False
    if type(record.get('sampleRate')) is not int or record['sampleRate']!=16000:return False
    if type(record.get('channels')) is not int or record['channels']!=2:return False
    size=record.get('pcmBytes');frames=record.get('pcmFrames')
    if type(size) is not int or not 0<size<16000*1800*8 or size%8:return False
    if type(frames) is not int or frames!=size//8:return False
    duration=record.get('decodedDuration')
    return finite(duration) and abs(duration-frames/16000)<=1e-9


def source_timing(song,checkpoint):
    """Legacy EOF needs50ms agreement; new fatal-error complete PCM proof allows250ms."""
    result=dict(song);result.pop('sourceProof',None);meta=song.get('metadataDuration',song['duration'])
    result['metadataDuration']=meta
    original_hi=song.get('originalAllowedEnd',song['hi']);result['originalAllowedEnd']=original_hi
    result['manualEnd']=bool(song.get('manualEnd',False) or original_hi<meta-.001)
    eof=checkpoint.get('eofEvidence',{})
    digest=song.get('md5');pinned=checkpoint.get('md5')==digest and isinstance(digest,str) and re.fullmatch('[a-f0-9]{32}',digest)
    decoded=eof.get('decodedDuration');declared=eof.get('metadataDuration')
    timing=eof.get('decoderMethod',eof.get('sourceTimingMethod','verified-original-rms-16000-v1'))
    strict=timing==STRICT_TIMING_METHOD and strict_decode_valid(eof,digest)
    legacy=timing in ('verified-original-rms-16000-v1','full-verified-decode','verified-original-pcm-16000-stereo-v1') and eof.get('sourceMD5',digest)==digest
    timing_valid=legacy or strict
    if pinned and timing_valid and finite(decoded) and finite(declared) and abs(declared-meta)<=.001 and 0<decoded<=1800 and abs(decoded-meta)<=.25:
        result['decodedDuration']=decoded
        result['duration']=min(meta,decoded);result['hi']=min(original_hi,result['duration'])
        eof_tolerance=.25 if strict else .05
        if not eof.get('manualEnd') and not result['manualEnd'] and abs(decoded-meta)<=eof_tolerance:
            result['sourceProof']={'sourcePinned':True,'md5':digest,'decodedDuration':decoded,
                'metadataDuration':meta,'allowedStart':song['lo'],'allowedEnd':original_hi,
                'manualEnd':False,'decoderMethod':timing}
            if strict:
                result['sourceProof'].update({key:eof[key] for key in STRICT_TIMING_FIELDS})
                result['sourceProof']['metadataToleranceSeconds']=.25
    return result


def _valid_eof_source(proof,evidence,point):
    if not isinstance(proof,dict) or proof.get('sourcePinned') is not True or proof.get('manualEnd') is not False:return False
    method=proof.get('decoderMethod')
    if method not in ('verified-original-rms-16000-v1','full-verified-decode','verified-original-pcm-16000-stereo-v1',STRICT_TIMING_METHOD):return False
    md5=proof.get('md5')
    if not isinstance(md5,str) or not re.fullmatch('[a-f0-9]{32}',md5) or evidence.get('md5')!=md5:return False
    if any(not finite(proof.get(k)) for k in ('decodedDuration','metadataDuration','allowedStart','allowedEnd')):return False
    eof,meta=proof['decodedDuration'],proof['metadataDuration']
    strict=method==STRICT_TIMING_METHOD
    if strict and not strict_decode_valid(proof,md5):return False
    tolerance=.25 if strict else .05
    return (0<eof<=1800 and abs(eof-meta)<=tolerance and 0<=proof['allowedStart']<eof and
        proof['allowedEnd']>=meta-.001 and proof['allowedEnd']<=meta+.001 and
        eof-2.5<=point['time']<=eof-.45 and point['fadeStart']-.2>=proof['allowedStart'])


def replay_source(item,current_source):
    """Bind persisted vocal proof to today's source range, timing and gain."""
    check=item.get('exit',{}).get('vocalCheck',{})
    current_gain=gain_db(current_source)
    if not finite(check.get('playbackGainDb')) or abs(current_gain-check['playbackGainDb'])>1e-6:
        raise ValueError('vocal-proof-gain-changed')
    trusted=None
    if check.get('criterion')=='physical-EOF-quiet':
        trusted=source_timing(current_source,item).get('sourceProof')
        claimed=check.get('sourceProof')
        if trusted is None or trusted!=claimed or item.get('exit',{}).get('sourceProof',trusted)!=trusted:
            raise ValueError('vocal-proof-source-changed')
    return current_gain,trusted


def vocal_word_clear(words,start,end,padding=.3):
    # Ignore floating point equality noise, not real vocal overlap.
    return word_clear(words,start+1e-7,end-1e-7,padding)


def check_endpoint(evidence, point, words, playback_gain_db, reference_rms, *, source_proof=None):
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
    mix_max_db=20*math.log10(max(1e-10,max(mix)))+playback_gain_db
    mix_p95_db=20*math.log10(max(1e-10,quantile(mix,.95)))+playback_gain_db
    asr_clear = vocal_word_clear(words, point['fadeStart'], point['time'], PARAMETERS['wordPaddingSeconds'])
    proof=source_proof or evidence.get('sourceProof') or point.get('sourceProof')
    strong_eof=(_valid_eof_source(proof,evidence,point) and b-a>=1.5-1e-6 and
        normalized_max<=-43 and normalized_p95<=-48 and mix_max_db<=-35 and mix_p95_db<=-40)
    ignored=bool(not asr_clear and strong_eof)
    if not asr_clear:criterion='physical-EOF-quiet' if strong_eof else None
    ramp_ok=not (mix_p95_db>-30 and point['fade']<2-1e-7 or point['fade']<.7-1e-7 and mix_max_db>-35)
    if not ramp_ok:criterion=None
    result = {'accepted': criterion is not None, 'reason': 'accepted' if criterion else 'music-release-too-short' if not ramp_ok else 'vocal-over-fade' if not asr_clear else 'vocal-not-quiet',
              'criterion': criterion, 'parameters': dict(PARAMETERS), 'modelSha256': MODEL_SHA256,
              'proofStart': a, 'proofEnd': b, 'sourceOffsetSeconds': offset,
              'sourceEndSeconds': offset+duration, 'frameFirst': first, 'frameLastExclusive': last,
              'frameCount': last-first, 'playbackGainDb': playback_gain_db,
              'vocalMaxRms': maximum, 'vocalP95Rms': p95,
              'normalizedVocalMaxDbfs': normalized_max, 'normalizedVocalP95Dbfs': normalized_p95,
              'vocalToMixP95Db': ratio_p95, 'mixRms': max(mix),
              'mixP95Rms': quantile(mix, .95), 'normalizedMixMaxDbfs':mix_max_db,
              'normalizedMixP95Dbfs':mix_p95_db,'rampAcceptable':ramp_ok, 'instrumentalMaxRms': max(instrumental),
              'relativeMax': max(mix)/reference_rms, 'referenceRms': reference_rms,
              'asrClear': asr_clear, 'asrIgnored': ignored,
              'asrIgnoredReason':'terminal-ASR-conflict-corroborated-by-vocal-and-stereo-decay' if ignored else None,
              'sourceProof':proof if ignored else None,
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
            'vocalCheck': check, 'asrIgnored': check['asrIgnored'],
            'asrIgnoredReason':check.get('asrIgnoredReason'),'sourceProof':check.get('sourceProof')}


def _search_candidates(old, song, evidence, vocal, owner, rejected, extra_entries=()):
    """Find sustained low-vocal1.5s intervals, then pair only already safe entries."""
    prepared = _vectors(vocal)
    if prepared is None:
        return []
    offset, hop, duration, values = prepared
    entries = entries_for(old, song['lo'], song['hi'], evidence.get('words', []),
                          evidence.get('coverage', []), [], owner)
    seen_entries={round(e['time'],4) for e in entries}
    entries.extend(e for e in extra_entries if isinstance(e,dict) and finite(e.get('time')) and round(e['time'],4) not in seen_entries)
    entries=[entry for entry in entries if _entry_valid(entry,old,song,evidence,owner)]
    gain = gain_db(song); candidate_points = []
    reference=max(1e-5,quantile(values[0],.75))
    # Loud music needs a real long engineered release; short fades are reserved
    # for independently quiet stereo sound, not merely an absence of vocals.
    for minimum,fade in ((2.8,2.4),(1.5,.8),(.7,.3),(1.6,1.2)):
        if minimum<1 and candidate_points:continue
        n=math.ceil(minimum/hop-1e-8)
        stride=1 if minimum<1 else 10
        for first in range(math.ceil(PARAMETERS['excerptGuardSeconds']/hop),len(values[0])-n,stride):
            a=offset+first*hop;b=a+n*hop
            if a<song['lo'] or b>min(song['hi'],offset+duration-PARAMETERS['excerptGuardSeconds']):continue
            voice=values[1][first:first+n];ratio=values[3][first:first+n]
            maximum=20*math.log10(max(1e-10,max(voice)))+gain
            quiet=20*math.log10(max(1e-10,quantile(voice,.95)))+gain<=PARAMETERS['quietP95Dbfs']
            if maximum>PARAMETERS['absoluteMaxDbfs'] or not(quiet or quantile(ratio,.95)<=PARAMETERS['suppressedP95Db']):continue
            margin=(minimum-fade)/2
            end=round(a+margin+fade,4)
            point={'time':end,'fade':fade,'fadeStart':round(end-fade,4),'regionStart':a,'regionEnd':b}
            if any(abs(end-t)<.35 for t in rejected):continue
            eligible=[entry for entry in entries if 45<=end-entry['time']<=100 and not any(abs(entry['time']-t)<.35 for t in rejected)]
            if not eligible:continue
            check=check_endpoint(vocal,point,evidence.get('words',[]),gain,reference)
            if not check['accepted']:continue
            for entry in eligible:
                span=end-entry['time']
                score=entry.get('penalty',1)+abs(span-62)/45+abs(entry['time']-old['start'])/200+abs(end-old['end'])/100
                candidate_points.append({'score':score,'entry':entry,'exit':point})
    return sorted(candidate_points, key=lambda item:item['score'])[:32]


def _tail_candidates(old,song,evidence,owner,rejected,extra_entries=()):
    """An engineered release before allowed end, never an actual-EOF exception."""
    end=round(song['hi']-.7,4)
    entries=entries_for(old,song['lo'],song['hi'],evidence.get('words',[]),
                        evidence.get('coverage',[]),[],owner)
    entries.extend(e for e in extra_entries if isinstance(e,dict))
    candidates=[]
    for fade in (2.4,2.0,1.5,1.2):
        has_source=bool(song.get('sourceProof'))
        if not word_clear(evidence.get('words',[]),end-fade,end,PARAMETERS['wordPaddingSeconds']) and not has_source:continue
        for entry in entries:
            if not _entry_valid(entry,old,song,evidence,owner):continue
            span=end-entry['time']
            if not 45<=span<=100 or any(min(abs(entry['time']-t),abs(end-t))<.35 for t in rejected):continue
            candidates.append({'entry':entry,'exit':{'time':end,'fade':fade,'fadeStart':end-fade,
                'method':'allowed-tail-candidate','actualEOF':False},
                'score':entry.get('penalty',1)+abs(span-62)/45+abs(entry['time']-old['start'])/200+(2.4-fade)})
    ordered=sorted(candidates,key=lambda row:row['score'])
    # The vocal verdict is endpoint-specific, not entry-specific. Keep the best
    # already guarded entry for each fade instead of copying hundreds of proofs.
    return [next(row for row in ordered if row['exit']['fade']==fade) for fade in (2.4,2.0,1.5,1.2)
            if any(row['exit']['fade']==fade for row in ordered)]


def review_checkpoint(old, song, checkpoint, evidence, probe_window, *, owner=(), rejected=(), search=True, max_seconds=MAX_TRACK_SECONDS, existing_evidence=()):
    """probe_window(begin,end) returns cached numeric evidence in source coordinates."""
    result = copy.deepcopy(checkpoint); attempts = []; proofs = []
    deadline = time.monotonic()+max_seconds; measured = {}; known = {}
    for value in list(existing_evidence)[:12]:
        prepared=_vectors(value,allow_warnings=True)
        if prepared is not None:
            offset,_,duration,_=prepared
            end=value.get('requestedEndSeconds',offset+duration)
            if finite(end):known[(round(offset*SAMPLE_RATE),round(end*SAMPLE_RATE))]={**value,**({'sourceProof':song['sourceProof']} if song.get('sourceProof') else {})}
    def bounded_probe(begin,end):
        key=(round(begin*SAMPLE_RATE),round(end*SAMPLE_RATE))
        if key in measured:return measured[key]
        if key in known:return known[key]
        if time.monotonic()>deadline:raise RuntimeError('VOCAL_REVIEW_TRACK_TIMEOUT')
        if len(measured)>=MAX_ENDPOINT_PROBES+MAX_CONTEXT_PROBES:raise RuntimeError('VOCAL_REVIEW_PROBE_BUDGET')
        value=probe_window(begin,end)
        if song.get('sourceProof'):value={**value,'sourceProof':song['sourceProof']}
        if time.monotonic()>deadline:raise RuntimeError('VOCAL_REVIEW_TRACK_TIMEOUT')
        measured[key]=value
        return value
    current = {'entry': checkpoint.get('entry'), 'exit': checkpoint.get('exit'), 'score': checkpoint.get('score', 0)}
    candidates = [current, *checkpoint.get('alternatives', [])[:MAX_ENDPOINT_PROBES-2]]
    extra_entries=[checkpoint['entry']] if isinstance(checkpoint.get('entry'),dict) else []
    seen = set()
    reference = checkpoint.get('referenceRms', .1)
    reference = reference if finite(reference) and reference > 0 else .1
    existing_ready = checkpoint.get('status') == 'ready'

    def inspect(candidate, vocal=None):
        entry, point = candidate.get('entry'), candidate.get('exit')
        if not isinstance(entry, dict) or not isinstance(point, dict):return None
        if not _entry_valid(entry, old, song, evidence, owner):return None
        end = point.get('time'); fade = point.get('fade')
        if not finite(end) or not finite(fade):return None
        end=round(end,4);fade=round(fade,4)
        if not 45 <= end-entry['time'] <= 100 or end > song['hi']:
            return None
        if any(min(abs(entry['time']-t),abs(end-t)) < .35 for t in rejected):return None
        signature = (round(entry['time'],4),round(end,4),round(fade,4))
        if signature in seen:return None
        seen.add(signature)
        point = {**point,'time':end,'fade':fade,'fadeStart':round(end-fade,4)}
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
            ramp={**ramp,'time':round(ramp['time'],4),'fade':round(ramp['fade'],4)}
            ramp['fadeStart']=round(ramp['time']-ramp['fade'],4)
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

    searched=set()
    def search_window(vocal):
        key=vocal.get('cacheKey') or (vocal.get('sourceOffsetSeconds'),vocal.get('durationSeconds'))
        if key in searched:return None
        searched.add(key)
        proofs.append({k:vocal[k] for k in ('cacheKey','sourceOffsetSeconds','durationSeconds','modelSha256') if k in vocal})
        for candidate in _search_candidates(old,song,evidence,vocal,owner,rejected,extra_entries):
            chosen=inspect(candidate,vocal)
            if chosen:return chosen
        return None

    # Reuse all known numeric windows before asking for another model call.
    selected = None
    if search:
        for vocal in known.values():
            selected=search_window(vocal)
            if selected:break
    if selected is None:
        for candidate in candidates:
            selected = inspect(candidate)
            if selected:break
            if search:
                for vocal in measured.values():
                    selected=search_window(vocal)
                    if selected:break
                if selected:break
    if selected is None and search:
        for candidate in _tail_candidates(old,song,evidence,owner,rejected,extra_entries):
            selected=inspect(candidate)
            if selected:break
            # All tail entries use the same endpoint and numeric window. A
            # different entry does not justify repeatedly running the model.
            if measured:
                selected=search_window(next(reversed(measured.values())))
                if selected:break
    if selected is None and search and song['hi']-song['lo'] >= 45:
        # Prefer the current repaired exit, then the actual allowed outro. The
        # baseline is a fallback only when one of these windows coincides.
        current_end=checkpoint.get('exit',{}).get('time',old['end'])
        if not finite(current_end):current_end=old['end']
        windows=[]
        for end in (min(song['hi'],current_end+10),song['hi'],min(song['hi'],old['end']+10)):
            begin=max(song['lo'],end-20)
            key=(round(begin*SAMPLE_RATE),round(end*SAMPLE_RATE))
            if end-begin>=1.5 and key not in windows:windows.append(key)
        contexts=0
        for first,last in windows:
            key=(first,last)
            cached=known.get(key,measured.get(key))
            if cached is not None:
                selected=search_window(cached)
                if selected:break
                continue
            if contexts>=MAX_CONTEXT_PROBES:break
            contexts+=1
            vocal=bounded_probe(first/SAMPLE_RATE,last/SAMPLE_RATE)
            selected=search_window(vocal)
            if selected:break
    if selected is not None:
        result.update(selected)
    else:
        result.update(status='needs-review', vocalReviewReason='no-independent-vocal-free-fade')
    result.update(vocalReviewVersion=VERSION, vocalReviewPolicyVersion=REVIEW_POLICY_VERSION, vocalReviewParameters=dict(PARAMETERS),
                  vocalReviewAttempts=attempts, vocalEvidence=proofs,
                  vocalReviewBudget={'endpointLimit':MAX_ENDPOINT_PROBES,'contextLimit':MAX_CONTEXT_PROBES,
                      'distinctWindows':len(measured),'cachedWindows':len(known),
                      'maxRequestedAudioSeconds':MAX_ENDPOINT_PROBES*5.4+MAX_CONTEXT_PROBES*20,
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

    def existing(self,references,limit=12):
        if limit is not None and (type(limit) is not int or limit<0):
            raise ValueError('Vocal reference limit must be nonnegative or None')
        values=[];seen=set()
        for reference in references if limit is None else references[:limit]:
            if not isinstance(reference,dict):continue
            key=reference.get('cacheKey','')
            if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key) or key in seen:continue
            seen.add(key);path=self.folder/(key+'.json')
            if not path.exists():continue
            value=read_json(path)
            if value.get('cacheKey')!=key or value.get('md5')!=self.song['md5'] or _vectors(value,allow_warnings=True) is None:continue
            begin=value.get('sourceOffsetSeconds');end=value.get('requestedEndSeconds')
            if not finite(begin) or not finite(end):continue
            expected=hashlib.sha256(json.dumps([VERSION,self.song['md5'],begin,end,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
            if expected==key:values.append(value)
        return values

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
            if previous.get('status') == 'ready' and previous.get('exit',{}).get('method') not in ('reobserved-instrumental-fade','instrumental-fade',*METHODS):continue
            if previous.get('vocalReviewPolicyVersion') == REVIEW_POLICY_VERSION and not retry:continue
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
                    source_checkpoint={**previous,'md5':old['md5']}
                    song=source_timing(song,source_checkpoint)
                    probe = CachedProbe(source,song,work/'vocal-evidence',decoder,model_path)
                    result = review_checkpoint(old,song,previous,evidence,probe,
                        owner=owner.get(old['trackId'],[]),rejected=rejected.get(old['trackId'],[]),
                        existing_evidence=probe.existing(previous.get('vocalEvidence',[])))
            except Exception as exc:
                code = str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('VOCAL_REVIEW_') else 'VOCAL_REVIEW_'+type(exc).__name__.upper()
                result = {**previous,'status':'needs-review','vocalReviewVersion':VERSION,'vocalReviewPolicyVersion':REVIEW_POLICY_VERSION,'vocalReviewError':code}
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
