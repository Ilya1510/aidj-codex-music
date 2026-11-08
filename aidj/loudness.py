"""Content-addressed original levels; decoding runs only in the serial DJ worker."""
from collections import deque
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from .disk import Disk,md5

METHOD='ebu-r128-v1'
MAX_BYTES=40*1024**2
MAX_DURATION=1800
MIN_FREE=768*1024**2
TIMEOUT=120


def validate(value):
    if not isinstance(value,dict) or value.get('method')!=METHOD:raise ValueError('LOUDNESS_METHOD')
    for name,lo,hi in [('integratedLufs',-70,0),('truePeakDbtp',-60,6)]:
        number=value.get(name)
        if type(number) not in (int,float) or not math.isfinite(number) or not lo<=number<=hi:
            raise ValueError('LOUDNESS_LEVEL')
    return {key:value[key] for key in ('integratedLufs','truePeakDbtp','method')}


def save(db,digest,value):
    value=validate(value)
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{32}',digest):raise ValueError('LOUDNESS_MD5')
    db.execute('''INSERT INTO audio_loudness(md5,integrated_lufs,true_peak_dbtp,method,status,attempts,retry_after,updated)
        VALUES (?,?,?,?,'complete',0,0,?) ON CONFLICT(md5) DO UPDATE SET
        integrated_lufs=excluded.integrated_lufs,true_peak_dbtp=excluded.true_peak_dbtp,method=excluded.method,
        status='complete',retry_after=0,updated=excluded.updated''',
        (digest,value['integratedLufs'],value['truePeakDbtp'],METHOD,time.time()))
    db.commit()


def metadata(db):
    levels={}
    for row in db.execute("SELECT * FROM audio_loudness WHERE status='complete'"):
        try:levels[row['md5']]=validate({'integratedLufs':row['integrated_lufs'],'truePeakDbtp':row['true_peak_dbtp'],'method':row['method']})
        except ValueError:continue
    return levels


def parse(output):
    """Read input measurements, never the normalized output fields."""
    objects=re.findall(r'\{[^{}]*\}',output[-8192:])
    for raw in reversed(objects):
        try:
            value=json.loads(raw)
            if any(type(value.get(key)) is bool for key in ('input_i','input_tp')):continue
            return validate({'integratedLufs':float(value['input_i']),'truePeakDbtp':float(value['input_tp']),'method':METHOD})
        except (ValueError,KeyError,TypeError):continue
    raise ValueError('LOUDNESS_OUTPUT')


def measure(file,ffmpeg,duration):
    if type(duration) not in (int,float) or not math.isfinite(duration) or not 0<duration<=MAX_DURATION:
        raise ValueError('LOUDNESS_DURATION')
    if not 0<Path(file).stat().st_size<=MAX_BYTES:raise ValueError('LOUDNESS_SIZE')
    args=[ffmpeg,'-nostdin','-hide_banner','-nostats','-v','info','-threads','1','-filter_threads','1',
          '-i',str(file),'-map','0:a:0','-t',str(MAX_DURATION+.1),'-vn','-sn','-dn',
          '-af','loudnorm=I=-16:TP=-1:LRA=11:print_format=json','-f','null','-']
    # Drain stderr continuously, retaining only an 8 KiB tail. Even malformed
    # files cannot make communicate() collect an unbounded decoder log in RAM.
    tail=deque(maxlen=8)
    child=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True)
    def drain():
        while chunk:=child.stderr.read(1024):tail.append(chunk)
    reader=threading.Thread(target=drain,daemon=True);reader.start()
    try:
        if child.wait(timeout=TIMEOUT)!=0:raise RuntimeError('LOUDNESS_DECODE')
    finally:
        if child.poll() is None:os.killpg(child.pid,signal.SIGKILL);child.wait()
        reader.join();child.stderr.close()
    return parse(b''.join(tail).decode('utf-8',errors='replace'))


def _claim(db,digest,now):
    row=db.execute('SELECT * FROM audio_loudness WHERE md5=?',(digest,)).fetchone()
    if row:
        if row['status']=='complete':return False
        if row['retry_after']>now:return False
    db.execute('''INSERT INTO audio_loudness(md5,method,status,attempts,retry_after,updated)
        VALUES (?,?,'working',1,?,?) ON CONFLICT(md5) DO UPDATE SET status='working',
        attempts=attempts+1,retry_after=excluded.retry_after,updated=excluded.updated''',(digest,METHOD,now+300,now))
    db.commit();return True


def _failed(db,digest):
    row=db.execute('SELECT attempts FROM audio_loudness WHERE md5=?',(digest,)).fetchone()
    now=time.time();delay=min(86400,300*2**min(9,max(0,row['attempts']-1)))
    db.execute("UPDATE audio_loudness SET status='failed',retry_after=?,updated=? WHERE md5=?",(now+delay,now,digest));db.commit()


def _eligible(settings,asset):
    return (type(asset['bytes']) in (int,float) and 0<asset['bytes']<=min(MAX_BYTES,settings.max_bytes)
            and type(asset['duration']) in (int,float) and math.isfinite(asset['duration'])
            and 0<asset['duration']<=min(MAX_DURATION,settings.max_duration)
            and bool(re.fullmatch('[a-f0-9]{32}',asset['md5'])))


def measure_original(settings,db,asset,file,ffmpeg):
    """Best effort on an already downloaded, MD5-checked DJ original."""
    if not _eligible(settings,asset) or not _claim(db,asset['md5'],time.time()):return False
    try:
        save(db,asset['md5'],measure(file,ffmpeg,asset['duration']))
    except Exception:
        _failed(db,asset['md5'])
    return True


def process_next(settings,db,ffmpeg):
    """One idle-worker probe. Caller holds the existing dj-worker.lock."""
    if shutil.disk_usage(settings.state).free<MIN_FREE:return False
    asset=db.execute('''SELECT a.* FROM web_assets a LEFT JOIN library_added n ON n.track_key=a.track_key
        LEFT JOIN audio_loudness l ON l.md5=a.md5
        WHERE a.bytes>0 AND a.bytes<=? AND a.duration>0 AND a.duration<=?
        AND length(a.md5)=32 AND a.md5 NOT GLOB '*[^a-f0-9]*'
        AND (l.md5 IS NULL OR (l.status!='complete' AND l.retry_after<=?))
        ORDER BY COALESCE(n.added,0) DESC,a.rowid DESC LIMIT 1''',
        (min(MAX_BYTES,settings.max_bytes),min(MAX_DURATION,settings.max_duration),time.time())).fetchone()
    if not asset or not _eligible(settings,asset) or not _claim(db,asset['md5'],time.time()):return False
    try:
        with tempfile.TemporaryDirectory(prefix='loudness-',dir=settings.state/'work') as scratch:
            file=Path(scratch)/'original'
            Disk(replace(settings,max_bytes=min(MAX_BYTES,settings.max_bytes))).download(asset['remote_path'],file,expected_md5=asset['md5'])
            if file.stat().st_size!=asset['bytes'] or md5(file)!=asset['md5']:raise ValueError('LOUDNESS_CONTENT')
            save(db,asset['md5'],measure(file,ffmpeg,asset['duration']))
    except Exception:
        # Neither signed download URLs nor decoder text enter durable metadata.
        _failed(db,asset['md5'])
    return True
