"""A factual stored-file ending is not a quiet or artist-intended ending claim.

One bounded, complete, error-fatal decode proves the stored asset's PCM extent.
Keep its final recorded word by using native completion, with no imposed fade
for original files. Entry validation remains a separate mandatory final gate.
"""
import argparse
from array import array
import copy
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from .config import Settings, atomic_json
from .fixed_set_hard_review import measure_source_timing
from .fixed_set_repair import covered, entries_for, selection_error as exit_error
from .fixed_set_resolve import read_json, restore_evidence
from .fixed_set_vocal_review import finite, gain_db, quantile, strict_decode_valid

VERSION = 1
METHODS = {'stored-file-ending', 'stored-owner-clip-ending'}
SNAPSHOT_FIELDS = ('id','md5','bytes','duration','lo','hi','lufs','peak','loudnessMethod','loudnessStatus')


def snapshot(source):
    return {key:source.get(key) for key in SNAPSHOT_FIELDS}


def signature(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def clip_lineage(db, aid):
    """Current app-created clip lineage; no URL, filename or title inference."""
    asset=db.execute('SELECT track_key FROM web_assets WHERE id=?',(aid,)).fetchone()
    if asset is None:raise RuntimeError('CLOSING_SOURCE_MISSING')
    key=asset[0]
    if not key.startswith('clip:'):return None
    result=db.execute('SELECT result FROM audio_clips WHERE track_key=?',(key,)).fetchone()
    if result is None:raise RuntimeError('CLOSING_CLIP_LINEAGE_MISSING')
    raw=json.loads(result[0]);part=raw.get('clip',{})
    original=db.execute('SELECT id,track_key,duration FROM web_assets WHERE md5=?',
                        (part.get('original_md5'),)).fetchone()
    return {'method':'app-created-aac-clip-v1','trackKey':key,'start':part.get('start'),
            'end':part.get('end'),'originalMD5':part.get('original_md5'),
            'originalAssetId':original[0] if original else None,
            'originalTrackKey':original[1] if original else None,
            'originalDuration':original[2] if original else None}


def _clip_valid(lineage, source):
    if not isinstance(lineage,dict) or lineage.get('method')!='app-created-aac-clip-v1':return False
    digest=lineage.get('originalMD5')
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{32}',digest):return False
    if any(not finite(lineage.get(k)) for k in ('start','end','originalDuration')):return False
    a,b,total=lineage['start'],lineage['end'],lineage['originalDuration']
    if not 0<=a<b<=total+.05 or abs((b-a)-source['duration'])>.25:return False
    identity=hashlib.sha256(f'{digest}:{a:.3f}:{b:.3f}:aac-v1'.encode()).hexdigest()[:24]
    return lineage.get('trackKey')=='clip:'+identity


def observe_tail(pcm, timing):
    """Read only the final5s of a complete stereo f32le decode; never all PCM."""
    frames=timing['pcmFrames'];first=max(0,frames-5*16000);size=(frames-first)*8
    with Path(pcm).open('rb') as stream:
        stream.seek(first*8);payload=stream.read(size)
    if len(payload)!=size or not size:raise RuntimeError('CLOSING_TAIL_READ')
    samples=array('f');samples.frombytes(payload)
    if sys.byteorder!='little':samples.byteswap()
    if any(not math.isfinite(v) for v in samples):raise RuntimeError('CLOSING_TAIL_NONFINITE')
    left=[];right=[];peak_left=[];peak_right=[];counts=[]
    for start in range(0,frames-first,160):
        stop=min(frames-first,start+160);n=stop-start
        l=samples[start*2:stop*2:2];r=samples[start*2+1:stop*2:2]
        left.append(math.sqrt(sum(x*x for x in l)/n));right.append(math.sqrt(sum(x*x for x in r)/n))
        peak_left.append(max(abs(x) for x in l));peak_right.append(max(abs(x) for x in r));counts.append(n)
    return {'method':'strict-complete-stereo-pcm-tail-v1','sourceMD5':timing['sourceMD5'],
            'sampleRate':16000,'channels':2,'sourceFirstFrame':first,'sourceLastFrameExclusive':frames,
            'sourceStartSeconds':first/16000,'sourceEndSeconds':frames/16000,
            'frameHopSeconds':.01,'frameSamples':counts,'rmsLeft':left,'rmsRight':right,
            'peakLeft':peak_left,'peakRight':peak_right,
            'maxChannelRms':[max(l,r) for l,r in zip(left,right)],
            'lastStereoSamples':list(samples[-min(32,frames-first)*2:]),
            'silenceClaimed':False,'wordAlignmentClaimed':False}


def tail_error(tail, timing, digest):
    if not isinstance(tail,dict) or tail.get('method')!='strict-complete-stereo-pcm-tail-v1':return 'tail-method'
    if tail.get('sourceMD5')!=digest or tail.get('sampleRate')!=16000 or tail.get('channels')!=2:return 'tail-source'
    total=timing['pcmFrames'];first=max(0,total-80000)
    if tail.get('sourceFirstFrame')!=first or tail.get('sourceLastFrameExclusive')!=total:return 'tail-frame-range'
    if tail.get('sourceStartSeconds')!=first/16000 or tail.get('sourceEndSeconds')!=total/16000:return 'tail-time-range'
    counts=tail.get('frameSamples');expected=[min(160,total-first-i) for i in range(0,total-first,160)]
    if counts!=expected or any(type(n) is not int for n in counts) or tail.get('frameHopSeconds')!=.01:return 'tail-frame-count'
    fields=('rmsLeft','rmsRight','peakLeft','peakRight','maxChannelRms')
    arrays=[tail.get(k) for k in fields]
    if any(not isinstance(v,list) or len(v)!=len(counts) or any(not finite(x) or x<0 for x in v) for v in arrays):return 'tail-values'
    for left,right,peak_left,peak_right,maximum in zip(*arrays):
        if maximum!=max(left,right) or left>peak_left+1e-9 or right>peak_right+1e-9:return 'tail-inconsistent-values'
    last=tail.get('lastStereoSamples')
    if not isinstance(last,list) or len(last)!=min(32,total-first)*2 or any(not finite(x) for x in last):return 'tail-last-samples'
    for i,value in enumerate(last):
        frame=total-len(last)//2+i//2
        bin_index=(frame-first)//160
        if abs(value)>arrays[2+(i%2)][bin_index]+1e-9:return 'tail-last-sample-peak'
    if tail.get('silenceClaimed') is not False or tail.get('wordAlignmentClaimed') is not False:return 'tail-false-claim'
    return None


def tail_diagnostics(tail, level):
    recent=tail['maxChannelRms'][-min(50,len(tail['maxChannelRms'])):]
    db=lambda value:20*math.log10(max(1e-10,value))+level
    return {'lastHalfSecondMaxRmsDbfs':db(max(recent)),
            'lastSampleAbsMaxDbfs':db(max(abs(x) for x in tail['lastStereoSamples'][-2:])),
            'lastFiveSecondsPeakDbfs':db(max(*tail['peakLeft'],*tail['peakRight'])),
            'lastFiveSecondsP95RmsDbfs':db(quantile(tail['maxChannelRms'],.95))}


def ending_flags(key, duration, diagnostics, is_clip, fade):
    flags=[]
    if key and key.startswith('youtube:'):flags.append('video-upload-ending-not-musically-verified')
    if is_clip:flags.append('owner-created-clip-not-full-original-recording')
    if duration<100:flags.append('short-stored-recording-or-edit')
    if diagnostics['lastHalfSecondMaxRmsDbfs']>-35:flags.append('measured-tail-energy-above-minus35dBFS')
    if diagnostics['lastSampleAbsMaxDbfs']>-55:flags.append('nonzero-final-sample-inspect-edge')
    if fade:flags.append('owner-clip-generated-softout')
    return flags


def choose_entry(old, source, evidence, owner=(), rejected=()):
    end=source['duration'];lo=source['lo'];minimum=min(45.,end-lo)
    if minimum<8:raise RuntimeError('CLOSING_RANGE_TOO_SHORT')
    choices=entries_for(old,lo,end,evidence.get('words',[]),evidence.get('coverage',[]),[],owner)
    for start,stop,*confidence in evidence.get('words',[]):
        if not confidence or confidence[0] is not True:continue
        point=round(start-.12,4)
        if point<lo or not covered(evidence.get('coverage',[]),point-.3,point+.35):continue
        words=evidence.get('words',[])
        if any(a<point+.02-1e-7 and b>point-.02+1e-7 for a,b,*_ in words):continue
        if any(b<=point-.02+1e-7 and b>point-.08+1e-7 for a,b,*_ in words):continue
        choices.append({'time':point,'kind':'closing-onset-candidate','penalty':1.1,'fade':.08})
    valid=[]
    for entry in choices:
        span=end-entry['time']
        if not minimum-1e-7<=span<=100 or any(abs(entry['time']-t)<.35 for t in rejected):continue
        tier=0 if entry['kind']=='owner' else .2 if entry['time']==lo else 1
        valid.append((tier+abs(span-62)/100,entry))
    if valid:return min(valid,key=lambda pair:pair[0])[1]
    start=round(max(lo,end-min(62.,end-lo)),4)
    if any(abs(start-t)<.35 for t in rejected):
        options=[round(start+offset,4) for offset in (-1.,1.,-2.,2.)]
        start=next((t for t in options if lo<=t<end and minimum<=end-t<=100 and not any(abs(t-r)<.35 for r in rejected)),None)
        if start is None:raise RuntimeError('CLOSING_NO_ENTRY_CANDIDATE')
    return {'time':start,'kind':'closing-estimated-entry','fade':.08,
            'requiresFinalEntryProof':True,'wordBoundaryClaimed':False}


def create_checkpoint(old, source, previous, evidence, timing, tail, *, owner=(), rejected=(), lineage=None):
    if source.get('md5')!=old['md5'] or source.get('id')!=old['trackId']:raise RuntimeError('CLOSING_SOURCE_CHANGED')
    if source['hi']<source['duration']-.001:raise RuntimeError('CLOSING_MANUAL_END_HOLD')
    if not strict_decode_valid(timing,source['md5']):raise RuntimeError('CLOSING_STRICT_DECODE_REQUIRED')
    if not finite(timing.get('metadataDuration')) or abs(timing['metadataDuration']-source['duration'])>.001 or abs(timing['decodedDuration']-source['duration'])>.25:raise RuntimeError('CLOSING_DURATION_MISMATCH')
    error=tail_error(tail,timing,source['md5'])
    if error:raise RuntimeError('CLOSING_'+error.upper().replace('-','_'))
    is_clip=source.get('track_key','').startswith('clip:')
    if is_clip and not _clip_valid(lineage,source):raise RuntimeError('CLOSING_CLIP_LINEAGE_INVALID')
    if not is_clip and lineage is not None:raise RuntimeError('CLOSING_CLIP_SOURCE_MISMATCH')
    method='stored-owner-clip-ending' if is_clip else 'stored-file-ending'
    entry=choose_entry(old,source,evidence,owner,rejected);end=source['duration'];start=entry['time']
    level=gain_db(source);diagnostics=tail_diagnostics(tail,level)
    active_db=diagnostics['lastHalfSecondMaxRmsDbfs']
    fade=4. if is_clip and active_db>-35 else 0.
    proof={'version':VERSION,'method':method,'sourceSnapshot':snapshot(source),
           'sourceSignature':signature(snapshot(source)),'storedSourceKey':source.get('track_key'),
           'sourceMD5':source['md5'],'sourceBytes':source['bytes'],
           'downloadVerifiedMD5':source['md5'],'downloadVerifiedBytes':source['bytes'],
           'metadataDuration':end,'decodedDuration':timing['decodedDuration'],
           'metadataMinusDecodedSeconds':end-timing['decodedDuration'],
           'timing':copy.deepcopy(timing),'tailEvidence':tail,'tailEvidenceHash':signature(tail),
           'clipLineage':copy.deepcopy(lineage),'playbackGainDb':level,
           'tailDiagnostics':diagnostics,
           'riskFlags':ending_flags(source.get('track_key'),end,diagnostics,is_clip,fade),
           'tailLastHalfSecondMaxDbfs':active_db,'fadeOutPolicy':'owner-clip-generated-softout' if fade else 'no-imposed-exit-fade',
           'nativeCompletionRequired':True,'endingAtStoredFileBoundary':True,
           'quietClaimed':False,'wordAlignmentClaimed':False,'artistIntendedEndingClaimed':False,
           'entryVerifiedByEndingProof':False}
    row={**old,'start':start,'end':end,'fadeIn':entry.get('fade',.08),'fadeOut':fade,
         'transitionKind':'clean-cut','transitionSeconds':0,'rate':1}
    result=copy.deepcopy(previous)
    for field in ('stereoEvidence','stereoVersion','entryProof','entryReviewVersion','entryReviewInputHash',
                  'hardEntryCandidate','entryReviewError','stereoError','vocalReviewError',
                  'closingError','entryShiftNeedsStereoRecheck'):
        result.pop(field,None)
    result.update(status='ready',trackId=old['trackId'],md5=old['md5'],artist=source.get('artist'),
        title=source.get('title'),old=copy.deepcopy(old),row=row,entry=entry,
        exit={'method':method,'time':end,'fade':fade,'fadeStart':end-fade,
              'validated':True,'proof':'strict-stored-file-completion','quietClaimed':False,
              'wordAlignmentClaimed':False},sourceEnding=proof,closingVersion=VERSION,
        entryReviewOnly=False,entryReviewStatus='pending-final-entry-proof',
        observedWords=copy.deepcopy(evidence.get('words',[])),
        observedCoverage=copy.deepcopy(evidence.get('coverage',[])),
        repairProbes=copy.deepcopy(evidence.get('repairProbes',[])),eofEvidence=copy.deepcopy(timing))
    return result


def selection_error(item, current_source, *, clip_lineage=None):
    """Pure ending gate; assembler must ALSO require the independent entry gate."""
    if item.get('status')!='ready':return 'closing-unresolved'
    proof=item.get('sourceEnding',{});row=item.get('row',{});point=item.get('exit',{})
    if proof.get('version')!=VERSION or point.get('method') not in METHODS or proof.get('method')!=point.get('method'):return 'closing-method'
    if any(current_source.get(k) is None for k in ('id','md5','bytes','duration','lo','hi')):return 'closing-current-source-fields'
    try:expected=snapshot(current_source);current_hash=signature(expected)
    except (TypeError,ValueError):return 'closing-current-source-fields'
    if proof.get('sourceSnapshot')!=expected or proof.get('sourceSignature')!=current_hash:return 'closing-source-snapshot-changed'
    digest=current_source['md5'];size=current_source['bytes'];end=current_source['duration'];lo=current_source['lo'];hi=current_source['hi']
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{32}',digest) or type(size) is not int or not 0<size<=40*1024**2:return 'closing-source-fields'
    if not all(finite(v) for v in (end,lo,hi)) or not 0<=lo<hi<=end+.001 or end>1800:return 'closing-source-range'
    if hi<end-.001:return 'closing-manual-end-hold'
    if item.get('trackId')!=current_source['id'] or item.get('md5')!=digest or row.get('md5')!=digest:return 'closing-source-mismatch'
    if proof.get('sourceMD5')!=digest or proof.get('sourceBytes')!=size or proof.get('downloadVerifiedMD5')!=digest or proof.get('downloadVerifiedBytes')!=size:return 'closing-download-identity'
    timing=proof.get('timing',{})
    if not strict_decode_valid(timing,digest):return 'closing-strict-decode-required'
    if not finite(timing.get('metadataDuration')) or abs(timing['metadataDuration']-end)>.001 or abs(timing['decodedDuration']-end)>.25:return 'closing-duration-mismatch'
    if proof.get('metadataDuration')!=end or proof.get('decodedDuration')!=timing['decodedDuration'] or proof.get('metadataMinusDecodedSeconds')!=end-timing['decodedDuration']:return 'closing-duration-provenance'
    if any(not finite(row.get(k)) for k in ('start','end','fadeIn','fadeOut','rate','transitionSeconds')):return 'closing-row-fields'
    span=end-row['start'];minimum=min(45.,end-lo)
    from .fixed_set_duration_exception import maximum_span
    try:maximum=maximum_span(item,current_source)
    except (ValueError,TypeError):return 'closing-duration-exception-invalid'
    if row['end']!=end or point.get('time')!=end or row['end']!=proof['metadataDuration']:return 'closing-not-exact-stored-end'
    if row['trackId']!=item['trackId'] or not lo<=row['start']<end or not minimum-1e-7<=span<=maximum or span<8:return 'closing-row-bounds'
    if row['transitionKind']!='clean-cut' or row['transitionSeconds']!=0 or row['rate']!=1:return 'closing-overlap-or-rate'
    if row['fadeIn']<0 or row['fadeOut']<0 or row['fadeIn']+row['fadeOut']>=span:return 'closing-fade-budget'
    if point.get('fade')!=row['fadeOut'] or point.get('fadeStart')!=end-row['fadeOut']:return 'closing-fade-mismatch'
    error=tail_error(proof.get('tailEvidence'),timing,digest)
    if error:return 'closing-'+error
    if signature(proof['tailEvidence'])!=proof.get('tailEvidenceHash'):return 'closing-tail-hash'
    level=gain_db(current_source)
    diagnostics=tail_diagnostics(proof['tailEvidence'],level)
    active_db=diagnostics['lastHalfSecondMaxRmsDbfs']
    if proof.get('playbackGainDb')!=level or proof.get('tailLastHalfSecondMaxDbfs')!=active_db:return 'closing-current-gain-mismatch'
    if proof.get('tailDiagnostics')!=diagnostics:return 'closing-tail-diagnostics'
    is_clip=point['method']=='stored-owner-clip-ending'
    if is_clip:
        if not _clip_valid(clip_lineage,current_source) or proof.get('clipLineage')!=clip_lineage:return 'closing-clip-lineage-changed'
        fade=4. if active_db>-35 else 0.
    else:
        if clip_lineage is not None or proof.get('clipLineage') is not None:return 'closing-clip-kind-mismatch'
        fade=0.
    if row['fadeOut']!=fade:return 'closing-imposed-exit-fade'
    policy='owner-clip-generated-softout' if fade else 'no-imposed-exit-fade'
    if proof.get('fadeOutPolicy')!=policy:return 'closing-fade-policy'
    if proof.get('riskFlags')!=ending_flags(proof.get('storedSourceKey'),end,diagnostics,is_clip,fade):return 'closing-ending-flags'
    if any(proof.get(k) is not True for k in ('nativeCompletionRequired','endingAtStoredFileBoundary')):return 'closing-completion-contract'
    if any(proof.get(k) is not False for k in ('quietClaimed','wordAlignmentClaimed','artistIntendedEndingClaimed','entryVerifiedByEndingProof')):return 'closing-false-claim'
    return None


def saved_selection_error(item):
    """Cached structural check only; final assembly must replay CURRENT DB state."""
    proof=item.get('sourceEnding',{})
    return selection_error(item,proof.get('sourceSnapshot',{}),clip_lineage=proof.get('clipLineage'))


def preserve_existing_exit(item):
    """An entry-only failure never authorizes replacing a confirmed exit."""
    if item.get('stereoError') or item.get('stereoEvidence',{}).get('accepted') is False:
        return False
    return (item.get('exit',{}).get('method') not in METHODS | {'reobserved-instrumental-fade','instrumental-fade'} and
            exit_error({**item,'status':'ready'}) is None)


def run(settings, work, decoder, limit=None):
    from .disk import Disk
    if limit is not None and limit<0:raise RuntimeError('CLOSING_LIMIT')
    decoder=shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file() or not os.access(decoder,os.X_OK):raise RuntimeError('CLOSING_DECODER')
    version=subprocess.run([decoder,'-version'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
    if version.returncode or not version.stdout.startswith(b'ffmpeg version'):raise RuntimeError('CLOSING_DECODER')
    baseline=read_json(work/'baseline.json');curation=settings.state/'research/dj1-curation'
    owner=read_json(curation/'owner-entries.json');rejected=read_json(curation/'owner-rejected.json')
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(replace(settings,max_bytes=min(settings.max_bytes,40*1024**2)));count=0
    try:
        for position,old in enumerate(baseline['tracks'],1):
            path=work/'tracks'/(old['trackId']+'.json');previous=read_json(path)
            # Entry-only failures cannot erase an already confirmed exit.
            if preserve_existing_exit(previous):continue
            if limit is not None and count>=limit:break
            asset=db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,l.integrated_lufs AS lufs,
                l.true_peak_dbtp AS peak,l.method AS loudnessMethod,l.status AS loudnessStatus
                FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''',(old['trackId'],)).fetchone()
            if not asset or asset['md5']!=old['md5']:raise RuntimeError('CLOSING_SOURCE_CHANGED')
            song=dict(asset);song['lo']=song['lo'] or 0.;song['hi']=song['duration'] if song['hi'] is None else song['hi']
            lineage=clip_lineage(db,old['trackId'])
            if previous.get('exit',{}).get('method') in METHODS and selection_error({**previous,'status':'ready'},song,clip_lineage=lineage) is None:continue
            if song['hi']<song['duration']-.001:
                atomic_json(path,{**previous,'status':'needs-review','closingError':'CLOSING_MANUAL_END_HOLD'});count+=1;continue
            if not 0<song['bytes']<=40*1024**2 or not 0<song['duration']<=1800:raise RuntimeError('CLOSING_SOURCE_LIMIT')
            if shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('CLOSING_DISK')
            raw=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(old['md5'],)).fetchone()
            if raw is None:raise RuntimeError('CLOSING_ASR_MISSING')
            evidence=restore_evidence(json.loads(raw[0]),previous)
            try:
                with tempfile.TemporaryDirectory(prefix='closing-original-',dir=work) as directory:
                    source=Path(directory)/('source'+Path(song['remote_path']).suffix)
                    disk.download(song['remote_path'],source,expected_md5=old['md5'])
                    if source.stat().st_size!=song['bytes']:raise RuntimeError('CLOSING_SOURCE_SIZE')
                    observed={}
                    def collect(pcm,timing):observed['tail']=observe_tail(pcm,timing)
                    timing=measure_source_timing(source,song,decoder,directory,observer=collect)
                    result=create_checkpoint(old,song,previous,evidence,timing,observed['tail'],
                        owner=owner.get(old['trackId'],[]),rejected=rejected.get(old['trackId'],[]),lineage=lineage)
                    error=selection_error(result,song,clip_lineage=lineage)
                    if error:raise RuntimeError('CLOSING_CHECK_'+error)
            except Exception as exc:
                code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith(('CLOSING_','HARD_REVIEW_')) else 'CLOSING_'+type(exc).__name__.upper()
                result={**previous,'status':'needs-review','closingError':code,'closingVersion':VERSION}
            result['position']=position;atomic_json(path,result);count+=1
            print('CLOSING_REVIEWED',position,result['status'],old['trackId'],result.get('closingError','ok'),flush=True)
    finally:db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--ffmpeg',required=True);parser.add_argument('--limit',type=int);args=parser.parse_args()
    run(Settings(),args.work,args.ffmpeg,args.limit)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith(('CLOSING_','HARD_REVIEW_')) else 'CLOSING_'+type(exc).__name__.upper()
        print(code,flush=True);raise SystemExit(1)
