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
from .dj import complete


def vocal_cues(words,beats,bpm,duration):
    """Require a bounded gap between recognised phrases, not missing ASR output.

    Fades stay after the preceding word and before the following word. This is
    conservative evidence, not a guarantee: singing can confuse ASR too.
    """
    if len(words)<12 or not 60<=bpm<=210:return []
    words=sorted((w[0],w[1],w[2] if len(w)>2 else True) for w in words)
    gaps=[];end=words[0][1];confident=words[0][2]
    for start,stop,next_confident in words[1:]:
        if confident and next_confident and 1.6<=start-end<=5:gaps.append((end+.2,start-.2))
        if stop>=end:end=stop;confident=next_confident
    starts=[]
    for lo,hi in gaps:
        if not 8<=lo<=55:continue
        grid=[t for t in beats if lo<=t<=hi]
        if grid:starts.append((grid[0],lo,hi))
    # The original beginning is always a valid entry if no vocal gap was found.
    start=min(starts,key=lambda item:abs(item[0]-24))[0] if starts else 0
    cues=[]
    for target in (80,110,140):
        choices=[]
        for lo,hi in gaps:
            if hi>duration or not 45<=hi-start<=190 or abs((hi-start)-target)>28:continue
            # Prefer a beat-aligned end, but leave the whole vocal finish intact.
            candidates=[t for t in beats if lo+1.2<=t<=hi]
            if not candidates and hi-lo>=1.2:candidates=[hi]
            if not candidates:continue
            stop=min(candidates,key=lambda t:abs((t-start)-target))
            fade=min(2,stop-lo)
            if fade<1.2:continue
            choices.append((abs(stop-start-target),{'start':round(start,4),'end':round(stop,4),'bpm':round(bpm,4),'fade':round(fade,4),'grid':round(beats[0]%(60/bpm),6)}))
        if choices:
            cue=min(choices,key=lambda c:c[0])[1]
            if cue not in cues:cues.append(cue)
    return cues


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


def analyse(file,ffmpeg,models,language=None):
    import numpy as np
    # Bounded to 250 seconds: all automatic cuts end before 245 seconds.
    raw=file.with_suffix('.f32')
    try:
        result=subprocess.run([ffmpeg,'-nostdin','-v','error','-threads','1','-i',str(file),'-t','250','-vn','-ac','1','-ar','16000','-f','f32le','-y',str(raw)],capture_output=True,timeout=60)
        if result.returncode:raise RuntimeError('DECODE_FAILED')
        if raw.stat().st_size>16000*250*4+4096:raise RuntimeError('DECODE_TOO_LARGE')
        timing=raw.with_suffix('.rhythm.json')
        # Separate phases: librosa/Numba exits before the speech model is loaded.
        try:subprocess.run([os.sys.executable,'-m','aidj.dj_worker','rhythm','--file',str(raw),'--output',str(timing),'--ffmpeg',ffmpeg],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        except subprocess.CalledProcessError as exc:
            if exc.returncode in (-9,137):raise MemoryError('RHYTHM_RESOURCE_LIMIT') from None
            raise
        grid=json.loads(timing.read_text());timing.unlink();beats,bpm=grid['beats'],grid['bpm']
        if not bpm:return {'cues':[],'method':'analysed'}
        y=np.fromfile(raw,dtype=np.float32)
        from faster_whisper import WhisperModel
        model=WhisperModel('base',device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,download_root=str(models))
        # A NumPy input also avoids a PyAV decoder compatibility issue.
        words=[]
        # Singing is transcribed more reliably with short independent windows than
        # a long music recording. Overlaps are occupied to prevent invented gaps.
        for offset in range(0,min(200,int(len(y)/16000)),40):
            sample=y[offset*16000:(offset+40)*16000]
            segments,_=model.transcribe(sample,language=language,beam_size=3,word_timestamps=True,condition_on_previous_text=False,vad_filter=False)
            for part in segments:
                confident=part.no_speech_prob<=.8 and part.avg_logprob>=-.85
                if not part.words:
                    words.append((offset+part.start,offset+part.end,False));continue
                for word in part.words:
                    # Low-confidence words occupy time too; never drop uncertain vocals.
                    if word.end>word.start:words.append((offset+word.start,offset+word.end,confident and word.probability>=.45))
            if offset>0:words.append((offset-1,offset+1,False))
        return {'cues':vocal_cues(words,beats,bpm,len(y)/16000),'method':'analysed'}
    finally:
        raw.unlink(missing_ok=True);raw.with_suffix('.rhythm.json').unlink(missing_ok=True)


def process(settings,db,row,ffmpeg):
    from .disk import Disk
    asset=db.execute('SELECT * FROM web_assets WHERE md5=? ORDER BY id LIMIT 1',(row['md5'],)).fetchone()
    if not asset or asset['bytes']>settings.max_bytes or not 30<=asset['duration']<=600:
        complete(db,row['md5'],{'cues':[]},max(0,asset['duration'] if asset else 0));return
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
            code=child.wait(timeout=240)
            if code in (-9,137):raise MemoryError('ANALYSIS_RESOURCE_LIMIT')
            if code!=0:raise RuntimeError('ANALYSIS_FAILED')
        except BaseException:
            if child.poll() is None:os.killpg(child.pid,signal.SIGKILL);child.wait()
            raise
        if output.stat().st_size>8192:raise RuntimeError('METADATA_TOO_LARGE')
        complete(db,row['md5'],json.loads(output.read_text()),asset['duration'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['serve','once','analyse','rhythm']);parser.add_argument('--file',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--ffmpeg',required=True);parser.add_argument('--language');args=parser.parse_args()
    settings=Settings();settings.prepare()
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
        db.execute("UPDATE dj_analysis SET status='pending' WHERE status='working'");db.commit()
        while True:
            row=db.execute("SELECT * FROM dj_analysis WHERE status='pending' ORDER BY updated LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE dj_analysis SET status='working' WHERE md5=?",(row['md5'],));db.commit()
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
            else:time.sleep(10)


if __name__=='__main__':
    try:main()
    except MemoryError:
        print('DJ_RESOURCE_LIMIT_STOPPED',flush=True);raise SystemExit(137)
