import hashlib
import json
import time
from types import SimpleNamespace as N

from aidj.config import Settings
from aidj.store import connect
from aidj.web_catalog import register_asset,sync_assets,asset_id
from aidj.dj_batch import claim
from tests.test_playlists import env


def result(identity='new',digest=None,duration=180):
    return {'track':{'source':'youtube','id':identity,'title':'Song '+identity,'artist':'Artist','url':'https://example.invalid/song'},
            'duration':duration,'md5':digest or hashlib.md5(identity.encode()).hexdigest(),'bytes':12,'remote_path':'/Music/'+identity+'.mp3'}


def jobs(db):return db.execute('SELECT * FROM dj_batch_jobs ORDER BY md5').fetchall()


def test_sync_enrols_normal_cache_clips_and_predlozhka_without_start_or_playing(env):
    _,db,*_=env
    normal=result('normal');clip=result('clip');clip['track']['source']='clip';suggestion=result('proposal')
    db.execute('INSERT INTO cache VALUES (?,?,NULL,?)',('normal:key',json.dumps(normal),0))
    db.execute('INSERT INTO cache VALUES (?,?,NULL,?)',('review:private-preview',json.dumps(result('private')),0))
    db.execute('INSERT INTO audio_clips VALUES (?,?,?)',('clip:clip',json.dumps(clip),0))
    db.execute('INSERT INTO playlists VALUES (1,?,?,?,0)',('Предложка','предложка','/Music/Предложка'))
    db.execute('INSERT INTO playlist_tracks VALUES (?,?,?,?,?,?,?,?,?)',(1,'youtube:proposal',1,suggestion['remote_path'],suggestion['md5'],12,180,'Artist — Proposal','complete'))
    db.commit();sync_assets(db)
    assert {row['md5'] for row in jobs(db)}=={normal['md5'],clip['md5'],suggestion['md5']}
    assert all(row['state']=='pending' and row['error']=='NEW_ASSET' for row in jobs(db))
    assert db.execute('SELECT count(*) FROM dj_analysis').fetchone()[0]==0
    assert not db.execute("SELECT 1 FROM web_assets WHERE track_key='youtube:private'").fetchone()


def test_enrol_is_atomic_deduplicates_aliases_and_does_not_reset_failed_or_running(env):
    _,db,*_=env;value=result('same')
    register_asset(db,value);db.rollback();assert not jobs(db)
    register_asset(db,value);db.commit();job=jobs(db)[0]
    db.execute("UPDATE dj_batch_jobs SET state='failed',error='RESOURCE_LIMIT',attempts=1");db.commit()
    register_asset(db,result('alias',value['md5']));db.commit()
    assert len(jobs(db))==1 and jobs(db)[0]['state']=='failed' and jobs(db)[0]['attempts']==1
    db.execute("UPDATE dj_batch_jobs SET state='running',token=?,lease=?",('a'*32,time.time()+300));db.commit()
    register_asset(db,result('another-alias',value['md5']));db.commit()
    assert jobs(db)[0]['token']=='a'*32 and jobs(db)[0]['state']=='running'
    newer=result('same',hashlib.md5(b'new bytes').hexdigest());register_asset(db,newer);db.commit()
    assert len(jobs(db))==2
    assert db.execute('SELECT token FROM dj_batch_jobs WHERE md5=?',(value['md5'],)).fetchone()[0]=='a'*32


def test_strong_cached_results_are_reused_and_short_or_long_valid_sources_are_eligible(env):
    _,db,*_=env;cached=result('cached')
    db.execute('INSERT INTO dj_evidence VALUES (?,?,0)',(cached['md5'],json.dumps({'version':3,'fullComplete':True,'boundaryPass':1})));db.commit()
    for value in (cached,result('short',duration=8),result('long',duration=1200),result('invalid-zero',duration=0),result('too-long',duration=1801),{**result('too-large'),'bytes':40*1024**2+1}):register_asset(db,value)
    db.commit();entries=jobs(db)
    assert len(entries)==3
    assert db.execute('SELECT state FROM dj_batch_jobs WHERE md5=?',(cached['md5'],)).fetchone()[0]=='done'


def test_current_audible_track_then_new_asset_precede_the_old_backfill(env):
    _,db,*_=env
    backfill=result('backfill');current=result('current');fresh=result('fresh')
    for value in (backfill,current):register_asset(db,value)
    db.execute('UPDATE dj_batch_jobs SET error=NULL,updated=0');db.commit()
    register_asset(db,fresh);db.commit()
    db.execute('INSERT INTO dj_priority VALUES (?,?,?)',(current['md5'],0,time.time()));db.commit()
    run=jobs(db)[0]['run_id']
    assert claim(db,run)['job']['md5']==current['md5']
    assert claim(db,run)['job']['md5']==fresh['md5']
    assert claim(db,run)['job']['md5']==backfill['md5']


def test_bot_playlist_add_enrols_before_a_catalog_refresh(env,tmp_path):
    _,db,disk,bot,manager,value=env;playlist=manager.create('Предложка')
    manager.add(playlist['id'],value,tmp_path)
    assert len(jobs(db))==1 and jobs(db)[0]['md5']==value['md5']
    asset=db.execute('SELECT remote_path FROM web_assets WHERE id=?',(asset_id('yandex:123'),)).fetchone()
    assert asset and '/Предложка/' in asset[0]


def test_normal_pipeline_enrols_immediately_but_private_review_preview_does_not(tmp_path,monkeypatch):
    from aidj.pipeline import Pipeline
    from aidj.sources import Track
    class Sources:
        def yandex_track(self,value):return Track('yandex',value,'Song','Artist','url',available=True)
        def download_yandex(self,track,work):
            path=work/'source.mp3';path.write_bytes(b'fixture audio');return path
    class Disk:
        def mkdir(self,path):pass
        def upload(self,local,remote):return hashlib.md5(local.read_bytes()).hexdigest()
    monkeypatch.setattr('aidj.pipeline.duration',lambda path:180)
    monkeypatch.setattr('aidj.pipeline.trim',lambda source,target:(source,N(to_dict=lambda:{})))
    for namespace,expected in [('',1),('review:',0)]:
        settings=Settings(state=tmp_path/('normal' if not namespace else 'private'));db=connect(settings)
        work=settings.state/'work';Pipeline(settings,db,Sources(),Disk(),namespace=namespace).run('https://music.yandex.ru/track/123',{},work)
        assert len(jobs(db))==expected
        assert db.execute('SELECT count(*) FROM web_assets').fetchone()[0]==expected


def test_owner_clip_enrols_at_creation_without_an_app_refresh(tmp_path,monkeypatch):
    from aidj.clips import create
    settings=Settings(state=tmp_path/'state');db=connect(settings);work=settings.state/'work'
    original=result('original');original['duration']=180
    class Disk:
        def download(self,remote,target,expected):target.write_bytes(b'source')
        def mkdir(self,path):pass
        def upload(self,local,remote):return hashlib.md5(local.read_bytes()).hexdigest()
    class Tags(dict):
        def save(self):pass
    def ffmpeg(command,**kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(b'encoded clip');return N(returncode=0)
    monkeypatch.setattr('aidj.clips.MP4',lambda target:Tags())
    monkeypatch.setattr('aidj.clips.subprocess.run',ffmpeg)
    monkeypatch.setattr('aidj.clips.ffmpeg',lambda:'ffmpeg')
    monkeypatch.setattr('aidj.clips.duration',lambda target:70)
    value=create(settings,db,original,10,80,work,Disk())
    assert len(jobs(db))==1 and jobs(db)[0]['md5']==value['md5']


def test_reconnect_migrates_a_previous_trigger_definition_without_reenqueuing_existing_jobs(tmp_path):
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    db.execute("UPDATE meta SET value='old' WHERE key='dj_asset_trigger_version'")
    db.execute('DROP TRIGGER dj_enrol_asset_insert');db.commit();db.close()
    db=connect(settings);register_asset(db,result('restored-short',duration=8));db.commit()
    assert len(jobs(db))==1


def test_backup_copy_never_enrols_operational_work_and_remains_byte_stable(env,tmp_path):
    import sqlite3
    from aidj.backup import snapshot
    settings,db,*_=env;register_asset(db,result('backup-source'));db.commit()
    assert len(jobs(db))==1
    first_dir=tmp_path/'first';first_dir.mkdir();first,_=snapshot(settings,first_dir)
    copied=sqlite3.connect(first)
    assert copied.execute('SELECT count(*) FROM dj_batch_jobs').fetchone()[0]==0
    assert copied.execute('SELECT count(*) FROM dj_batch_runs').fetchone()[0]==0
    assert copied.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]==0
    assert copied.execute("SELECT value FROM meta WHERE key='dj_asset_trigger_version'").fetchone() is None
    copied.close()
    second_dir=tmp_path/'second';second_dir.mkdir();second,_=snapshot(settings,second_dir)
    assert first.read_bytes()==second.read_bytes()
    # A restored/running connection reinstalls the invariant; snapshots alone do not.
    restored=connect(Settings(state=first.parent))
    register_asset(restored,result('after-restore'));restored.commit()
    assert len(jobs(restored))==1


def test_legacy_normal_cached_hit_is_indexed_immediately_but_private_cached_preview_stays_private(tmp_path):
    from aidj.pipeline import Pipeline,ALGORITHM
    from aidj.sources import Track
    for namespace,expected in [('',1),('review:',0)]:
        settings=Settings(state=tmp_path/('public-hit' if not namespace else 'private-hit'));db=connect(settings)
        value=result('legacy');key=namespace+ALGORITHM+':youtube:legacy'
        db.execute('INSERT INTO cache VALUES (?,?,NULL,0)',(key,json.dumps(value)));db.commit()
        assert not jobs(db)
        pipeline=Pipeline(settings,db,sources=object(),disk=N(verify=lambda *args:True),namespace=namespace)
        answer=pipeline.cached(Track('youtube','legacy','Song','Artist','url'),settings.state/'work',need_local=False)
        assert answer['cached'] and len(jobs(db))==expected
        assert db.execute('SELECT count(*) FROM web_assets').fetchone()[0]==expected


def test_legacy_cached_clip_without_asset_is_indexed_before_return(tmp_path):
    from aidj.clips import create
    settings=Settings(state=tmp_path/'state');db=connect(settings);original=result('original');original['duration']=180
    identity=hashlib.sha256(f"{original['md5']}:10.000:80.000:aac-v1".encode()).hexdigest()[:24]
    cached=result(identity,duration=70);cached['track']['source']='clip'
    db.execute('INSERT INTO audio_clips VALUES (?,?,0)',('clip:'+identity,json.dumps(cached)));db.commit()
    class Disk:
        def verify(self,*args):return True
        def download(self,remote,target,md5):target.write_bytes(b'fixture')
    assert not jobs(db)
    answer=create(settings,db,original,10,80,settings.state/'work',Disk())
    assert answer['md5']==cached['md5'] and len(jobs(db))==1
    assert db.execute('SELECT id FROM web_assets').fetchone()[0]==asset_id('clip:'+identity)
