"""Serial incoming-boundary review; a short vocal-free breath is not a word detector.

Reuse source-pinned UVR vectors before one bounded5.4s inference. Singing after
the breath is expected. Source/human exemptions remain explicit and never claim
an acoustic proof. Run after exit selection, before final stereo verification.
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
from dataclasses import replace

from .config import Settings, atomic_json
from .fixed_set_repair import covered, word_clear
from .fixed_set_resolve import read_json, restore_evidence
from .fixed_set_vocal_review import CachedProbe, VERSION as CACHE_VERSION, _vectors, finite, gain_db, quantile
from .vocal_probe import MODEL_SHA256, verify_model

VERSION = 3
PARAMETERS = {'beforeSeconds': .15, 'afterSeconds': .05, 'excerptGuardSeconds': .25,
              'quietP95Dbfs': -43., 'absoluteMaxDbfs': -37., 'suppressedP95Db': -30.,
              'asrMarginSeconds': .1, 'asrPaddingSeconds': .2,
              'asrCoverageMarginSeconds': .3, 'searchRadiusSeconds': 2.}
EXEMPTIONS = {'physical-source-start', 'manual-range-start', 'owner-approved'}
ONSET_METHOD = 'vocal-breath-before-new-word'
ONSET_PARAMETERS = {'preStartSeconds':-.15,'preEndSeconds':-.02,
                    'postStartSeconds':.025,'postEndSeconds':.35,
                    'coverageBeforeSeconds':.3,'coverageAfterSeconds':.35,
                    'excerptGuardSeconds':.25,'wordCrossingMarginSeconds':.02,
                    'priorWordBreathSeconds':.08,'minimumPrerollSeconds':.1,
                    'preferredPrerollSeconds':.12,'maximumWordDelaySeconds':.35,
                    'minimumRiseDb':8.,'quietP95Dbfs':-43.,'absoluteMaxDbfs':-37.,
                    'suppressedP95Db':-30.,'maximumFadeInSeconds':.08,
                    'fadeBeforeOnsetSeconds':.03,'minimumFadeInSeconds':.005}
MAX_CACHED_EVIDENCE_BYTES=2*1024**2


def discover_cached_references(folder):
    """Index one bounded JSON at a time; retain small refs, never raw arrays.

    This repairs lost checkpoint references without changing their evidence.
    The normal CachedProbe adapter still reloads/validates every selected raw
    file, and the final entry gate independently replays its measurements.
    """
    index={}
    if not Path(folder).is_dir():return index
    with os.scandir(folder) as files:
        for entry in files:
            if not re.fullmatch('[a-f0-9]{64}\\.json',entry.name):continue
            try:
                if not entry.is_file(follow_symlinks=False) or not 0<entry.stat(follow_symlinks=False).st_size<=MAX_CACHED_EVIDENCE_BYTES:continue
                with open(entry.path,'rb') as stream:
                    encoded=stream.read(MAX_CACHED_EVIDENCE_BYTES+1)
                if len(encoded)>MAX_CACHED_EVIDENCE_BYTES:continue
                value=json.loads(encoded)
                if not isinstance(value,dict):continue
                key=entry.name[:-5];digest=value.get('md5')
                if (value.get('cacheKey')!=key or not isinstance(digest,str) or
                        not re.fullmatch('[a-f0-9]{32}',digest) or value.get('modelSha256')!=MODEL_SHA256):continue
                begin,end=value.get('sourceOffsetSeconds'),value.get('requestedEndSeconds')
                duration=value.get('durationSeconds')
                if (not finite(begin) or not finite(end) or not finite(duration) or
                        not 0<=begin<end<=1800 or not .1-1e-7<=end-begin<=30+1e-7 or
                        abs(duration-(end-begin))>.05+1e-7):continue
                expected=hashlib.sha256(json.dumps([CACHE_VERSION,digest,begin,end,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
                if expected!=key or _vectors(value,allow_warnings=True) is None:continue
                reference={field:value[field] for field in ('cacheKey','sourceOffsetSeconds',
                    'durationSeconds','requestedEndSeconds','modelSha256')}
                index.setdefault(digest,[]).append(reference)
            except (OSError,ValueError,RuntimeError,UnicodeError,RecursionError):
                continue
            finally:
                # No previous raw arrays survive while parsing the next file.
                value=None;encoded=None
    return index


def entry_cached_references(item,discovered=()):
    """Keep the selected proof first, then checkpoint refs and recovered refs."""
    refs=[];seen=set()
    for reference in [item.get('entryProof',{}),*item.get('entryVocalEvidence',[]),
                      *item.get('vocalEvidence',[]),*discovered]:
        if not isinstance(reference,dict):continue
        key=reference.get('cacheKey')
        if isinstance(key,str) and re.fullmatch('[a-f0-9]{64}',key) and key not in seen:
            refs.append(reference);seen.add(key)
    return refs


def exemption(point, song, owner=()):
    if not finite(point) or not song['lo']-.0001 <= point < song['hi']:
        return None
    if abs(point) <= .0001 and abs(song['lo']) <= .0001:
        return 'physical-source-start'
    if song.get('manualStart') is True and song['lo'] > 0 and abs(point-song['lo']) <= .0001:
        return 'manual-range-start'
    if any(finite(t) and abs(point-t) <= .0001 for t in owner):
        return 'owner-approved'
    return None


def check_entry(vocal, point, words, playback_gain_db, source_md5, *, coverage=()):
    return _check_entry(vocal,point,words,playback_gain_db,source_md5,_vectors(vocal),coverage=coverage)


def _check_entry(vocal, point, words, playback_gain_db, source_md5, prepared, *, coverage=()):
    reject = lambda reason: {'accepted': False, 'reason': reason}
    if not finite(point) or not finite(playback_gain_db):
        return reject('invalid-entry-point')
    if not isinstance(source_md5,str) or not re.fullmatch('[a-f0-9]{32}',source_md5) or vocal.get('md5') != source_md5:
        return reject('entry-source-mismatch')
    if prepared is None:
        return reject('invalid-entry-vocal-evidence')
    offset, hop, duration, values = prepared
    a = point-PARAMETERS['beforeSeconds']; b = point+PARAMETERS['afterSeconds']
    guard = PARAMETERS['excerptGuardSeconds']
    if a < offset+guard-1e-7 or b > offset+duration-guard+1e-7:
        return reject('incomplete-entry-context')
    first = max(0, math.floor((a-offset)/hop+1e-8))
    last = min(len(values[0]), math.ceil((b-offset)/hop-1e-8))
    if first >= last or offset+first*hop > a+1e-6 or offset+last*hop < b-1e-6:
        return reject('incomplete-entry-frames')
    mix, voice, _, ratio = [v[first:last] for v in values]
    maximum = 20*math.log10(max(1e-10, max(voice)))+playback_gain_db
    p95 = 20*math.log10(max(1e-10, quantile(voice,.95)))+playback_gain_db
    rp95 = quantile(ratio,.95)
    criterion = ('absolute-quiet' if maximum <= -37 and p95 <= -43 else
                 'suppressed-vocal' if maximum <= -37 and rp95 <= -30 else None)
    asr_clear = word_clear(words, point-.1, point+.1, .2)
    asr_observed = covered(coverage,point-.3,point+.3)
    return {'accepted': criterion is not None and asr_clear and asr_observed,
            'reason': 'entry-asr-unobserved' if not asr_observed else 'vocal-at-entry' if not asr_clear else 'accepted' if criterion else 'entry-vocal-not-quiet',
            'method': 'vocal-free-entry-breath', 'criterion': criterion,
            'parameters': dict(PARAMETERS), 'modelSha256': MODEL_SHA256,
            'sourceMD5': source_md5, 'sourceStart': point,
            'proofStart': a, 'proofEnd': b, 'sourceOffsetSeconds': offset,
            'sourceEndSeconds': offset+duration, 'frameFirst': first,
            'frameLastExclusive': last, 'frameCount': last-first,
            'playbackGainDb': playback_gain_db, 'normalizedVocalMaxDbfs': maximum,
            'normalizedVocalP95Dbfs': p95, 'vocalToMixP95Db': rp95,
            'normalizedMixMaxDbfs': 20*math.log10(max(1e-10,max(mix)))+playback_gain_db,
            'asrClear': asr_clear, 'asrCoverageComplete':asr_observed,
            'asrCoverageStart':point-.3, 'asrCoverageEnd':point+.3,
            'verifiedWordBoundary': False,
            'naturalSilenceClaimed': False}


def fingerprint(item, song, owner):
    values = [VERSION, MODEL_SHA256, item.get('md5'), item.get('row'), song['lo'],
              song['hi'], song.get('manualStart'), gain_db(song), list(owner),
              item.get('observedWords', []),item.get('observedCoverage',[])]
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def check_entry_onset(vocal,point,words,playback_gain_db,source_md5,*,coverage=(),fade_in=.08):
    """A measured breath BEFORE a new word, separate from the strict quiet tier.

    This is not a phoneme/word detector: all-confidence ASR crossings veto it.
    Preroll also leaves time for the player's50ms volume tick before the attack.
    """
    return _check_entry_onset(vocal,point,words,playback_gain_db,source_md5,_vectors(vocal),
                              coverage=coverage,fade_in=fade_in)


def _check_entry_onset(vocal,point,words,playback_gain_db,source_md5,prepared,*,coverage=(),fade_in=.08):
    reject=lambda reason:{'accepted':False,'reason':reason,'method':ONSET_METHOD}
    if not finite(point) or not finite(playback_gain_db) or not finite(fade_in) or fade_in<.005:
        return reject('invalid-entry-point')
    if not isinstance(source_md5,str) or not re.fullmatch('[a-f0-9]{32}',source_md5) or vocal.get('md5')!=source_md5:
        return reject('entry-source-mismatch')
    if prepared is None:return reject('invalid-entry-vocal-evidence')
    offset,hop,duration,values=prepared
    a,b=point-.3,point+.35
    if a<offset+.25-1e-7 or b>offset+duration-.25+1e-7:
        return reject('incomplete-entry-context')
    if not covered(coverage,a,b):return reject('entry-asr-unobserved')
    observed=[]
    for word in words:
        if (not isinstance(word,(list,tuple)) or len(word)<3 or
                not finite(word[0]) or not finite(word[1]) or word[0]>word[1]):
            return reject('invalid-entry-words')
        observed.append(word)
    if any(x<point+.02-1e-7 and y>point-.02+1e-7 for x,y,*_ in observed):
        return reject('entry-word-crosses-point')
    previous=[y for x,y,*_ in observed if y<=point-.02+1e-7]
    if previous and max(previous)>point-.08+1e-7:return reject('entry-prior-word-too-close')
    upcoming=sorted((w for w in observed if w[0]>=point+.02-1e-7),key=lambda w:w[0])
    if not upcoming:return reject('entry-new-word-unobserved')
    next_start=upcoming[0][0]
    if any(w[2] is not True for w in upcoming if abs(w[0]-next_start)<1e-7):
        return reject('entry-new-word-not-confident')
    word_delay=next_start-point
    if not .1-1e-7<=word_delay<=.35+1e-7:return reject('entry-new-word-preroll')
    def region(lo,hi):
        first=max(0,math.floor((lo-offset)/hop+1e-8))
        last=min(len(values[0]),math.ceil((hi-offset)/hop-1e-8))
        if first>=last or offset+first*hop>lo+1e-6 or offset+last*hop<hi-1e-6:return None
        mix,voice,_,ratio=[v[first:last] for v in values]
        db=lambda value:20*math.log10(max(1e-10,value))+playback_gain_db
        return {'start':lo,'end':hi,'frameFirst':first,'frameLastExclusive':last,
                'vocalMaxDbfs':db(max(voice)),'vocalP95Dbfs':db(quantile(voice,.95)),
                'vocalToMixP95Db':quantile(ratio,.95),'mixMaxDbfs':db(max(mix))}
    pre=region(point-.15,point-.02);post=region(point+.025,point+.35)
    if pre is None or post is None:return reject('incomplete-entry-frames')
    criterion=('absolute-quiet' if pre['vocalMaxDbfs']<=-37 and pre['vocalP95Dbfs']<=-43 else
               'suppressed-vocal' if pre['vocalMaxDbfs']<=-37 and pre['vocalToMixP95Db']<=-30 else None)
    if not criterion:return reject('entry-prebreath-not-quiet')
    rise=post['vocalP95Dbfs']-pre['vocalP95Dbfs']
    if rise<8-1e-7:return reject('entry-new-voice-rise-too-small')
    # Every touching frame counts, including one brief early voice frame. Never
    # locate onset using only a high post-window percentile.
    threshold=pre['vocalP95Dbfs']+8
    first=max(0,math.floor((point-.02-offset)/hop+1e-8))
    last=min(len(values[1]),math.ceil((point+.35-offset)/hop-1e-8))
    rising=[i for i in range(first,last) if
            20*math.log10(max(1e-10,values[1][i]))+playback_gain_db>=threshold]
    if not rising:return reject('entry-new-voice-unobserved')
    rising_time=offset+rising[0]*hop
    delay=rising_time-point
    if delay<.1-1e-7:return reject('entry-new-voice-preroll')
    cap=min(.08,delay-.03,word_delay-.03)
    chosen=round(min(fade_in,cap),4)
    if chosen>cap+1e-10:chosen=round(chosen-.0001,4)
    if chosen<.005:return reject('entry-fade-in-cannot-finish-before-word')
    return {'accepted':True,'reason':'accepted','method':ONSET_METHOD,'criterion':criterion,
            'parameters':dict(ONSET_PARAMETERS),'modelSha256':MODEL_SHA256,
            'sourceMD5':source_md5,'sourceStart':point,'proofStart':point-.15,'proofEnd':point+.35,
            'sourceOffsetSeconds':offset,'sourceEndSeconds':offset+duration,
            'playbackGainDb':playback_gain_db,'pre':pre,'post':post,'vocalRiseDb':rise,
            'risingVoiceTime':rising_time,'risingVoiceDelaySeconds':delay,
            'risingVoiceFrame':rising[0],'risingThresholdDbfs':threshold,
            'nextWordStart':next_start,'nextWordEnd':upcoming[0][1],
            'nextWordConfident':True,'nextWordDelaySeconds':word_delay,
            'priorWordEnd':max(previous) if previous else None,
            'fadeInCapSeconds':cap,'fadeInSeconds':chosen,
            'asrCoverageStart':a,'asrCoverageEnd':b,'asrCoverageComplete':True,
            'asrWordCrossingClear':True,'verifiedWordBoundary':False,
            'naturalSilenceClaimed':False,'lowSpeechProbabilityUsed':False}


def review_checkpoint(item, song, words, probe_window, *, coverage=(), owner=(), rejected=(), existing_evidence=()):
    result = copy.deepcopy(item); row = result.get('row')
    if not row or item.get('md5') != song.get('md5'):
        raise RuntimeError('ENTRY_REVIEW_SOURCE_CHANGED')
    result['observedWords'] = copy.deepcopy(words)
    result['observedCoverage'] = copy.deepcopy(list(coverage))
    original = row['start']; kind = exemption(original,song,owner)
    if any(abs(original-t) < .35 for t in rejected):
        kind = None
    attempts = []; selected = None; chosen_vector = None; new_probes = 0
    known = [v for v in existing_evidence if v.get('md5') == song['md5'] and _vectors(v) is not None]
    observed_vectors = list(known)
    if kind:
        selected = {'accepted':True, 'method':kind, 'sourceStart':original,
                    'sourceMD5':song['md5'], 'verifiedWordBoundary':False,
                    'acousticProofClaimed':False}
    else:
        minimum = min(45.,song['hi']-song['lo'])
        def valid(t):
            return (song['lo'] <= t < row['end'] <= song['hi']+.0001 and
                    minimum-.0001 <= row['end']-t <= 100.0001 and
                    row.get('fadeIn',0)+row.get('fadeOut',0) < row['end']-t and
                    not any(abs(t-r) < .35 for r in rejected))
        points = [original]
        points.extend(round(original+i*.01,4) for i in sorted(range(-200,201),key=lambda n:(abs(n),n)) if i)
        def search(vectors):
            nonlocal selected,chosen_vector
            # Validate each immutable numerical excerpt once per search. Final
            # assembly still calls the public checks against freshly read data.
            prepared={id(v):_vectors(v) for v in vectors}
            for t in points:
                if not valid(t):continue
                for vector in vectors:
                    checked = _check_entry(vector,t,words,gain_db(song),song['md5'],prepared[id(vector)],coverage=coverage)
                    if t == original and checked['reason'] != 'incomplete-entry-context':attempts.append(checked)
                    if not checked['accepted']:
                        onset=_check_entry_onset(vector,t,words,gain_db(song),song['md5'],prepared[id(vector)],
                            coverage=coverage,fade_in=row.get('fadeIn',.08))
                        if onset['accepted']:
                            # When possible keep120ms of measured preroll, not
                            # merely100ms, without escaping source/span/vetoes.
                            delay=min(onset['risingVoiceDelaySeconds'],onset['nextWordDelaySeconds'])
                            refined=round(t-max(0.,.12-delay),4)
                            if refined!=t and abs(refined-original)<=2 and valid(refined):
                                longer=_check_entry_onset(vector,refined,words,gain_db(song),song['md5'],prepared[id(vector)],
                                    coverage=coverage,fade_in=row.get('fadeIn',.08))
                                if longer['accepted']:onset=longer
                            checked=onset
                        elif t==original:attempts.append(onset)
                    if checked['accepted']:
                        selected,chosen_vector = checked,vector
                        return True
            return False
        if not search(known):
            begin = max(0.,original-2.7); end = min(song['duration'],begin+5.4)
            vocal = probe_window(begin,end); new_probes = 1
            observed_vectors.append(vocal)
            search([vocal])
    if selected is None:
        result.update(entryReviewStatus='needs-review',entryReviewOnly=True,status='needs-review',
                      entryProof={'accepted':False,'method':'unresolved-entry','sourceStart':original,
                                  'sourceMD5':song['md5'],'reason':'no-observed-vocal-free-entry-breath'})
    else:
        t = selected['sourceStart']; changed = abs(t-original) > .0001
        previous_fade=row.get('fadeIn',0)
        if selected['method']==ONSET_METHOD:row['fadeIn']=selected['fadeInSeconds']
        fade_changed=abs(row.get('fadeIn',0)-previous_fade)>1e-7
        result.pop('entryReviewError',None)
        if changed or fade_changed:
            row['start'] = t
            result['entry'] = {**result.get('entry',{}),'time':t,'kind':selected['method'],
                               'originalTime':original}
            # The measured outgoing audio remains, but its row fingerprint is
            # stale. The final stereo verifier must explicitly renew it.
            result.pop('stereoEvidence',None); result.pop('stereoError',None)
            result.pop('stereoVersion',None)
            result['exit'] = {**result.get('exit',{})}
            result['exit'].pop('stereoVerified',None)
            result['entryShiftNeedsStereoRecheck'] = True
        if chosen_vector is not None:
            selected['cacheKey'] = chosen_vector.get('cacheKey')
        result.update(entryProof=selected,entryReviewStatus='ready',entryReviewOnly=False,
                      status='ready' if item.get('status')=='ready' or item.get('entryReviewOnly') else item['status'])
    result.update(entryReviewVersion=VERSION,entryReviewAttempts=attempts,
                  entryReviewBudget={'newProbes':new_probes,'maximumNewProbes':1,'maximumAudioSeconds':5.4})
    references = {}
    for vector in observed_vectors:
        key = vector.get('cacheKey')
        if key:
            references[key] = {field:vector[field] for field in
                ('cacheKey','sourceOffsetSeconds','durationSeconds','modelSha256') if field in vector}
    result['entryVocalEvidence'] = list(references.values())
    result['entryReviewInputHash'] = fingerprint(result,song,owner)
    return result


def selection_error(item, song, *, owner=(), evidence_folder):
    """Final gate: independently reload the selected raw proof, not its verdict."""
    row = item.get('row',{}); proof = item.get('entryProof',{})
    if item.get('entryReviewVersion') != VERSION or not proof.get('accepted'):
        return 'entry-not-reviewed'
    if item.get('md5') != song.get('md5') or proof.get('sourceMD5') != song.get('md5'):
        return 'entry-source-mismatch'
    if not finite(row.get('start')) or abs(proof.get('sourceStart',-999)-row['start']) > .0001:
        return 'entry-proof-stale'
    if item.get('entryReviewInputHash') != fingerprint(item,song,owner):
        return 'entry-input-changed'
    if proof.get('method') in EXEMPTIONS:
        return None if (exemption(row['start'],song,owner) == proof['method'] and
                        proof.get('acousticProofClaimed') is False) else 'entry-exemption-invalid'
    if proof.get('method') not in ('vocal-free-entry-breath',ONSET_METHOD):
        return 'entry-proof-method'
    expected_parameters=ONSET_PARAMETERS if proof.get('method')==ONSET_METHOD else PARAMETERS
    if proof.get('parameters')!=expected_parameters:return 'entry-proof-method'
    key = proof.get('cacheKey')
    if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key):return 'entry-proof-key'
    try:
        raw = read_json(Path(evidence_folder)/(key+'.json'))
    except (OSError,ValueError,RuntimeError):return 'entry-proof-unavailable'
    if raw.get('cacheKey') != key:return 'entry-proof-key'
    if proof['method']==ONSET_METHOD:
        check=check_entry_onset(raw,row['start'],item.get('observedWords',[]),gain_db(song),song['md5'],
            coverage=item.get('observedCoverage',[]),fade_in=row.get('fadeIn'))
        if not check['accepted']:return 'entry-proof-'+check['reason']
        expected={**check,'cacheKey':key}
        if proof!=expected or row.get('fadeIn')!=check['fadeInSeconds']:return 'entry-proof-mismatch'
        return None
    check=check_entry(raw,row['start'],item.get('observedWords',[]),gain_db(song),song['md5'],
        coverage=item.get('observedCoverage',[]))
    if not check['accepted']:return 'entry-proof-'+check['reason']
    for field in ('modelSha256','sourceMD5','sourceStart','proofStart','proofEnd','frameFirst','frameLastExclusive',
                  'asrCoverageStart','asrCoverageEnd','asrCoverageComplete'):
        if proof.get(field) != check.get(field):return 'entry-proof-mismatch'
    for field in ('playbackGainDb','normalizedVocalMaxDbfs','normalizedVocalP95Dbfs','vocalToMixP95Db'):
        if not finite(proof.get(field)) or abs(proof[field]-check[field]) > 1e-5:
            return 'entry-proof-mismatch'
    return None


def run(settings, work, decoder, model_path, limit=None, retry=False):
    from .disk import Disk
    if limit is not None and limit < 0:raise RuntimeError('ENTRY_REVIEW_LIMIT')
    decoder = shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file() or not os.access(decoder,os.X_OK):raise RuntimeError('ENTRY_REVIEW_DECODER')
    version = subprocess.run([decoder,'-version'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
    if version.returncode or not version.stdout.startswith(b'ffmpeg version'):raise RuntimeError('ENTRY_REVIEW_DECODER')
    verify_model(model_path)
    baseline = read_json(work/'baseline.json'); curation=settings.state/'research/dj1-curation'
    owners=read_json(curation/'owner-entries.json'); rejected=read_json(curation/'owner-rejected.json')
    cached_index=discover_cached_references(work/'vocal-evidence')
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(replace(settings,max_bytes=min(settings.max_bytes,40*1024**2)));count=0
    try:
        for old in baseline['tracks']:
            path=work/'tracks'/(old['trackId']+'.json');item=read_json(path)
            if item.get('status')!='ready' and not item.get('entryReviewOnly'):continue
            asset=db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,l.integrated_lufs AS lufs,
                l.true_peak_dbtp AS peak,l.method AS loudnessMethod,l.status AS loudnessStatus
                FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''',(old['trackId'],)).fetchone()
            if not asset or asset['md5']!=old['md5']:raise RuntimeError('ENTRY_REVIEW_SOURCE_CHANGED')
            song=dict(asset);song['manualStart']=song['lo'] is not None
            song['lo']=song['lo'] or 0.;song['hi']=song['hi'] if song['hi'] is not None else song['duration']
            owner=owners.get(old['trackId'],[])
            if not retry and item.get('entryReviewInputHash')==fingerprint(item,song,owner):continue
            if limit is not None and count>=limit:break
            if not 0<song['bytes']<=40*1024**2 or not 0<song['duration']<=1800:raise RuntimeError('ENTRY_REVIEW_SOURCE_LIMIT')
            raw=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(old['md5'],)).fetchone()
            if raw is None:raise RuntimeError('ENTRY_REVIEW_ASR_EVIDENCE')
            evidence=restore_evidence(json.loads(raw[0]),item)
            item['observedWords']=copy.deepcopy(evidence.get('words',[]))
            try:
                # Do not download originals for exemptions or sufficient raw
                # cached vectors. A miss lazily downloads just this one source.
                with tempfile.TemporaryDirectory(prefix='entry-original-',dir=work) as directory:
                    source=Path(directory)/('source'+Path(song['remote_path']).suffix)
                    probe=CachedProbe(source,song,work/'vocal-evidence',decoder,model_path)
                    keys=entry_cached_references(item,cached_index.get(song['md5'],()))
                    existing=probe.existing(keys,limit=None)
                    def observe(a,b):
                        if shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('ENTRY_REVIEW_DISK')
                        disk.download(song['remote_path'],source,expected_md5=old['md5'])
                        if source.stat().st_size!=song['bytes']:raise RuntimeError('ENTRY_REVIEW_SOURCE_SIZE')
                        return probe(a,b)
                    result=review_checkpoint(item,song,evidence.get('words',[]),observe,owner=owner,
                        coverage=evidence.get('coverage',[]),
                        rejected=rejected.get(old['trackId'],[]),existing_evidence=existing)
            except Exception as exc:
                code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith(('ENTRY_REVIEW_','VOCAL_REVIEW_')) else 'ENTRY_REVIEW_'+type(exc).__name__.upper()
                result={**item,'status':'needs-review','entryReviewOnly':True,'entryReviewStatus':'needs-review',
                        'entryReviewVersion':VERSION,'entryReviewError':code}
            atomic_json(path,result);count+=1
            print('ENTRY_REVIEWED',item.get('position'),result.get('entryReviewStatus'),old['trackId'],
                  result.get('entryProof',{}).get('method','none'),flush=True)
    finally:db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--ffmpeg',required=True);parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--limit',type=int);parser.add_argument('--retry',action='store_true');args=parser.parse_args()
    run(Settings(),args.work,args.ffmpeg,args.model,args.limit,args.retry)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith(('ENTRY_REVIEW_','VOCAL_REVIEW_')) else 'ENTRY_REVIEW_'+type(exc).__name__.upper()
        print(code,flush=True);raise SystemExit(1)
