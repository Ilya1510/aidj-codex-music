"""Sequential CPU preparation. No LLM API, audio uploads or playlist mutations.

Optional environment: requirements-dj-runtime.txt. A bounded service owns one
scratch file at a time. Only numeric cues survive; transcripts are not stored.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from .config import Settings
from .store import connect
from .dj import complete,next_job,heartbeat,upgrade_queue


def vocal_cues(words,beats,bpm,duration):
    """Require a bounded gap between recognised phrases, not missing ASR output.

    Fades stay after the preceding word and before the following word. This is
    conservative evidence, not a guarantee: singing can confuse ASR too.
    """
    if len(words)<8:return []
    words=sorted((w[0],w[1],w[2] if len(w)>2 else True) for w in words)
    gaps=[];end=words[0][1];confident=words[0][2]
    for start,stop,next_confident in words[1:]:
        # Incoming audio can fade in early. The outgoing deck stays at full gain
        # until after the final word, so the available gap needn't cover the whole mix.
        if confident and next_confident and .9<=start-end<=5:gaps.append((end+.2,start-.2))
        if stop>=end:end=stop;confident=next_confident
    cues=[]
    for target in (80,110,140):
        choices=[]
        for lo,hi in gaps:
            if hi>duration or not 45<=hi<=190 or abs(hi-target)>28:continue
            # A few extra seconds are preferable to an unnecessarily abrupt
            # exit: use more of the confirmed vocal gap before chasing the grid.
            candidates=[t for t in beats if lo+.5<=t<=hi]
            if hi-lo>=.5:candidates.append(hi)
            if not candidates:continue
            stop=min(candidates,key=lambda t:abs(t-target)+8*max(0,1.2-min(2,t-lo)))
            fade=min(2,stop-lo)
            if fade<.5:continue
            cue={'start':0,'end':round(stop,4),'bpm':round(bpm,4),'fade':round(fade,4),'mix':3.2}
            if bpm and beats:cue['grid']=round(beats[0]%(60/bpm),6)
            choices.append((abs(stop-target)+8*max(0,1.2-fade),cue))
        if choices:
            cue=min(choices,key=lambda c:c[0])[1]
            if cue not in cues:cues.append(cue)
    return cues


def coverage(evidence):
    if 'coverage' in evidence:return evidence['coverage']
    # v2 transcribed the three contiguous 45-second windows, plus occupied
    # separators. Earlier hand-authored numeric fixtures covered the full file.
    return [[45,min(180,evidence['duration'])]] if evidence.get('version')==2 else [[0,evidence['duration']]]


def vocal_gaps(evidence,minimum=.9):
    words=sorted((float(w[0]),float(w[1]),bool(w[2]) if len(w)>2 else True) for w in evidence['words'])
    if len(words)<8:return []
    result=[];end=words[0][1];confident=words[0][2]
    for start,stop,good in words[1:]:
        # A missing transcription window must never look like an instrumental
        # gap. Both neighbours and the complete gap need observed coverage.
        observed=any(lo+.4<=end and start<=hi-.4 for lo,hi in coverage(evidence))
        if confident and good and observed and minimum<=start-end<=5:result.append((end+.2,start-.2))
        if stop>=end:end=stop;confident=good
    return result


def section_energy(evidence,start,end):
    values=evidence.get('energy',[])
    if not values:return .5
    part=values[max(0,int(start)):max(int(start)+1,int(end))]
    return sum(part)/len(part) if part else .5


def selection(evidence):
    duration=evidence['duration'];bpm=evidence['bpm'];beats=evidence['beats'];gaps=vocal_gaps(evidence)
    def cue(start,lo,end):
        result={'start':round(start,4),'end':round(end,4),'bpm':round(bpm,4),'fade':round(min(2,end-lo),4),
                'mix':3.2,'entryVerified':True,'energy':round(section_energy(evidence,start,end),4)}
        if bpm and beats:result['grid']=round(beats[0]%(60/bpm),6)
        return result
    exits=[(lo,hi) for lo,hi in gaps if hi-lo>=.5 and hi<=duration]
    # Previously validated endpoints remain evidence even if a short new ASR
    # observation has lower confidence. They are bound to this same source MD5.
    for lo,hi in evidence.get('safeExits',[]):
        if 0<=lo<hi<=duration and .5<=hi-lo<=8 and (lo,hi) not in exits:exits.append((lo,hi))
    entry_gaps=vocal_gaps(evidence,.5)
    entries=[0]+[lo for lo,hi in entry_gaps if 12<=lo<=min(130,duration-35)]
    cues=[]
    for target in (80,110,140):
        choices=[]
        for start in entries:
            for lo,end in exits:
                length=end-start
                if not max(35,target-28)<=length<=target+28:continue
                # Prefer an active complete section over a quiet opening. The
                # original start remains a safe fallback when no entry was found.
                energy=section_energy(evidence,start,end)
                cost=abs(length-target)/target+.6*(1-energy)+(.22 if start==0 else 0)+.12*max(0,1.2-min(2,end-lo))
                choices.append((cost,cue(start,lo,end)))
        if choices:
            chosen=min(choices,key=lambda item:item[0])[1]
            if chosen not in cues:cues.append(chosen)
    remixes=[]
    for start in [0]+[lo for lo,hi in entry_gaps]:
        for lo,end in exits:
            if 8<=end-start<=24:
                c=cue(start,lo,end)
                cost=abs(end-start-14)/20+.8*(1-c['energy'])+(.15 if start==0 else 0)
                remixes.append((cost,c))
    remix=[]
    for _,candidate in sorted(remixes,key=lambda item:item[0]):
        # Return genuinely different excerpts, not twelve near-identical cuts.
        if all(abs(candidate['start']-old['start'])>=6 for old in remix):remix.append(candidate)
        if len(remix)>=6:break
    reason=('few-recognised-words' if len(evidence['words'])<8 else 'no-safe-gap') if not cues else None
    return {'version':3,'cues':cues,'remix':remix,'method':'analysed','reason':reason}


def rhythm(y,sr):
    """Beat tracking with a 16-beat estimate, avoiding single-hop BPM quantisation."""
    import numpy as np
    import librosa
    hop=256
    onset=librosa.onset.onset_strength(y=y,sr=sr,hop_length=hop)
    _,frames=librosa.beat.beat_track(onset_envelope=onset,sr=sr,hop_length=hop,trim=False)
    beats=librosa.frames_to_time(frames,sr=sr,hop_length=hop)
    if len(beats)<64:return [],0
    spans=beats[16:]-beats[:-16];span=float(np.median(spans));bpm=60*16/span
    if not 60<=bpm<=210 or float(np.median(np.abs(spans-span)))>.20:return [],0
    return [float(t) for t in beats],float(bpm)


def numeric_words(part,offset):
    # faster-whisper alignment exposes NumPy probabilities. Convert every value
    # before saving evidence; numpy.bool_ is not JSON serializable.
    confident=bool(part.no_speech_prob<=.8 and part.avg_logprob>=-.85)
    if not part.words:return [(float(offset+part.start),float(offset+part.end),False)]
    return [(float(offset+w.start),float(offset+w.end),bool(confident and w.probability>=.45)) for w in part.words if w.end>w.start]


def energy_envelope(y,sr):
    import numpy as np
    values=[float(np.sqrt(np.mean(y[t:t+sr]**2))) for t in range(0,len(y),sr)]
    scale=float(np.quantile(values,.9)) if values else 1
    return [round(min(1,x/max(.0001,scale)),4) for x in values]


def probe_windows(evidence):
    """Two small endpoint observations first, a third only if still needed."""
    duration=evidence['duration'];energy=evidence.get('energy',[])
    if energy:
        # Seek a strong sustained section, not the loudest individual sample.
        starts=range(18,max(19,int(min(100,duration-100))),4)
        anchor=max(starts,key=lambda t:section_energy(evidence,t,t+24))
    else:anchor=min(60,max(18,duration/4))
    # A late probe can rescue continuous-vocal tracks whose old 45–180s pass
    # found no exit. Selection reuses all earlier observations too.
    centers=[anchor+8,min(duration-14,anchor+112),min(duration-14,anchor+48)]
    if evidence.get('version')==2:
        exits=vocal_gaps(evidence)+[tuple(x) for x in evidence.get('safeExits',[])]
        # Long windows can smear sung words across a rest. Re-observe a small
        # local phrase immediately before a known exit, instead of merely
        # recycling the same uncertain timestamps forever.
        near_exit=max(exits,key=lambda g:g[1]-g[0])[1]-9 if exits else anchor+8
        centers=[near_exit,min(duration-14,200),anchor+8]
    windows=[]
    for center in centers:
        lo=max(0,round(center-14));hi=min(duration,lo+28)
        if hi-lo<12:continue
        if any(abs(a-lo)<2 and abs(b-hi)<2 for a,b in evidence.get('probes',[])):continue
        if evidence.get('version')!=2 and any(a<=lo and hi<=b for a,b in coverage(evidence)):continue
        if not any(abs(lo-old[0])<8 for old in windows):windows.append((lo,hi))
    return windows[:3]


def analyse(file,ffmpeg,models,language=None,cached=None,checkpoint=None):
    import numpy as np
    from .config import atomic_json
    # Full structural pass is cheap and reused. ASR sees at most 3x28 seconds,
    # with no repeated temperature fallback/beam search over the whole song.
    raw=file.with_suffix('.f32');began=time.monotonic()
    evidence=dict(cached or {})
    try:
        result=subprocess.run([ffmpeg,'-nostdin','-v','error','-threads','1','-i',str(file),'-t','300','-vn','-ac','1','-ar','16000','-f','f32le','-y',str(raw)],capture_output=True,timeout=60)
        if result.returncode:raise RuntimeError('DECODE_FAILED')
        if raw.stat().st_size>16000*300*4+4096:raise RuntimeError('DECODE_TOO_LARGE')
        timing=raw.with_suffix('.rhythm.json');y=np.fromfile(raw,dtype=np.float32)
        if cached and 'beats' in evidence and 'bpm' in evidence and not evidence.get('energy'):
            evidence['energy']=energy_envelope(y,16000)
        if not evidence.get('energy'):
            try:subprocess.run([os.sys.executable,'-m','aidj.dj_worker','rhythm','--file',str(raw),'--output',str(timing),'--ffmpeg',ffmpeg],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
            except subprocess.CalledProcessError as exc:
                if exc.returncode in (-9,137):raise MemoryError('RHYTHM_RESOURCE_LIMIT') from None
                raise
            structure=json.loads(timing.read_text());timing.unlink()
            evidence.update(structure)
        evidence.setdefault('duration',len(y)/16000);evidence['duration']=len(y)/16000
        evidence['coverage']=coverage(evidence) if cached else []
        evidence.setdefault('words',[])
        evidence['timings']={'structure':round(time.monotonic()-began,3)}
        def save():
            if checkpoint:atomic_json(checkpoint,evidence)
        save()
        from faster_whisper import WhisperModel
        model=WhisperModel('base',device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,download_root=str(models))
        windows=probe_windows(evidence)
        asr_start=time.monotonic();analysed=0
        for lo,hi in windows:
            sample=y[int(lo*16000):int(hi*16000)]
            segments,_=model.transcribe(sample,language=language,beam_size=3,temperature=0.0,word_timestamps=True,condition_on_previous_text=False,vad_filter=False)
            words=[]
            for part in segments:words.extend(numeric_words(part,lo))
            # Keep occupied edges, replace only the freshly observed interior.
            # Empty/failed ASR cannot erase previous evidence.
            if len(words)>=8:
                evidence['words']=[w for w in evidence['words'] if not (lo+1<=w[0] and w[1]<=hi-1)]
                evidence['words'].extend(words);evidence['coverage'].append([lo,hi])
            evidence.setdefault('probes',[]).append([lo,hi]);analysed+=hi-lo
            # Window boundaries are not evidence of a pause. The coverage test
            # rejects a gap straddling separate, non-overlapping observations.
            evidence['timings'].update(asr=round(time.monotonic()-asr_start,3),observedSeconds=analysed)
            save();chosen=selection(evidence)
            if chosen['cues'] and len(chosen['remix'])>=2:break
        evidence['version']=3;evidence['timings']['total']=round(time.monotonic()-began,3);save()
        return {**selection(evidence),'evidence':evidence}
    finally:
        raw.unlink(missing_ok=True);raw.with_suffix('.rhythm.json').unlink(missing_ok=True)


def process(settings,db,row,ffmpeg):
    from .disk import Disk
    asset=db.execute('SELECT * FROM web_assets WHERE md5=? ORDER BY id LIMIT 1',(row['md5'],)).fetchone()
    if not asset or asset['bytes']>settings.max_bytes or not 30<=asset['duration']<=600:
        complete(db,row['md5'],{'cues':[]},max(0,asset['duration'] if asset else 0));return
    if asset['duration']<35:
        complete(db,row['md5'],{'cues':[],'reason':'too-short'},asset['duration']);return
    cached=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(row['md5'],)).fetchone()
    evidence=json.loads(cached[0]) if cached else {'version':2,'words':[],'duration':asset['duration'],'coverage':[]}
    previous=json.loads(row['payload']) if row['payload'] else {}
    evidence['safeExits']=[[c['end']-c['fade'],c['end']] for c in previous.get('cues',[])+previous.get('remix',[]) if .5<=c.get('fade',0)<=8]
    if cached:
        chosen=selection(evidence)
        if evidence.get('version',0)>=3 or chosen['cues'] and len(chosen['remix'])>=2:
            complete(db,row['md5'],chosen,asset['duration']);return
    if shutil.disk_usage(settings.state).free<768*1024**2:raise RuntimeError('LOW_DISK')
    with tempfile.TemporaryDirectory(prefix='dj-',dir=settings.state/'work') as scratch:
        file=Path(scratch)/('source'+Path(asset['remote_path']).suffix)
        Disk(settings).download(asset['remote_path'],file,expected_md5=row['md5'])
        # The child exits after each song, bounding native/JIT/model allocator growth.
        output=Path(scratch)/'cues.json';checkpoint=Path(scratch)/'evidence.json'
        cmd=[os.sys.executable,'-m','aidj.dj_worker','analyse','--file',str(file),'--output',str(output),'--ffmpeg',ffmpeg,'--checkpoint',str(checkpoint)]
        if cached or evidence['safeExits']:
            seed=Path(scratch)/'cached.json';seed.write_text(json.dumps(evidence));cmd+=['--cached',str(seed)]
        import re
        if re.search('[А-Яа-яЁё]',asset['artist']+' '+asset['title']):cmd+=['--language','ru']
        child=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            deadline=time.monotonic()+240
            while child.poll() is None:
                heartbeat(db,'working');remaining=deadline-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired(cmd,240)
                try:child.wait(timeout=min(10,remaining))
                except subprocess.TimeoutExpired:continue
            code=child.returncode
            if code in (-9,137):raise MemoryError('ANALYSIS_RESOURCE_LIMIT')
            if code!=0:raise RuntimeError('ANALYSIS_FAILED')
        except BaseException:
            if child.poll() is None:os.killpg(child.pid,signal.SIGKILL);child.wait()
            if checkpoint.exists() and checkpoint.stat().st_size<=256*1024:
                partial=json.loads(checkpoint.read_text());db.execute('INSERT OR REPLACE INTO dj_evidence VALUES (?,?,?)',(row['md5'],json.dumps(partial),time.time()));db.commit()
            raise
        if output.stat().st_size>256*1024:raise RuntimeError('METADATA_TOO_LARGE')
        value=json.loads(output.read_text());evidence=value.pop('evidence',None)
        if evidence:
            db.execute('INSERT OR REPLACE INTO dj_evidence VALUES (?,?,?)',(row['md5'],json.dumps(evidence),time.time()));db.commit()
        complete(db,row['md5'],value,asset['duration'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['serve','once','analyse','rhythm']);parser.add_argument('--file',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--ffmpeg',required=True);parser.add_argument('--language');parser.add_argument('--cached',type=Path);parser.add_argument('--checkpoint',type=Path);args=parser.parse_args()
    settings=Settings();settings.prepare()
    def terminate(signum,frame):raise SystemExit(0)
    signal.signal(signal.SIGTERM,terminate)
    if args.action=='rhythm':
        import numpy as np
        from .config import atomic_json
        if args.file.stat().st_size>16000*300*4+4096:raise RuntimeError('DECODE_TOO_LARGE')
        y=np.fromfile(args.file,dtype=np.float32);beats,bpm=rhythm(y,16000)
        atomic_json(args.output,{'beats':beats,'bpm':bpm,'energy':energy_envelope(y,16000)});return
    if args.action=='analyse':
        from .config import atomic_json
        atomic_json(args.output,analyse(args.file,args.ffmpeg,settings.state/'dj-models',args.language,json.loads(args.cached.read_text()) if args.cached else None,args.checkpoint));return
    with (settings.state/'dj-worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);db=connect(settings)
        db.execute("UPDATE dj_analysis SET status='pending' WHERE status='working'");db.commit();upgrade_queue(db);idle_heartbeat=0
        try:
            while True:
                row=next_job(db)
                if row:
                    heartbeat(db,'working')
                    claimed=db.execute("UPDATE dj_analysis SET status='working',updated=? WHERE md5=? AND status='pending'",(time.time(),row['md5'])).rowcount;db.commit()
                    if not claimed:continue
                    try:
                        process(settings,db,row,args.ffmpeg)
                        print('DJ_PREPARED',flush=True)
                    except MemoryError:
                        db.execute("UPDATE dj_analysis SET status='failed',updated=? WHERE md5=?",(time.time(),row['md5']));db.commit()
                        raise
                    except Exception as exc:
                        # No raw exceptions: network errors may contain signed URLs.
                        db.execute("UPDATE dj_analysis SET status='failed',updated=? WHERE md5=?",(time.time(),row['md5']));db.commit()
                        print('DJ_PREPARATION_FAILED',type(exc).__name__,flush=True)
                    if args.action=='once':break
                elif args.action=='once':break
                else:
                    if time.monotonic()-idle_heartbeat>20:heartbeat(db,'idle');idle_heartbeat=time.monotonic()
                    time.sleep(2)
        finally:
            db.rollback();db.execute("UPDATE dj_analysis SET status='pending' WHERE status='working'");heartbeat(db,'stopped')


if __name__=='__main__':
    try:main()
    except MemoryError:
        print('DJ_RESOURCE_LIMIT_STOPPED',flush=True);raise SystemExit(137)
