"""Bounded incoming-ASR repair; ASR and UVR live in separate serial children.

Run only after mandatory ENTRY review. Confirmed outgoing cuts, closing/source
proofs and frozen order remain untouched. No silence or confidence relaxation.
"""
import argparse
import array
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import replace

from .config import Settings, atomic_json
from .dj_worker import replace_observed_words
from .fixed_set_entry_review import review_checkpoint, selection_error as entry_error
from .fixed_set_hard_review import all_existing, pair, references
from .fixed_set_repair import selection_error as exit_error
from .fixed_set_resolve import read_json, restore_evidence
from .fixed_set_vocal_review import CachedProbe, _vectors, finite
from .vocal_probe import MODEL_SHA256, SAMPLE_RATE

VERSION=2
ASR_CACHE_VERSION=1  # Context ranking/lineage changes do not alter recognition.
MAX_CONTEXTS=2
CONTEXT_SECONDS=20
ASR_PARAMETERS={'model':'base','computeType':'int8','beamSize':3,'temperature':0.,
                'wordTimestamps':True,'conditionOnPreviousText':False,'vadFilter':False}


class NoCachedEntry(Exception):
    pass


def model_manifest(folder):
    """Pin an already cached snapshot without fetching or loading any model."""
    root=Path(folder)/'models--Systran--faster-whisper-base'
    ref=root/'refs/main'
    if not ref.is_file() or ref.stat().st_size>128:raise RuntimeError('ENTRY_RESOLVE_LOCAL_MODEL')
    revision=ref.read_text().strip()
    if not re.fullmatch('[a-f0-9]{40}',revision):raise RuntimeError('ENTRY_RESOLVE_LOCAL_MODEL')
    directory=root/'snapshots'/revision;digests={}
    for name in ('model.bin','config.json','tokenizer.json','vocabulary.txt'):
        path=directory/name
        if not path.is_file():raise RuntimeError('ENTRY_RESOLVE_LOCAL_MODEL')
        with path.open('rb') as stream:digests[name]=hashlib.file_digest(stream,'sha256').hexdigest()
    identity=hashlib.sha256(json.dumps([revision,digests],sort_keys=True).encode()).hexdigest()
    return {'directory':str(directory),'revision':revision,'files':digests,'fingerprint':identity}


def asr_key(song,begin,end,model,asr_input=None):
    values=[ASR_CACHE_VERSION,song['md5'],begin,end,model['fingerprint'],ASR_PARAMETERS]
    if asr_input is not None:values.append(asr_input)
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate_asr(value,song,begin,end,model,asr_input=None):
    if asr_input is not None:validate_stem(asr_input,song,begin,end)
    if (not isinstance(value,dict) or value.get('version')!=ASR_CACHE_VERSION or
            value.get('sourceMD5')!=song['md5'] or value.get('start')!=begin or
            value.get('requestedEnd')!=end or value.get('modelFingerprint')!=model['fingerprint'] or
            value.get('parameters')!=ASR_PARAMETERS or value.get('asrInput')!=asr_input or
            value.get('cacheKey')!=asr_key(song,begin,end,model,asr_input)):
        raise RuntimeError('ENTRY_RESOLVE_ASR_IDENTITY')
    actual=value.get('end');words=value.get('words')
    if not finite(actual) or abs(actual-end)>.05 or actual<=begin:
        raise RuntimeError('ENTRY_RESOLVE_ASR_DURATION')
    if not isinstance(words,list) or len(words)>4000:raise RuntimeError('ENTRY_RESOLVE_ASR_WORDS')
    for word in words:
        if (not isinstance(word,list) or len(word)!=3 or not all(finite(v) for v in word[:2]) or
                type(word[2]) is not bool or not begin-.001<=word[0]<word[1]<=actual+.001):
            raise RuntimeError('ENTRY_RESOLVE_ASR_WORDS')
    if type(value.get('confident')) is not int or value['confident']!=sum(w[2] for w in words):
        raise RuntimeError('ENTRY_RESOLVE_ASR_CONFIDENCE')
    return value


def stem_index_key(song,begin,end):
    return operation_hash(['vocal-stem-v1',song['md5'],begin,end,MODEL_SHA256])


def stronger_channel(energies):
    if (not isinstance(energies,list) or len(energies)!=2 or
            any(not finite(v) or v<0 for v in energies)):
        raise RuntimeError('ENTRY_RESOLVE_STEM_ENERGY')
    return 0 if energies[0]>=energies[1] else 1


def pcm_channel_energies(path):
    """Energy per actual stereo channel, never the cancelling L+R average."""
    totals=[0.,0.];frames=0
    with Path(path).open('rb') as stream:
        while block:=stream.read(64*1024):
            if len(block)%8:raise RuntimeError('ENTRY_RESOLVE_STEM_SIZE')
            values=array.array('f');values.frombytes(block)
            if sys.byteorder!='little':values.byteswap()
            if any(not math.isfinite(v) for v in values):raise RuntimeError('ENTRY_RESOLVE_STEM_ENERGY')
            for channel in (0,1):totals[channel]+=math.fsum(v*v for v in values[channel::2])
            frames+=len(values)//2
    if not frames:raise RuntimeError('ENTRY_RESOLVE_STEM_SIZE')
    return [value/frames for value in totals]


def validate_stem(value,song,begin,end):
    if (not isinstance(value,dict) or value.get('kind')!='vocals' or value.get('version')!=1 or
            value.get('sourceMD5')!=song['md5'] or value.get('requestedStart')!=begin or
            value.get('requestedEnd')!=end or value.get('uvrModelSha256')!=MODEL_SHA256 or
            value.get('sampleRate')!=SAMPLE_RATE or value.get('channels')!=2 or
            value.get('normalizationGainDb')!=0 or value.get('channelPolicy')!='higher-stereo-energy'):
        raise RuntimeError('ENTRY_RESOLVE_STEM_IDENTITY')
    count=value.get('pcmFrames');size=value.get('pcmBytes');offset=value.get('sourceOffsetSeconds')
    if (type(count) is not int or not 0<count<=20*SAMPLE_RATE or type(size) is not int or size!=count*8 or
            not finite(offset) or abs(offset-begin)>1/SAMPLE_RATE+1e-7 or
            abs(offset+count/SAMPLE_RATE-end)>.05 or
            not isinstance(value.get('pcmSHA256'),str) or not re.fullmatch('[a-f0-9]{64}',value['pcmSHA256']) or
            type(value.get('selectedChannel')) is not int or
            value.get('selectedChannel')!=stronger_channel(value.get('channelMeanSquares'))):
        raise RuntimeError('ENTRY_RESOLVE_STEM_PCM')
    return value


def validate_stem_file(path,value,song,begin,end):
    validate_stem(value,song,begin,end);path=Path(path)
    if path.stat().st_size!=value['pcmBytes']:raise RuntimeError('ENTRY_RESOLVE_STEM_SIZE')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=value['pcmSHA256']:
            raise RuntimeError('ENTRY_RESOLVE_STEM_HASH')
    if pcm_channel_energies(path)!=value['channelMeanSquares']:raise RuntimeError('ENTRY_RESOLVE_STEM_ENERGY')
    return path


def apply_asr(evidence,observation):
    """Same >=3-confident replacement rule as LocalObserver; empty never erases."""
    result=copy.deepcopy(evidence);fresh=copy.deepcopy(observation['words'])
    begin,end=observation['start'],observation['end']
    if observation['confident']>=3:
        result['words']=replace_observed_words(result.get('words',[]),fresh,begin,end)
    else:
        result['words']=[*result.get('words',[]),*fresh]
    result.setdefault('coverage',[]).append([begin,end])
    result.setdefault('repairProbes',[]).append({'start':begin,'end':end,'words':len(fresh),
        'confident':observation['confident'],'entryResolver':VERSION,
        'asrEvidenceKey':observation['cacheKey'],'asrModelFingerprint':observation['modelFingerprint']})
    if observation.get('asrInput') is not None:
        result['repairProbes'][-1].update(asrSource='vocals',asrInputSHA256=observation['asrInput']['pcmSHA256'])
    return result


def contexts(item,song):
    """Keep the current entry first; maximize newly observed feasible entries."""
    end=item['row']['end'];lo=max(song['lo'],end-100);hi=min(song['hi'],end-45)
    if hi<lo:return []
    def window(centre):
        centre=max(lo,min(hi,centre))
        begin=round(max(song['lo'],min(song['hi']-CONTEXT_SECONDS,centre-10)),4)
        finish=round(min(song['hi'],song['duration'],begin+CONTEXT_SECONDS),4)
        return (begin,finish) if finish-begin>=8 else None
    def core(span):
        # Candidate must retain ASR[-.3,+.35] plus the UVR excerpt250ms
        # context guard. Symmetric entry checks may use slightly more audio.
        return max(lo,span[0]+.55),min(hi,span[1]-.6)
    first=window(item['row']['start'])
    if first is None:return []
    chosen=[first];a,b=core(first);preferred=max(lo,min(hi,end-62))
    candidates={window(t) for t in (preferred,(lo+hi)/2,lo+10,hi-10,lo,hi)}-{None,first}
    ranked=[]
    for candidate in candidates:
        x,y=core(candidate)
        novel=max(0,y-x)-max(0,min(y,b)-max(x,a))
        if novel<=1e-7:continue
        centre=(candidate[0]+candidate[1])/2
        ranked.append((-novel,abs(centre-preferred),candidate[0],candidate))
    if ranked and MAX_CONTEXTS>1:chosen.append(min(ranked)[-1])
    return chosen


def numeric_state(evidence,md5):
    result={'sourceMD5':md5,'words':copy.deepcopy(evidence.get('words',[])),
        'coverage':copy.deepcopy(evidence.get('coverage',[])),
        'repairProbes':copy.deepcopy(evidence.get('repairProbes',[]))}
    encoded=json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>1024**2:raise RuntimeError('ENTRY_RESOLVE_SNAPSHOT_LIMIT')
    return result


def operation_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def lineage_error(item,song,*,evidence_folder,model_fingerprint):
    try:
        return _lineage_error(item,song,evidence_folder=evidence_folder,model_fingerprint=model_fingerprint)
    except (ValueError,TypeError,KeyError,RuntimeError):
        return 'entry-resolve-invalid-lineage'


def _lineage_error(item,song,*,evidence_folder,model_fingerprint):
    """Replay every replacement from actual source/model-pinned ASR cache."""
    initial=item.get('entryResolveInput');operations=item.get('entryResolveOperations')
    if (item.get('entryResolveVersion')!=VERSION or not isinstance(initial,dict) or
            initial.get('sourceMD5')!=song['md5'] or item.get('md5')!=song['md5'] or
            not isinstance(operations,list) or len(operations)>MAX_CONTEXTS):
        return 'entry-resolve-lineage-missing'
    if operation_hash(initial)!=item.get('entryResolveInputHash'):return 'entry-resolve-input-hash'
    state=copy.deepcopy(initial)
    for operation in operations:
        if not isinstance(operation,dict):return 'entry-resolve-operation-shape'
        op={k:v for k,v in operation.items() if k!='operationHash'}
        if operation_hash(op)!=operation.get('operationHash'):return 'entry-resolve-operation-hash'
        if (op.get('version')!=VERSION or op.get('beforeHash')!=operation_hash(state) or
                op.get('modelFingerprint')!=model_fingerprint):return 'entry-resolve-operation-input'
        key=op.get('asrEvidenceKey')
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key):return 'entry-resolve-ASR-key'
        try:
            observation=validate_asr(read_json(Path(evidence_folder)/(key+'.json')),song,
                op['start'],op['requestedEnd'],{'fingerprint':model_fingerprint},op.get('asrInput'))
        except (OSError,ValueError,KeyError,RuntimeError):return 'entry-resolve-ASR-cache'
        if (observation['cacheKey']!=key or observation['end']!=op.get('actualEnd') or
                observation['confident']!=op.get('confidentWords')):return 'entry-resolve-ASR-observation'
        policy='replace-observed-interior-1s' if observation['confident']>=3 else 'append-preserve-all-prior'
        if op.get('replacementPolicy')!=policy:return 'entry-resolve-replacement-policy'
        state=numeric_state(apply_asr(state,observation),song['md5'])
        if operation_hash(state)!=op.get('afterHash'):return 'entry-resolve-operation-output'
    if operation_hash(state)!=item.get('entryResolveOutputHash'):return 'entry-resolve-output-hash'
    final=numeric_state({'words':item.get('observedWords',[]),'coverage':item.get('observedCoverage',[]),
                         'repairProbes':item.get('repairProbes',[])},song['md5'])
    return None if final==state else 'entry-resolve-final-evidence'


def _clear_stale(item):
    for key in ('entryProof','entryReviewVersion','entryReviewInputHash','entryReviewError',
                'stereoEvidence','stereoVersion','stereoError','entryResolveError'):
        item.pop(key,None)
    item['entryReviewStatus']='pending'
    item['entryShiftNeedsStereoRecheck']=True


def select_cached(item,song,evidence,values,owner,rejected):
    """Existing v3 entry checker, then wide certified search, without a native call."""
    def unavailable(*args):raise NoCachedEntry()
    candidate=copy.deepcopy(item)
    candidate['observedWords']=copy.deepcopy(evidence.get('words',[]))
    candidate['observedCoverage']=copy.deepcopy(evidence.get('coverage',[]))
    try:
        checked=review_checkpoint(candidate,song,evidence.get('words',[]),unavailable,
            coverage=evidence.get('coverage',[]),owner=owner,rejected=rejected,existing_evidence=values)
    except NoCachedEntry:
        checked=None
    if checked is not None and not checked.get('entryReviewOnly'):return checked
    endpoint={'point':item['exit'],'fixedExit':True,'score':0}
    selected=pair(item.get('old',item['row']),song,evidence,[endpoint],values,
                  owner,rejected,certified_only=True)
    if selected is None:return None
    _,entry,_=selected
    candidate['row']={**candidate['row'],'start':entry['time'],'fadeIn':entry.get('fade',.08)}
    _clear_stale(candidate)
    try:
        return review_checkpoint(candidate,song,evidence.get('words',[]),unavailable,
            coverage=evidence.get('coverage',[]),owner=owner,rejected=rejected,existing_evidence=values)
    except NoCachedEntry:
        return None


def refine(item,song,evidence,observer,*,existing=(),owner=(),rejected=(),verify_entry):
    """Pure orchestration with injected child observer, bounded to two windows."""
    if not item.get('entryReviewOnly'):raise RuntimeError('ENTRY_RESOLVE_NOT_PENDING')
    if item.get('md5')!=song['md5'] or exit_error({**item,'status':'ready'}):
        raise RuntimeError('ENTRY_RESOLVE_PREVIOUS_EXIT_INVALID')
    original=copy.deepcopy(item);working=copy.deepcopy(item);evidence=copy.deepcopy(evidence)
    initial=numeric_state(evidence,song['md5']);operations=[]
    outgoing=copy.deepcopy(item['exit']);source_ending=copy.deepcopy(item.get('sourceEnding'))
    fixed_end=item['row']['end'];fixed_fade=item['row']['fadeOut']
    values={v['cacheKey']:v for v in existing if v.get('md5')==song['md5'] and _vectors(v) is not None}
    attempts=[];selected=None;gate_errors=[]
    def choose():
        nonlocal selected
        result=select_cached(working,song,evidence,list(values.values()),owner,rejected)
        if result is None or result.get('entryReviewOnly'):return False
        # Preserve exact confirmed outgoing JSON, including natural/source EOF
        # metadata time. Incoming ASR never changes that physical event.
        result['exit']=copy.deepcopy(outgoing)
        result['row']['end']=fixed_end;result['row']['fadeOut']=fixed_fade
        if source_ending is not None:result['sourceEnding']=copy.deepcopy(source_ending)
        result['observedWords']=copy.deepcopy(evidence.get('words',[]))
        result['observedCoverage']=copy.deepcopy(evidence.get('coverage',[]))
        error=verify_entry(result,song)
        if error:
            if error not in gate_errors:gate_errors.append(error)
            return False
        selected=result;return True
    if not choose():
        for begin,end in contexts(item,song):
            observation=observer.asr(begin,end)  # This child has EXITED before UVR.
            before=operation_hash(numeric_state(evidence,song['md5']))
            evidence=apply_asr(evidence,observation)
            operation={'version':VERSION,'beforeHash':before,
                'afterHash':operation_hash(numeric_state(evidence,song['md5'])),
                'asrEvidenceKey':observation['cacheKey'],'modelFingerprint':observation['modelFingerprint'],
                'start':observation['start'],'requestedEnd':observation.get('requestedEnd',end),
                'actualEnd':observation['end'],'confidentWords':observation['confident'],
                'replacementPolicy':'replace-observed-interior-1s' if observation['confident']>=3 else 'append-preserve-all-prior'}
            if observation.get('asrInput') is not None:operation['asrInput']=copy.deepcopy(observation['asrInput'])
            operations.append({**operation,'operationHash':operation_hash(operation)})
            working.update(observedWords=copy.deepcopy(evidence['words']),
                observedCoverage=copy.deepcopy(evidence['coverage']),repairProbes=copy.deepcopy(evidence['repairProbes']))
            _clear_stale(working)
            attempt={'start':begin,'end':end,'asrEvidenceKey':observation['cacheKey'],
                     'confidentWords':observation['confident'],'vocalProbeNeeded':False}
            attempts.append(attempt)
            if choose():break
            vocal=observer.vocal(begin,end)
            if vocal.get('md5')!=song['md5'] or _vectors(vocal) is None:
                raise RuntimeError('ENTRY_RESOLVE_VOCAL_IDENTITY')
            values[vocal['cacheKey']]=vocal
            attempt.update(vocalProbeNeeded=True,vocalEvidenceKey=vocal['cacheKey'])
            if choose():break
    result=selected if selected is not None else working
    if selected is None:
        result.update(status='needs-review',entryReviewOnly=True,entryReviewStatus='needs-review',
            entryResolveReason='no-certified-entry-after-targeted-ASR')
    else:
        result.update(status='ready',entryReviewOnly=False,entryReviewStatus='ready')
        result.pop('entryResolveReason',None);result.pop('entryResolveError',None)
    result.update(observedWords=copy.deepcopy(evidence.get('words',[])),
        observedCoverage=copy.deepcopy(evidence.get('coverage',[])),repairProbes=copy.deepcopy(evidence.get('repairProbes',[])),
        entryVocalEvidence=references(original.get('entryVocalEvidence',[]),[_ref(v) for v in values.values()]),
        entryResolveVersion=VERSION,entryResolveAttempts=attempts,
        entryResolveInput=initial,entryResolveInputHash=operation_hash(initial),
        entryResolveOperations=operations,
        entryResolveOutputHash=operation_hash(numeric_state(evidence,song['md5'])),
        entryResolveGateFailures=gate_errors,
        entryResolveBudget={'contexts':len(attempts),'maximumContexts':MAX_CONTEXTS,
                            'maximumASRAudioSeconds':40,'maximumUVRAudioSeconds':40,
                            'modelProcessesOverlap':False})
    if attempts:
        # Any ASR replacement changes lineage, even if the numeric point did
        # not move. Final stereo replay must renew the row/input fingerprint.
        for key in ('stereoEvidence','stereoVersion','stereoError'):result.pop(key,None)
        result['entryShiftNeedsStereoRecheck']=True
    assert result['exit']==outgoing and result['row']['end']==fixed_end and result['row']['fadeOut']==fixed_fade
    assert result.get('sourceEnding')==source_ending
    return result


def _ref(value):
    return {k:value[k] for k in ('cacheKey','sourceOffsetSeconds','durationSeconds','modelSha256') if k in value}


def child_process(command,*,timeout=180):
    """Return only after the native child exits; kill its decoder too on stop."""
    child=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        code=child.wait(timeout=timeout)
    except BaseException as exc:
        try:os.killpg(child.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            child.wait()
        if isinstance(exc,subprocess.TimeoutExpired):raise RuntimeError('ENTRY_RESOLVE_CHILD_TIMEOUT') from None
        raise
    if code<0:raise RuntimeError('ENTRY_RESOLVE_CHILD_RESOURCE')
    if code:raise RuntimeError('ENTRY_RESOLVE_CHILD_FAILED')


class ChildObserver:
    def __init__(self,source,song,work,decoder,uvr_model,asr_model,ensure_source,temporary,asr_source='mix'):
        self.source=source;self.song=song;self.work=work;self.decoder=decoder
        self.uvr_model=uvr_model;self.asr_model=asr_model;self.ensure_source=ensure_source
        self.temporary=Path(temporary)
        if asr_source not in ('mix','vocals'):raise RuntimeError('ENTRY_RESOLVE_ASR_SOURCE')
        self.asr_source=asr_source

    def invoke(self,kind,begin,end,**extra):
        self.ensure_source()
        request={'kind':kind,'source':str(self.source),'song':self.song,'work':str(self.work),
            'decoder':str(self.decoder),'uvrModel':str(self.uvr_model),'asrModel':self.asr_model,
            'start':begin,'end':end,'response':str(self.temporary/'response.json'),**extra}
        target=self.temporary/'request.json';atomic_json(target,request)
        Path(request['response']).unlink(missing_ok=True)
        child_process([sys.executable,'-m','aidj.fixed_set_entry_resolve','--child',str(target)])
        response=read_json(self.temporary/'response.json')
        if response.get('kind')!=kind:raise RuntimeError('ENTRY_RESOLVE_CHILD_RESPONSE')
        return response

    def asr(self,begin,end):
        folder=self.work/'entry-asr-evidence';folder.mkdir(exist_ok=True)
        if self.asr_source=='vocals':return self.vocal_asr(begin,end,folder)
        target=folder/(asr_key(self.song,begin,end,self.asr_model)+'.json')
        if not target.exists():self.invoke('asr',begin,end)
        return validate_asr(read_json(target),self.song,begin,end,self.asr_model)

    def vocal_asr(self,begin,end,folder):
        inputs=folder/'stem-inputs';inputs.mkdir(exist_ok=True)
        index=inputs/(stem_index_key(self.song,begin,end)+'.json')
        if index.exists():
            metadata=validate_stem(read_json(index),self.song,begin,end)
            target=folder/(asr_key(self.song,begin,end,self.asr_model,metadata)+'.json')
            if target.exists():return validate_asr(read_json(target),self.song,begin,end,self.asr_model,metadata)
        pcm=self.temporary/'entry-vocal-stem.f32'
        try:
            # This UVR process has completely exited before ASR is spawned.
            response=self.invoke('stem',begin,end,stemPath=str(pcm))
            metadata=validate_stem(response.get('asrInput'),self.song,begin,end)
            self.invoke('asr',begin,end,asrInput=metadata,stemPath=str(pcm))
            target=folder/(asr_key(self.song,begin,end,self.asr_model,metadata)+'.json')
            result=validate_asr(read_json(target),self.song,begin,end,self.asr_model,metadata)
            atomic_json(index,metadata)
            return result
        finally:pcm.unlink(missing_ok=True)

    def vocal(self,begin,end):
        adapter=CachedProbe(self.source,self.song,self.work/'vocal-evidence',self.decoder,self.uvr_model)
        from .fixed_set_vocal_review import VERSION as CV,SAMPLE_RATE,MODEL_SHA256
        a=round(begin*SAMPLE_RATE)/SAMPLE_RATE;b=min(self.song['duration'],round(end*SAMPLE_RATE)/SAMPLE_RATE)
        key=hashlib.sha256(json.dumps([CV,self.song['md5'],a,b,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
        cached=all_existing(adapter,[{'cacheKey':key}])
        if cached:return cached[0]
        response=self.invoke('vocal',begin,end)
        if response.get('cacheKey')!=key:raise RuntimeError('ENTRY_RESOLVE_VOCAL_RESPONSE')
        cached=all_existing(adapter,[{'cacheKey':response.get('cacheKey')}])
        if not cached:raise RuntimeError('ENTRY_RESOLVE_VOCAL_RESPONSE')
        return cached[0]


def asr_decode_command(decoder,source,begin,end,pcm):
    return [str(decoder),'-nostdin','-v','error','-xerror','-threads','1','-filter_threads','1',
        '-ss',str(begin),'-i',str(source),'-t',str(end-begin),'-vn','-sn','-dn','-ac','1',
        '-ar','16000','-f','f32le','-fs',str(16000*20*4+4),'-y',str(pcm)]


def stem_asr_decode_command(decoder,source,metadata,pcm):
    return [str(decoder),'-nostdin','-v','error','-xerror','-threads','1','-filter_threads','1',
        '-f','f32le','-ar',str(SAMPLE_RATE),'-ac','2','-i',str(source),
        '-af','pan=mono|c0=c'+str(metadata['selectedChannel']),'-ar','16000',
        '-t',str(metadata['pcmFrames']/SAMPLE_RATE),'-f','f32le',
        '-fs',str(16000*20*4+4),'-y',str(pcm)]


def extract_stem(req):
    """One pinned UVR inference, one temporary PCM and reusable numeric cache."""
    import numpy as np
    from .vocal_probe import _load_model, _separate, _evidence
    from .fixed_set_vocal_review import VERSION as CV
    song=req['song'];begin=req['start'];end=req['end'];folder=Path(req['work'])/'vocal-evidence'
    a=round(begin*SAMPLE_RATE)/SAMPLE_RATE;b=min(song['duration'],round(end*SAMPLE_RATE)/SAMPLE_RATE)
    with tempfile.TemporaryDirectory(prefix='stem-excerpt-',dir=Path(req['response']).parent) as directory:
        pcm=Path(directory)/'mixture.f32'
        command=[req['decoder'],'-nostdin','-v','error','-xerror','-threads','1','-filter_threads','1',
            '-ss',str(a),'-i',req['source'],'-t',str(b-a),'-vn','-sn','-dn','-ac','2',
            '-ar',str(SAMPLE_RATE),'-f','f32le','-fs',str(SAMPLE_RATE*20*8+8),'-y',str(pcm)]
        process=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
        if process.returncode or not pcm.exists() or not 0<pcm.stat().st_size<=SAMPLE_RATE*20*8 or pcm.stat().st_size%8:
            raise RuntimeError('ENTRY_RESOLVE_STEM_DECODE')
        wave=np.fromfile(pcm,dtype='<f4').reshape(-1,2).T
        if not np.isfinite(wave).all() or abs(a+wave.shape[1]/SAMPLE_RATE-b)>.05:
            raise RuntimeError('ENTRY_RESOLVE_STEM_DURATION')
        vocals=_separate(wave,_load_model(Path(req['uvrModel'])))
        if vocals.shape!=wave.shape or vocals.dtype!=np.float32 or not np.isfinite(vocals).all():
            raise RuntimeError('ENTRY_RESOLVE_STEM_OUTPUT')
        target=Path(req['stemPath']);vocals.T.astype('<f4',copy=False).tofile(target)
        with target.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        energies=pcm_channel_energies(target)
        metadata={'kind':'vocals','version':1,'sourceMD5':song['md5'],'requestedStart':begin,
            'requestedEnd':end,'uvrModelSha256':MODEL_SHA256,'sourceOffsetSeconds':a,
            'sampleRate':SAMPLE_RATE,'channels':2,'pcmBytes':target.stat().st_size,
            'pcmFrames':vocals.shape[1],'pcmSHA256':digest,'channelPolicy':'higher-stereo-energy',
            'channelMeanSquares':energies,'selectedChannel':stronger_channel(energies),'normalizationGainDb':0}
        validate_stem(metadata,song,begin,end)
        key=hashlib.sha256(json.dumps([CV,song['md5'],a,b,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
        adapter=CachedProbe(Path(req['source']),song,folder,req['decoder'],Path(req['uvrModel']))
        if not all_existing(adapter,[{'cacheKey':key}]):
            numeric=_evidence(wave,vocals,SAMPLE_RATE,wave.shape[1]/SAMPLE_RATE/2)
            numeric.update(sourceOffsetSeconds=a,md5=song['md5'],cacheKey=key,requestedEndSeconds=b,vocalReviewVersion=CV)
            if _vectors(numeric,allow_warnings=True) is None:raise RuntimeError('ENTRY_RESOLVE_STEM_EVIDENCE')
            atomic_json(folder/(key+'.json'),numeric)
    atomic_json(Path(req['response']),{'kind':'stem','asrInput':metadata,'vocalEvidenceKey':key})


def child(request):
    req=read_json(Path(request));song=req['song'];begin=req['start'];end=req['end']
    if not finite(begin) or not finite(end) or not 0<=begin<end<=song['duration']+.0001 or end-begin>20.0001:
        raise RuntimeError('ENTRY_RESOLVE_CHILD_BOUNDS')
    source=Path(req['source'])
    if not 0<source.stat().st_size<=40*1024**2:raise RuntimeError('ENTRY_RESOLVE_SOURCE_SIZE')
    with source.open('rb') as stream:
        if hashlib.file_digest(stream,'md5').hexdigest()!=song['md5']:raise RuntimeError('ENTRY_RESOLVE_SOURCE_CHANGED')
    if req['kind']=='stem':return extract_stem(req)
    if req['kind']=='vocal':
        value=CachedProbe(source,song,Path(req['work'])/'vocal-evidence',req['decoder'],Path(req['uvrModel']))(begin,end)
        atomic_json(Path(req['response']),{'kind':'vocal','cacheKey':value['cacheKey']});return
    if req['kind']!='asr':raise RuntimeError('ENTRY_RESOLVE_CHILD_KIND')
    model=req['asrModel']
    actual=model_manifest(Path(model['directory']).parents[2])
    if actual!=model:raise RuntimeError('ENTRY_RESOLVE_LOCAL_MODEL_CHANGED')
    import numpy as np
    from faster_whisper import WhisperModel
    from .dj_worker import numeric_words
    with tempfile.TemporaryDirectory(prefix='asr-excerpt-',dir=Path(req['response']).parent) as directory:
        pcm=Path(directory)/'excerpt.f32'
        asr_input=req.get('asrInput')
        if asr_input is not None:
            stem=validate_stem_file(req['stemPath'],asr_input,song,begin,end)
            command=stem_asr_decode_command(req['decoder'],stem,asr_input,pcm)
        else:command=asr_decode_command(req['decoder'],source,begin,end,pcm)
        process=subprocess.run(command,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
        if process.returncode or not pcm.exists() or not 0<pcm.stat().st_size<=16000*20*4 or pcm.stat().st_size%4:
            raise RuntimeError('ENTRY_RESOLVE_ASR_DECODE')
        offset=asr_input['sourceOffsetSeconds'] if asr_input else begin
        actual_end=offset+pcm.stat().st_size/64000
        if abs(actual_end-end)>.05:raise RuntimeError('ENTRY_RESOLVE_ASR_DURATION')
        wave=np.fromfile(pcm,dtype='<f4')
        asr=WhisperModel(model['directory'],device='cpu',compute_type='int8',
            cpu_threads=2,num_workers=1,local_files_only=True)
        segments,_=asr.transcribe(wave,beam_size=3,temperature=0.,word_timestamps=True,
                                 condition_on_previous_text=False,vad_filter=False)
        words=[]
        for part in segments:
            words.extend(list(word) for word in numeric_words(part,offset))
            if len(words)>4000:raise RuntimeError('ENTRY_RESOLVE_ASR_WORDS')
    value={'version':ASR_CACHE_VERSION,'sourceMD5':song['md5'],'start':begin,'end':actual_end,
        'requestedEnd':end,'modelFingerprint':model['fingerprint'],'parameters':ASR_PARAMETERS,
        'cacheKey':asr_key(song,begin,end,model,asr_input),'words':words,'confident':sum(w[2] for w in words)}
    if asr_input is not None:value['asrInput']=asr_input
    validate_asr(value,song,begin,end,model,asr_input)
    atomic_json(Path(req['work'])/'entry-asr-evidence'/(value['cacheKey']+'.json'),value)
    atomic_json(Path(req['response']),{'kind':'asr','cacheKey':value['cacheKey']})


def run(settings,work,decoder,model,limit=None,retry=False,asr_source='mix'):
    from .disk import Disk
    if limit is not None and limit<0:raise RuntimeError('ENTRY_RESOLVE_LIMIT')
    decoder=shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file():raise RuntimeError('ENTRY_RESOLVE_DECODER')
    asr_model=model_manifest(settings.state/'dj-models')
    plan=read_json(work/'baseline.json');curation=settings.state/'research/dj1-curation'
    owner=read_json(curation/'owner-entries.json');bad=read_json(curation/'owner-rejected.json')
    db=sqlite3.connect(f'file:{settings.state/"bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(replace(settings,max_bytes=min(settings.max_bytes,40*1024**2)));count=0
    try:
        for old in plan['tracks']:
            path=work/'tracks'/(old['trackId']+'.json');item=read_json(path)
            if not item.get('entryReviewOnly'):continue
            if (item.get('entryResolveVersion')==VERSION and item.get('entryResolveAsrSource','mix')==asr_source and not retry):continue
            if limit is not None and count>=limit:break
            asset=db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,l.integrated_lufs AS lufs,
                l.true_peak_dbtp AS peak,l.method AS loudnessMethod,l.status AS loudnessStatus
                FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''',(old['trackId'],)).fetchone()
            if not asset or asset['md5']!=old['md5']:raise RuntimeError('ENTRY_RESOLVE_SOURCE_CHANGED')
            song=dict(asset);song['manualStart']=song['lo'] is not None
            song['lo']=song['lo'] or 0.;song['hi']=song['duration'] if song['hi'] is None else song['hi']
            if not 0<song['bytes']<=40*1024**2 or not 0<song['duration']<=1800:raise RuntimeError('ENTRY_RESOLVE_SOURCE_LIMIT')
            raw=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(song['md5'],)).fetchone()
            if not raw:raise RuntimeError('ENTRY_RESOLVE_ASR_MISSING')
            evidence=restore_evidence(json.loads(raw[0]),item)
            try:
                with tempfile.TemporaryDirectory(prefix='entry-resolve-original-',dir=work) as directory:
                    source=Path(directory)/('source'+Path(song['remote_path']).suffix)
                    adapter=CachedProbe(source,song,work/'vocal-evidence',decoder,model)
                    values=all_existing(adapter,references(item.get('entryVocalEvidence',[]),item.get('vocalEvidence',[])))
                    downloaded=False
                    def ensure():
                        nonlocal downloaded
                        if downloaded:return
                        if shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('ENTRY_RESOLVE_DISK')
                        disk.download(song['remote_path'],source,expected_md5=song['md5'])
                        if source.stat().st_size!=song['bytes']:raise RuntimeError('ENTRY_RESOLVE_SOURCE_SIZE')
                        downloaded=True
                    safe_song={k:v for k,v in song.items() if k!='remote_path'}
                    observer=ChildObserver(source,safe_song,work,decoder,model,asr_model,ensure,directory,asr_source)
                    result=refine(item,safe_song,evidence,observer,existing=values,
                        owner=owner.get(old['trackId'],[]),rejected=bad.get(old['trackId'],[]),
                        verify_entry=lambda row,current:entry_error(row,current,owner=owner.get(old['trackId'],[]),
                                                                   evidence_folder=work/'vocal-evidence'))
                    if result.get('status')=='ready':
                        failure=lineage_error(result,safe_song,evidence_folder=work/'entry-asr-evidence',
                                              model_fingerprint=asr_model['fingerprint'])
                        if failure:
                            result.update(status='needs-review',entryReviewOnly=True,
                                entryReviewStatus='needs-review',entryResolveError=failure)
            except RuntimeError as exc:
                code=str(exc) if str(exc).startswith(('ENTRY_RESOLVE_','VOCAL_REVIEW_')) else 'ENTRY_RESOLVE_RUNTIME'
                if code in ('ENTRY_RESOLVE_CHILD_RESOURCE','ENTRY_RESOLVE_DISK'):raise
                result={**item,'status':'needs-review','entryReviewOnly':True,'entryResolveVersion':VERSION,'entryResolveError':code}
            result['entryResolveAsrSource']=asr_source
            atomic_json(path,result);count+=1
            print('ENTRY_RESOLVED',item.get('position'),result.get('entryReviewStatus'),old['trackId'],
                  result.get('entryProof',{}).get('method','none'),flush=True)
    finally:db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--child',type=Path)
    parser.add_argument('--work',type=Path);parser.add_argument('--ffmpeg');parser.add_argument('--model',type=Path)
    parser.add_argument('--limit',type=int);parser.add_argument('--retry',action='store_true')
    parser.add_argument('--asr-source',choices=('mix','vocals'),default='mix');args=parser.parse_args()
    if args.child:return child(args.child)
    if args.work is None or args.ffmpeg is None or args.model is None:parser.error('--work --ffmpeg --model required')
    run(Settings(),args.work,args.ffmpeg,args.model,args.limit,args.retry,args.asr_source)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith(('ENTRY_RESOLVE_','VOCAL_REVIEW_')) else 'ENTRY_RESOLVE_'+type(exc).__name__.upper()
        print(code,flush=True);raise SystemExit(1)
