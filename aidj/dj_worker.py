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


def selection(evidence):
    cues=vocal_cues(evidence['words'],evidence['beats'],evidence['bpm'],evidence['duration'])
    return {'cues':cues,'method':'analysed','reason':('few-recognised-words' if len(evidence['words'])<8 else 'no-safe-gap') if not cues else None}


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


def analyse(file,ffmpeg,models,language=None):
    import numpy as np
    # Bounded to 250 seconds: all automatic cuts end before 245 seconds.
    raw=file.with_suffix('.f32')
    try:
        result=subprocess.run([ffmpeg,'-nostdin','-v','error','-threads','1','-i',str(file),'-t','180','-vn','-ac','1','-ar','16000','-f','f32le','-y',str(raw)],capture_output=True,timeout=60)
        if result.returncode:raise RuntimeError('DECODE_FAILED')
        if raw.stat().st_size>16000*250*4+4096:raise RuntimeError('DECODE_TOO_LARGE')
        timing=raw.with_suffix('.rhythm.json')
        # Separate phases: librosa/Numba exits before the speech model is loaded.
        try:subprocess.run([os.sys.executable,'-m','aidj.dj_worker','rhythm','--file',str(raw),'--output',str(timing),'--ffmpeg',ffmpeg],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        except subprocess.CalledProcessError as exc:
            if exc.returncode in (-9,137):raise MemoryError('RHYTHM_RESOURCE_LIMIT') from None
            raise
        grid=json.loads(timing.read_text());timing.unlink();beats,bpm=grid['beats'],grid['bpm']
        y=np.fromfile(raw,dtype=np.float32)
        from faster_whisper import WhisperModel
        model=WhisperModel('base',device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,download_root=str(models))
        # A NumPy input also avoids a PyAV decoder compatibility issue.
        words=[]
        # Singing is transcribed more reliably with short independent windows than
        # a long music recording. Overlaps are occupied to prevent invented gaps.
        for offset in range(45,min(180,int(len(y)/16000)),45):
            sample=y[offset*16000:(offset+45)*16000]
            segments,_=model.transcribe(sample,language=language,beam_size=3,word_timestamps=True,condition_on_previous_text=False,vad_filter=False)
            for part in segments:
                words.extend(numeric_words(part,offset))
            if offset>45:words.append((offset-1,offset+1,False))
        evidence={'version':2,'words':words,'beats':beats,'bpm':bpm,'duration':len(y)/16000}
        return {**selection(evidence),'evidence':evidence}
    finally:
        raw.unlink(missing_ok=True);raw.with_suffix('.rhythm.json').unlink(missing_ok=True)


def process(settings,db,row,ffmpeg):
    from .disk import Disk
    asset=db.execute('SELECT * FROM web_assets WHERE md5=? ORDER BY id LIMIT 1',(row['md5'],)).fetchone()
    if not asset or asset['bytes']>settings.max_bytes or not 30<=asset['duration']<=600:
        complete(db,row['md5'],{'cues':[]},max(0,asset['duration'] if asset else 0));return
    if asset['duration']<70:
        complete(db,row['md5'],{'cues':[],'reason':'too-short'},asset['duration']);return
    cached=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(row['md5'],)).fetchone()
    if cached:
        evidence=json.loads(cached[0]);complete(db,row['md5'],selection(evidence),asset['duration']);return
    if shutil.disk_usage(settings.state).free<768*1024**2:raise RuntimeError('LOW_DISK')
    with tempfile.TemporaryDirectory(prefix='dj-',dir=settings.state/'work') as scratch:
        file=Path(scratch)/('source'+Path(asset['remote_path']).suffix)
        Disk(settings).download(asset['remote_path'],file,expected_md5=row['md5'])
        # The child exits after each song, bounding native/JIT/model allocator growth.
        output=Path(scratch)/'cues.json'
        cmd=[os.sys.executable,'-m','aidj.dj_worker','analyse','--file',str(file),'--output',str(output),'--ffmpeg',ffmpeg]
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
            raise
        if output.stat().st_size>256*1024:raise RuntimeError('METADATA_TOO_LARGE')
        value=json.loads(output.read_text());evidence=value.pop('evidence',None)
        if evidence:
            db.execute('INSERT OR REPLACE INTO dj_evidence VALUES (?,?,?)',(row['md5'],json.dumps(evidence),time.time()));db.commit()
        complete(db,row['md5'],value,asset['duration'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['serve','once','analyse','rhythm']);parser.add_argument('--file',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--ffmpeg',required=True);parser.add_argument('--language');args=parser.parse_args()
    settings=Settings();settings.prepare()
    def terminate(signum,frame):raise SystemExit(0)
    signal.signal(signal.SIGTERM,terminate)
    if args.action=='rhythm':
        import numpy as np
        from .config import atomic_json
        if args.file.stat().st_size>16000*250*4+4096:raise RuntimeError('DECODE_TOO_LARGE')
        beats,bpm=rhythm(np.fromfile(args.file,dtype=np.float32),16000)
        atomic_json(args.output,{'beats':beats,'bpm':bpm});return
    if args.action=='analyse':
        from .config import atomic_json
        atomic_json(args.output,analyse(args.file,args.ffmpeg,settings.state/'dj-models',args.language));return
    with (settings.state/'dj-worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);db=connect(settings)
        db.execute("UPDATE dj_analysis SET status='pending' WHERE status='working'");db.commit();upgrade_queue(db)
        try:
            while True:
                row=next_job(db)
                if row:
                    heartbeat(db,'working')
                    db.execute("UPDATE dj_analysis SET status='working',updated=? WHERE md5=?",(time.time(),row['md5']));db.commit()
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
                else:heartbeat(db,'idle');time.sleep(10)
        finally:
            db.rollback();db.execute("UPDATE dj_analysis SET status='pending' WHERE status='working'");heartbeat(db,'stopped')


if __name__=='__main__':
    try:main()
    except MemoryError:
        print('DJ_RESOURCE_LIMIT_STOPPED',flush=True);raise SystemExit(137)
