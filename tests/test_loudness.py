"""No decoder, network, or original audio is needed for these contracts."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace as N

import pytest

from aidj import loudness
from aidj.backup import snapshot
from aidj.config import Settings
from aidj.store import connect
from aidj.web_catalog import catalog


@pytest.fixture
def library(tmp_path):
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    yield settings,db
    db.close()


LEVELS={'integratedLufs':-26.1,'truePeakDbtp':-12.,'method':'ebu-r128-v1'}


def asset(db,key,body=b'original',added=0,**overrides):
    values={'id':key,'track_key':'test:'+key,'title':key,'artist':'Artist','duration':240,
            'md5':hashlib.md5(body).hexdigest(),'bytes':len(body),'remote_path':'/Music/'+key+'.mp3',**overrides}
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',tuple(values.values()))
    db.execute('INSERT INTO library_added VALUES (?,?)',(values['track_key'],added));db.commit()
    return db.execute('SELECT * FROM web_assets WHERE id=?',(key,)).fetchone()


def test_shared_md5_levels_are_top_level_and_leave_audio_and_manual_ranges_unchanged(library):
    settings,db=library
    first=asset(db,'one');asset(db,'alias')
    db.execute('INSERT INTO playback_ranges VALUES (?,?,?,?)',('one',12,200,0));db.commit()
    loudness.save(db,first['md5'],LEVELS)
    tracks=catalog(db)['tracks']
    assert len(tracks)==2 and all(t['loudness']==LEVELS for t in tracks)
    track=next(t for t in tracks if t['id']=='one')
    assert track['rangeStart']==12 and track['rangeEnd']==200
    assert track['duration']==240 and track['bytes']==len(b'original') and 'loudness' not in track.get('dj',{})


@pytest.mark.parametrize('key,value',[
    ('integratedLufs',True),('integratedLufs',float('nan')),('integratedLufs',float('inf')),
    ('integratedLufs',-70.1),('integratedLufs',.1),('integratedLufs','-26.1'),
    ('truePeakDbtp',False),('truePeakDbtp',-60.1),('truePeakDbtp',6.1),('method','unmeasured'),
])
def test_invalid_levels_rejected_before_storage(library,key,value):
    _,db=library
    with pytest.raises(ValueError):loudness.save(db,'a'*32,{**LEVELS,key:value})
    assert not loudness.metadata(db)


def test_invalid_or_incomplete_database_metadata_never_reaches_catalog(library):
    _,db=library
    for key,status,level,method in [('one','complete',1,loudness.METHOD),('two','working',-26,loudness.METHOD),('three','complete',-26,'unknown')]:
        row=asset(db,key,key.encode())
        db.execute('INSERT INTO audio_loudness VALUES (?,?,?,?,?,?,?,?)',(row['md5'],level,-12,method,status,1,0,0))
    db.commit()
    assert all('loudness' not in track for track in catalog(db)['tracks'])


@pytest.mark.parametrize('invalid',['-inf','nan','1','-71'])
def test_parser_rejects_silence_nonfinite_and_out_of_range_measurements(invalid):
    with pytest.raises(ValueError):loudness.parse(json.dumps({'input_i':invalid,'input_tp':'-12'}))


def test_decoder_is_bounded_and_reads_input_levels_from_only_small_stderr_tail(tmp_path,monkeypatch):
    file=tmp_path/'original';file.write_bytes(b'original');calls=[]
    payload=json.dumps({'input_i':'-26.10','input_tp':'-12.00','output_i':'-16.00','output_tp':'-1.00'}).encode()
    class Child:
        def __init__(self,args,**kwargs):
            calls.append((args,kwargs));self.stderr=io.BytesIO(b'x'*30000+b'\n'+payload);self.returncode=0
        def wait(self,timeout=None):assert timeout==120;return 0
        def poll(self):return 0
    monkeypatch.setattr(loudness.subprocess,'Popen',Child)
    assert loudness.measure(file,'/ffmpeg',240)==LEVELS
    args,kwargs=calls[0]
    assert args[args.index('-threads')+1]=='1' and args[args.index('-filter_threads')+1]=='1'
    assert args[args.index('-t')+1]=='1800.1' and args[-3:]==['-f','null','-']
    assert kwargs['stdout']==loudness.subprocess.DEVNULL and kwargs['start_new_session']
    assert file.read_bytes()==b'original'


def fake_io(monkeypatch,bodies):
    downloads=[];measured=[]
    class Disk:
        def __init__(self,settings):assert settings.max_bytes<=40*1024**2
        def download(self,remote,file,expected_md5):
            downloads.append((remote,Path(file)));Path(file).write_bytes(bodies[remote])
            assert hashlib.md5(bodies[remote]).hexdigest()==expected_md5
    monkeypatch.setattr(loudness,'Disk',Disk)
    monkeypatch.setattr(loudness.shutil,'disk_usage',lambda _:N(free=2*1024**3))
    def measure(file,*_):measured.append(Path(file));return dict(LEVELS)
    monkeypatch.setattr(loudness,'measure',measure)
    return downloads,measured


def test_idle_probe_recent_first_downloads_once_for_aliases_and_deletes_original(library,monkeypatch):
    settings,db=library
    old=asset(db,'old',b'older',added=1);recent=asset(db,'recent',added=5);asset(db,'alias',added=4)
    bodies={old['remote_path']:b'older',recent['remote_path']:b'original'}
    downloads,measured=fake_io(monkeypatch,bodies)
    assert loudness.process_next(settings,db,'ffmpeg')
    assert downloads[0][0]==recent['remote_path'] and measured==[downloads[0][1]]
    assert not measured[0].exists() and not measured[0].parent.exists()
    assert loudness.process_next(settings,db,'ffmpeg')
    assert len(downloads)==2 and not loudness.process_next(settings,db,'ffmpeg')
    assert all(t['loudness']==LEVELS for t in catalog(db)['tracks'])


def test_failure_backoff_stale_claim_recovery_and_retry_cap(library,monkeypatch):
    settings,db=library;row=asset(db,'one')
    fake_io(monkeypatch,{row['remote_path']:b'original'})
    now=[1000.];monkeypatch.setattr(loudness.time,'time',lambda:now[0])
    def fail(*_):raise RuntimeError('https://signed.invalid?token=SECRET')
    monkeypatch.setattr(loudness,'measure',fail)
    assert loudness.process_next(settings,db,'ffmpeg')
    stored=db.execute('SELECT * FROM audio_loudness').fetchone()
    assert stored['status']=='failed' and stored['attempts']==1 and stored['retry_after']==1300
    assert not loudness.process_next(settings,db,'ffmpeg')
    now[0]=1300;assert loudness.process_next(settings,db,'ffmpeg')
    assert db.execute('SELECT retry_after FROM audio_loudness').fetchone()[0]==1900
    db.execute("UPDATE audio_loudness SET status='working',attempts=20,retry_after=0");db.commit()
    assert loudness.process_next(settings,db,'ffmpeg')
    stored=db.execute('SELECT * FROM audio_loudness').fetchone()
    assert stored['retry_after']==now[0]+86400 and stored['status']=='failed'
    assert 'SECRET' not in str(tuple(stored)) and not loudness.metadata(db)


def test_dj_reuses_original_and_measurement_failure_is_nonfatal(library,tmp_path,monkeypatch):
    settings,db=library;row=asset(db,'one');file=tmp_path/'original';file.write_bytes(b'original')
    monkeypatch.setattr(loudness,'Disk',lambda *_:pytest.fail('must reuse the original'))
    def fail(*_):raise ValueError('broken decoder')
    monkeypatch.setattr(loudness,'measure',fail)
    assert loudness.measure_original(settings,db,row,file,'ffmpeg')
    assert file.exists() and db.execute('SELECT status FROM audio_loudness').fetchone()[0]=='failed'


def test_low_disk_and_settings_limits_defer_without_download_or_attempt(library,monkeypatch):
    settings,db=library;asset(db,'one')
    monkeypatch.setattr(loudness,'Disk',lambda *_:pytest.fail('must not download'))
    monkeypatch.setattr(loudness.shutil,'disk_usage',lambda _:N(free=loudness.MIN_FREE-1))
    assert not loudness.process_next(settings,db,'ffmpeg')
    monkeypatch.setattr(loudness.shutil,'disk_usage',lambda _:N(free=2*1024**3))
    assert not loudness.process_next(replace(settings,max_duration=120),db,'ffmpeg')
    assert not loudness.process_next(replace(settings,max_bytes=1),db,'ffmpeg')
    assert db.execute('SELECT COUNT(*) FROM audio_loudness').fetchone()[0]==0


def test_backup_preserves_measured_levels_and_accepts_old_database(library,tmp_path):
    settings,db=library;row=asset(db,'one');loudness.save(db,row['md5'],LEVELS)
    work=tmp_path/'backup';work.mkdir();file,counts=snapshot(settings,work)
    copy=sqlite3.connect(file)
    assert counts['audio_loudness']==1 and copy.execute('SELECT integrated_lufs,true_peak_dbtp FROM audio_loudness').fetchone()==(-26.1,-12.)
    copy.close();db.execute('DROP TABLE audio_loudness');db.commit()
    work=tmp_path/'old';work.mkdir();file,counts=snapshot(settings,work)
    assert counts['audio_loudness']==0
    copy=sqlite3.connect(file);assert copy.execute('SELECT COUNT(*) FROM audio_loudness').fetchone()[0]==0;copy.close()


@pytest.mark.parametrize('once',[True,False])
def test_real_batch_controller_idle_once_defers_and_serve_probes_one_then_rechecks_priority(library,monkeypatch,once):
    from aidj import dj_batch
    settings,db=library;stop=threading.Event();events=[]
    monkeypatch.setattr(dj_batch,'start',lambda *_:{'runId':'a'*24})
    monkeypatch.setattr(dj_batch,'recover_local',lambda *_:None)
    monkeypatch.setattr(dj_batch,'refine_completed',lambda *_:{'queuedForRefinement':0})
    monkeypatch.setattr(dj_batch.shutil,'disk_usage',lambda _:N(free=2*1024**3))
    def claim(*_):
        events.append('claim')
        if events.count('claim')==2:stop.set()
        return {'job':None}
    def probe(*_):events.append('probe');return True
    monkeypatch.setattr(dj_batch,'claim',claim)
    monkeypatch.setattr(loudness,'process_next',probe)
    dj_batch.serve(settings,'unused-python','unused-ffmpeg',once=once,stopped=stop,pause=lambda *_:None)
    assert events==(['claim'] if once else ['claim','probe','claim'])


def test_batch_downloaded_original_is_measured_before_asr_without_second_transfer(library,monkeypatch):
    from aidj import dj_batch
    from tests.test_dj_batch import seed,evidence
    settings,db=library;seed(db,1)
    run=dj_batch.start(db)['runId'];job=dj_batch.claim(db,run)['job'];events=[]
    class Disk:
        def __init__(self,*_):pass
        def download(self,remote,file,expected_md5):events.append('download');file.write_bytes(b'x'*123)
    def measure(settings,db,asset,file,ffmpeg):
        assert file.read_bytes()==b'x'*123 and asset['md5']==job['md5'];events.append('levels');return True
    def analysis(command,*_,**kwargs):
        events.append('asr');output=Path(command[command.index('--output')+1])
        output.write_text(json.dumps({'evidence':evidence()}));return {}
    monkeypatch.setattr(loudness,'measure_original',measure)
    result=dj_batch.local_job(settings,db,run,job,'unused-python','unused-ffmpeg',threading.Event(),analysis,Disk)
    assert result['state']=='done' and events==['download','levels','asr']
    assert not list((settings.state/'work').iterdir())
