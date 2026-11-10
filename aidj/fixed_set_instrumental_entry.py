"""Strong, independently corroborated instrumental entries, never word erasure.

Only pending incoming cuts may change. Existing source-pinned UVR evidence and
raw stereo MIX Silero evidence must agree throughout a complete two seconds.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import resource
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import replace

from .config import Settings,atomic_json
from .fixed_set_repair import covered,selection_error as exit_error
from .fixed_set_resolve import read_json
from .fixed_set_vocal_review import CachedProbe,_vectors,finite,gain_db,quantile
from .fixed_set_hard_review import all_existing,references
from .vocal_probe import MODEL_SHA256
from .vocal_speech import MODEL_FILES,INPUT_RATE,MODEL_RATE,WINDOW

VERSION=1
METHOD='corroborated-instrumental-entry'
PARAMETERS={'beforeSeconds':.75,'afterSeconds':1.25,'vocalMaxDbfs':-55.,'vocalP95Dbfs':-60.,
    'mixSpeechMax':.01,'vocalContextGuardSeconds':.25,'mixWarmupSeconds':1.,
    'mixEndGuardSeconds':.25,'fadeInSeconds':.15,'negativeOwnerRadiusSeconds':.35}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def mix_identity(song,begin,end):
    return {'version':1,'kind':'raw-stereo-mix-at-current-playback-gain','sourceMD5':song['md5'],
        'sourceBytes':song['bytes'],'sourceOffsetSeconds':begin,'requestedEndSeconds':end,
        'playbackGainDb':gain_db(song),'modelSHA256':{k:v['sha256'] for k,v in MODEL_FILES.items()},
        'inputRate':INPUT_RATE,'channels':2}


def validate_mix(value,song):
    """Validate real channel/frame geometry, pins and CURRENT asset/level identity."""
    if not isinstance(value,dict):return None
    identity=value.get('identity',{});a=identity.get('sourceOffsetSeconds');b=identity.get('requestedEndSeconds')
    if not finite(a) or not finite(b) or not 0<=a<b<=song['duration']+.0001 or b-a>20.0001:return None
    if identity!=mix_identity(song,a,b) or value.get('cacheKey')!=digest(identity):return None
    profile={k:song.get(k) for k in ('lufs','peak','loudnessMethod','loudnessStatus')}
    if (value.get('version')!=1 or value.get('sourceMD5')!=song['md5'] or
            value.get('sourceOriginalMD5Verified') is not True or value.get('sourceDuration')!=song['duration'] or
            value.get('sourceRange')!=[song['lo'],song['hi']] or value.get('playbackGainDb')!=gain_db(song) or
            value.get('playbackProfile')!=profile or value.get('normalizedInputLayout')!='channel-major-float32-little' or
            any(value.get(k) is not False for k in ('uvrInferred','stemInput','channelsAveraged'))):return None
    decode=value.get('decode',{});n=decode.get('pcmFrames')
    if (type(n) is not int or not 4410<=n<=20*INPUT_RATE or type(decode.get('pcmBytes')) is not int or decode.get('pcmBytes')!=8*n or
            decode.get('method')!='ffmpeg-xerror-20s-stereo-f32le' or decode.get('sampleRate')!=INPUT_RATE or
            decode.get('channels')!=2 or decode.get('sourceOffsetSeconds')!=a or
            decode.get('actualEndSeconds')!=a+n/INPUT_RATE or abs(a+n/INPUT_RATE-b)>.05):return None
    for h in (decode.get('pcmSHA256'),value.get('normalizedInputSHA256')):
        if not isinstance(h,str) or len(h)!=64 or any(c not in '0123456789abcdef' for c in h):return None
    speech=value.get('speech',{});frames=(n*160+441*WINDOW-1)//(441*WINDOW)
    real=(n*160+440)//441;padded=((real+64+WINDOW-1)//WINDOW)*WINDOW
    if (speech.get('method')!='silero-v5-stereo-speech-probability' or speech.get('version')!=1 or
            speech.get('modelArtifactsVerified') is not True or speech.get('modelSHA256')!=identity['modelSHA256'] or
            speech.get('modelWrapperVersion')!='faster-whisper-1.1.1' or
            speech.get('sourceOffsetSeconds')!=a or speech.get('sourceEndSeconds')!=a+n/INPUT_RATE or
            speech.get('durationSeconds')!=n/INPUT_RATE or
            speech.get('inputSampleRate')!=INPUT_RATE or speech.get('inputSamples')!=n or
            speech.get('modelSampleRate')!=MODEL_RATE or speech.get('resampledSamples')!=real or
            speech.get('modelPaddedSamples')!=padded or speech.get('frameHopSeconds')!=WINDOW/MODEL_RATE or
            speech.get('paddingFramesExcluded')!=padded//WINDOW-frames or
            speech.get('rawProbabilityShape') not in ([2,padded//WINDOW],[2,padded//WINDOW,1])):return None
    arrays=[speech.get(k) for k in ('frameStartSeconds','frameEndSeconds','leftSpeechProbability',
                                   'rightSpeechProbability','maxSpeechProbability')]
    if any(not isinstance(v,list) or len(v)!=frames for v in arrays):return None
    for i,(lo,hi,left,right,maximum) in enumerate(zip(*arrays)):
        if (not all(finite(v) for v in (lo,hi,left,right,maximum)) or
                abs(lo-(a+i*WINDOW/MODEL_RATE))>1e-7 or
                abs(hi-min(a+(i+1)*WINDOW/MODEL_RATE,a+n/INPUT_RATE))>1e-7 or
                not 0<=left<=1 or not 0<=right<=1 or abs(max(left,right)-maximum)>1e-7):return None
    return speech


def voice_metrics(vocal,point,song,coverage,rejected=(),prepared=None):
    if (not finite(point) or not song['lo']<=point<song['hi'] or
            any(abs(point-t)<.35 for t in rejected) or vocal.get('md5')!=song['md5'] or
            any(w!='near-zero-vocal-output' for w in vocal.get('warnings',[]))):return None
    prepared=_vectors(vocal,allow_warnings=True) if prepared is None else prepared
    if prepared is None:return None
    offset,hop,duration,values=prepared;a=point-.75;b=point+1.25
    if a<song['lo'] or b>song['hi'] or a<offset+.25-1e-7 or b>offset+duration-.25+1e-7:return None
    if not covered(coverage,a,b):return None
    first=math.floor((a-offset)/hop+1e-8);last=math.ceil((b-offset)/hop-1e-8)
    voice=values[1][first:last];level=gain_db(song)
    maximum=20*math.log10(max(1e-10,max(voice)))+level
    p95=20*math.log10(max(1e-10,quantile(voice,.95)))+level
    if maximum>-55 or p95>-60:return None
    return {'proofStart':a,'proofEnd':b,'vocalFrameFirst':first,'vocalFrameLastExclusive':last,
        'normalizedVocalMaxDbfs':maximum,'normalizedVocalP95Dbfs':p95,
        'vocalRmsGeometry':'sqrt-mean-stereo-channel-squares',
        'perChannelVocalMaxUpperBoundDbfs':maximum+10*math.log10(2),
        'perChannelVocalP95UpperBoundDbfs':p95+10*math.log10(2),
        'uvrModelSha256':MODEL_SHA256,'playbackGainDb':level,'allowedVocalWarnings':list(vocal.get('warnings',[]))}


def mix_covers(speech,a,b):
    return a>=speech['sourceOffsetSeconds']+1-1e-7 and b<=speech['sourceEndSeconds']-.25+1e-7


def check_joint(vocal,mix,point,song,words,coverage,*,rejected=()):
    reject=lambda why:{'accepted':False,'method':METHOD,'reason':why}
    voice=voice_metrics(vocal,point,song,coverage,rejected)
    if voice is None:return reject('unproven-two-second-vocal-quiet')
    speech=validate_mix(mix,song)
    if speech is None:return reject('invalid-raw-mix-speech-evidence')
    a,b=voice['proofStart'],voice['proofEnd']
    if not mix_covers(speech,a,b):return reject('mix-warmup-or-end-edge')
    indexes=[i for i,(lo,hi) in enumerate(zip(speech['frameStartSeconds'],speech['frameEndSeconds']))
             if lo<b-1e-8 and hi>a+1e-8]
    if not indexes or speech['frameStartSeconds'][indexes[0]]>a+1e-7 or speech['frameEndSeconds'][indexes[-1]]<b-1e-7:
        return reject('incomplete-mix-frames')
    left=max(speech['leftSpeechProbability'][i] for i in indexes)
    right=max(speech['rightSpeechProbability'][i] for i in indexes)
    if max(left,right)>.01:return reject('raw-mix-speech-at-entry')
    if any(not isinstance(w,(list,tuple)) or len(w)<3 or not all(finite(t) for t in w[:2]) or w[0]>w[1] for w in words):
        return reject('invalid-retained-ASR')
    crossings=[list(w) for w in words if w[0]<point+.02-1e-7 and w[1]>point-.02+1e-7]
    retained=[list(w) for w in words if w[0]<b and w[1]>a]
    return {'accepted':True,'method':METHOD,'reason':'accepted','parameters':dict(PARAMETERS),
        'sourceMD5':song['md5'],'sourceStart':point,**voice,
        'mixFrameFirst':indexes[0],'mixFrameLastExclusive':indexes[-1]+1,
        'leftMixSpeechMax':left,'rightMixSpeechMax':right,'mixModelSHA256':speech['modelSHA256'],
        'mixNormalizedInputSHA256':mix['normalizedInputSHA256'],'mixPCMInputSHA256':mix['decode']['pcmSHA256'],
        'retainedASRCrossings':crossings,'retainedASRWordsInProof':retained,
        'asrCoverageComplete':True,'asrOverrideApplied':bool(crossings),
        'asrOverrideReason':'prolonged-joint-instrumental-interior' if crossings else None,
        'negativeOwnerInputHash':digest(list(rejected)),'fadeInSeconds':.15,
        'verifiedWordBoundary':False,'naturalSilenceClaimed':False,'ASRWordsErased':False}


def candidates(item,song,values,coverage,rejected=()):
    out=[];end=item['row']['end']
    for value in values:
        prepared=_vectors(value,allow_warnings=True)
        if prepared is None:continue
        offset,hop,duration,_=prepared
        low=max(song['lo'],end-100,offset+1);high=min(song['hi'],end-45,offset+duration-1.5)
        for i in range(math.ceil((low-offset)/hop),math.floor((high-offset)/hop)+1):
            point=round(offset+i*hop,4)
            check=voice_metrics(value,point,song,coverage,rejected,prepared)
            if check is not None:
                out.append({'point':point,'vocal':value,
                    'score':abs(point-item['row']['start'])/40+abs(end-point-62)/100})
    return sorted(out,key=lambda x:(x['score'],x['point']))


def select(item,song,values,mixes,*,owner=(),rejected=()):
    if not item.get('entryReviewOnly'):return None
    if any(abs(item['row']['start']-t)<=.0001 for t in owner):return None
    if item.get('md5')!=song['md5'] or exit_error({**item,'status':'ready'}):return None
    from .fixed_set_entry_review import fingerprint,VERSION as ENTRY_VERSION
    for candidate in candidates(item,song,values,item.get('observedCoverage',[]),rejected):
        for mix in mixes:
            check=check_joint(candidate['vocal'],mix,candidate['point'],song,
                item.get('observedWords',[]),item.get('observedCoverage',[]),rejected=rejected)
            if not check['accepted']:continue
            result=copy.deepcopy(item)
            result['row'].update(start=candidate['point'],fadeIn=.15)
            proof={**check,'cacheKey':candidate['vocal']['cacheKey'],'mixCacheKey':mix['cacheKey']}
            result.update(entryProof=proof,entryReviewVersion=ENTRY_VERSION,
                entryReviewStatus='ready',entryReviewOnly=False,status='ready',
                instrumentalEntryVersion=VERSION,entryShiftNeedsStereoRecheck=True)
            for key in ('stereoEvidence','stereoVersion','stereoError','entryReviewError','entryResolveError'):
                result.pop(key,None)
            result['entry']={**result.get('entry',{}),'time':candidate['point'],'kind':METHOD}
            result['entryReviewInputHash']=fingerprint(result,song,owner)
            return result
    return None


def selection_error(item,song,*,owner=(),evidence_folder,rejected=None):
    try:
        folder=Path(evidence_folder);proof=item.get('entryProof',{});point=item.get('row',{}).get('start')
        if not folder.is_dir():return 'instrumental-entry-vocal-cache'
        if rejected is None:
            rejected=read_json(folder.parent.parent/'dj1-curation'/'owner-rejected.json').get(item['trackId'],[])
        key=proof.get('cacheKey');mixkey=proof.get('mixCacheKey')
        if any(not isinstance(k,str) or len(k)!=64 or any(c not in '0123456789abcdef' for c in k) for k in (key,mixkey)):
            return 'instrumental-entry-key'
        adapter=CachedProbe(Path('/unused'),song,folder,'unused',Path('/unused'))
        raw=all_existing(adapter,[{'cacheKey':key}])
        if not raw:return 'instrumental-entry-vocal-cache'
        mix=read_json(folder.parent/'mix-vad-probes'/(mixkey+'.json'))
        check=check_joint(raw[0],mix,point,song,item.get('observedWords',[]),item.get('observedCoverage',[]),rejected=rejected)
        if not check['accepted']:return 'instrumental-entry-'+check['reason']
        if proof!={**check,'cacheKey':key,'mixCacheKey':mixkey}:return 'instrumental-entry-proof-mismatch'
        row=item['row'];span=row['end']-point
        if not 45<=span<=100 or row.get('fadeIn')!=.15 or point+.15>check['proofEnd']:
            return 'instrumental-entry-row'
        return None
    except (OSError,ValueError,TypeError,KeyError,RuntimeError):return 'instrumental-entry-evidence-unavailable'


def cached_mixes(folder,song):
    out=[]
    for path in sorted(folder.glob('*.json')):
        with path.open('rb') as stream:head=stream.read(4096)
        if song['md5'].encode() not in head:continue
        value=read_json(path)
        if value.get('cacheKey')==path.stem and validate_mix(value,song) is not None:out.append(value)
    return out


def probe_mix(source,song,begin,end,decoder,folder):
    """Only new native work here: pinned Silero on original stereo, no UVR/ASR."""
    begin=round(begin*INPUT_RATE)/INPUT_RATE;end=round(end*INPUT_RATE)/INPUT_RATE
    identity=mix_identity(song,begin,end);key=digest(identity);target=folder/(key+'.json')
    if target.exists():
        old=read_json(target)
        if validate_mix(old,song) is not None:return old
        raise RuntimeError('INSTRUMENTAL_ENTRY_CACHE')
    if source.stat().st_size!=song['bytes']:raise RuntimeError('INSTRUMENTAL_ENTRY_SOURCE_SIZE')
    with source.open('rb') as stream:
        if hashlib.file_digest(stream,'md5').hexdigest()!=song['md5']:raise RuntimeError('INSTRUMENTAL_ENTRY_SOURCE_MD5')
    with tempfile.TemporaryDirectory(prefix='instrumental-mix-',dir=folder) as directory:
        pcm=Path(directory)/'excerpt.f32'
        child=subprocess.run([decoder,'-nostdin','-v','error','-xerror','-threads','1','-filter_threads','1',
            '-ss',str(begin),'-i',str(source),'-t',str(end-begin),'-vn','-sn','-dn','-ac','2','-ar',str(INPUT_RATE),
            '-f','f32le','-fs',str(INPUT_RATE*20*8+8),'-y',str(pcm)],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
        if child.returncode or not pcm.exists() or not 0<pcm.stat().st_size<=INPUT_RATE*20*8 or pcm.stat().st_size%8:
            raise RuntimeError('INSTRUMENTAL_ENTRY_DECODE')
        import numpy as np
        from .vocal_speech import speech_evidence
        n=pcm.stat().st_size//8
        if abs(begin+n/INPUT_RATE-end)>.05:raise RuntimeError('INSTRUMENTAL_ENTRY_DECODE_DURATION')
        with pcm.open('rb') as stream:rawhash=hashlib.file_digest(stream,'sha256').hexdigest()
        wave=np.fromfile(pcm,dtype='<f4').reshape(-1,2).T
        normalized=np.ascontiguousarray(wave*np.float32(10**(gain_db(song)/20)),dtype='<f4')
        speech=speech_evidence(normalized,begin)
        value={'version':1,'cacheKey':key,'identity':identity,'sourceMD5':song['md5'],
            'sourceOriginalMD5Verified':True,'sourceDuration':song['duration'],'sourceRange':[song['lo'],song['hi']],
            'playbackGainDb':gain_db(song),'playbackProfile':{k:song.get(k) for k in ('lufs','peak','loudnessMethod','loudnessStatus')},
            'decode':{'method':'ffmpeg-xerror-20s-stereo-f32le','sampleRate':INPUT_RATE,'channels':2,'pcmFrames':n,
                'pcmBytes':n*8,'sourceOffsetSeconds':begin,'actualEndSeconds':begin+n/INPUT_RATE,'pcmSHA256':rawhash},
            'normalizedInputSHA256':hashlib.sha256(memoryview(normalized)).hexdigest(),
            'normalizedInputLayout':'channel-major-float32-little','speech':speech,
            'peakRssKiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'uvrInferred':False,'stemInput':False,'channelsAveraged':False,'audioRetained':False}
        if validate_mix(value,song) is None:raise RuntimeError('INSTRUMENTAL_ENTRY_INVALID_SPEECH')
        atomic_json(target,value)
    return value


def run(settings,work,decoder,limit=None,retry=False):
    from .disk import Disk
    if limit is not None and limit<0:raise RuntimeError('INSTRUMENTAL_ENTRY_LIMIT')
    decoder=shutil.which(str(decoder)) or str(decoder)
    if not Path(decoder).is_file():raise RuntimeError('INSTRUMENTAL_ENTRY_DECODER')
    plan=read_json(work/'baseline.json');curation=settings.state/'research/dj1-curation'
    owners=read_json(curation/'owner-entries.json');bad=read_json(curation/'owner-rejected.json')
    folder=work/'mix-vad-probes';folder.mkdir(exist_ok=True)
    db=sqlite3.connect(f'file:{settings.state/"bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(replace(settings,max_bytes=min(settings.max_bytes,40*1024**2)));count=0
    try:
        for old in plan['tracks']:
            path=work/'tracks'/(old['trackId']+'.json');item=read_json(path)
            if not item.get('entryReviewOnly'):continue
            if item.get('instrumentalEntryVersion')==VERSION and not retry:continue
            if limit is not None and count>=limit:break
            asset=db.execute('''SELECT a.*,r.start AS lo,r.end AS hi,l.integrated_lufs AS lufs,
                l.true_peak_dbtp AS peak,l.method AS loudnessMethod,l.status AS loudnessStatus FROM web_assets a
                LEFT JOIN playback_ranges r ON r.track_id=a.id LEFT JOIN audio_loudness l ON l.md5=a.md5
                WHERE a.id=?''',(old['trackId'],)).fetchone()
            if not asset or asset['md5']!=old['md5']:raise RuntimeError('INSTRUMENTAL_ENTRY_SOURCE_CHANGED')
            song=dict(asset);song['manualStart']=song['lo'] is not None
            song['lo']=song['lo'] or 0.;song['hi']=song['duration'] if song['hi'] is None else song['hi']
            if not 0<song['bytes']<=40*1024**2 or not 0<song['duration']<=1800:raise RuntimeError('INSTRUMENTAL_ENTRY_SOURCE_LIMIT')
            owner=owners.get(old['trackId'],[]);rejected=bad.get(old['trackId'],[])
            if exit_error({**item,'status':'ready'}):raise RuntimeError('INSTRUMENTAL_ENTRY_PREVIOUS_EXIT')
            adapter=CachedProbe(Path('/unused'),song,work/'vocal-evidence',decoder,Path('/unused'))
            values=all_existing(adapter,references(item.get('entryVocalEvidence',[]),item.get('vocalEvidence',[])))
            choices=candidates(item,song,values,item.get('observedCoverage',[]),rejected)
            if not choices or any(abs(item['row']['start']-t)<=.0001 for t in owner):
                print('INSTRUMENTAL_ENTRY_NO_CANDIDATE',item.get('position'),old['trackId'],flush=True);continue
            mixes=cached_mixes(folder,song);result=select(item,song,values,mixes,owner=owner,rejected=rejected);probes=0
            with tempfile.TemporaryDirectory(prefix='instrumental-original-',dir=work) as directory:
                source=Path(directory)/('source'+Path(song['remote_path']).suffix);downloaded=False
                while result is None and probes<2:
                    unobserved=next((c for c in choices if not any(mix_covers(m['speech'],c['point']-.75,c['point']+1.25) for m in mixes)),None)
                    if unobserved is None:break
                    if shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('INSTRUMENTAL_ENTRY_DISK')
                    if not downloaded:
                        disk.download(song['remote_path'],source,expected_md5=song['md5'])
                        if source.stat().st_size!=song['bytes']:raise RuntimeError('INSTRUMENTAL_ENTRY_SOURCE_SIZE')
                        downloaded=True
                    begin=max(0.,min(song['duration']-20,unobserved['point']-10));end=min(song['duration'],begin+20)
                    mixes.append(probe_mix(source,song,begin,end,decoder,folder));probes+=1
                    result=select(item,song,values,mixes,owner=owner,rejected=rejected)
            if result is None:
                result={**item,'instrumentalEntryVersion':VERSION,'instrumentalEntryStatus':'needs-review'}
            else:
                from .fixed_set_entry_review import selection_error as public_gate
                if public_gate(result,song,owner=owner,evidence_folder=work/'vocal-evidence'):
                    raise RuntimeError('INSTRUMENTAL_ENTRY_FINAL_GATE')
                result['instrumentalEntryStatus']='ready'
            result['instrumentalEntryNewMixProbes']=probes
            assert result['exit']==item['exit'] and result['row']['end']==item['row']['end'] and result.get('sourceEnding')==item.get('sourceEnding')
            atomic_json(path,result);count+=1
            print('INSTRUMENTAL_ENTRY',item.get('position'),result.get('instrumentalEntryStatus'),old['trackId'],flush=True)
    finally:db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True);parser.add_argument('--ffmpeg',required=True)
    parser.add_argument('--limit',type=int);parser.add_argument('--retry',action='store_true');args=parser.parse_args()
    run(Settings(),args.work,args.ffmpeg,args.limit,args.retry)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('INSTRUMENTAL_ENTRY_') else 'INSTRUMENTAL_ENTRY_'+type(exc).__name__.upper()
        print(code,flush=True);raise SystemExit(1)
