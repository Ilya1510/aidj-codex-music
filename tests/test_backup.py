from dataclasses import replace
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from aidj.backup import ROOT,META,BackupError,snapshot,upload,restore
from aidj.store import connect,meta_set
from tests.test_playlists import env
from tests.test_reviews import setup


def test_snapshot_copies_votes_and_playlists_but_never_auth_sessions_or_hidden_prompts(setup,tmp_path):
    settings,db,disk,bot,manager,result,p,*_=setup
    canary='BACKUP_TEST_SECRET_MUST_STAY_LOCAL'
    meta_set(db,'private_prompt',canary)
    db.execute('INSERT INTO native_transfer VALUES (1,?,9999999999)',(canary,))
    db.execute('INSERT INTO web_sessions VALUES (?,9999999999)',(canary,));db.commit()
    work=tmp_path/'copy';work.mkdir();file,counts=snapshot(settings,work)
    copy=sqlite3.connect(file)
    assert copy.execute('SELECT COUNT(*) FROM playlist_tracks').fetchone()[0]==1
    assert copy.execute('SELECT COUNT(*) FROM native_transfer').fetchone()[0]==0
    assert copy.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0]==0
    assert all(r[0] in META for r in copy.execute('SELECT key FROM meta'))
    copy.close();assert canary.encode() not in file.read_bytes()
    other=tmp_path/'copy-again';other.mkdir();second,_=snapshot(settings,other)
    assert file.read_bytes()==second.read_bytes()


def test_backup_rotates_two_latest_slots_and_restore_preserves_data(setup,tmp_path):
    settings,db,disk,bot,manager,result,p,*_=setup
    now=datetime.fromisoformat('2026-11-03T22:00:00+03:00')
    status,first=upload(settings,disk,now)
    assert status=='uploaded' and first['current']['file']=='state-a.sqlite'
    assert upload(settings,disk,now+timedelta(minutes=1))[0]=='unchanged'
    db.execute("UPDATE playlists SET name='Renamed' WHERE id=?",(p['id'],));db.commit()
    status,second=upload(settings,disk,now+timedelta(minutes=2))
    assert second['current']['file']=='state-b.sqlite'
    assert ROOT+'/state-a.sqlite' in disk.files
    destination=replace(settings,state=tmp_path/'restored')
    restore(destination,disk)
    copied=connect(destination)
    assert copied.execute('SELECT name FROM playlists').fetchone()[0]=='Renamed'
    assert copied.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0]==0
    with pytest.raises(BackupError):restore(destination,disk)


def test_daily_snapshot_is_retained_when_current_manifest_publish_fails(setup,tmp_path):
    settings,db,disk,bot,manager,result,p,*_=setup
    now=datetime.fromisoformat('2026-11-03T22:00:00+03:00')
    _,first=upload(settings,disk,now)
    old_manifest=disk.files[ROOT+'/latest.json']
    db.execute("UPDATE playlists SET name='Unpublished' WHERE id=?",(p['id'],));db.commit()
    original=disk.upload
    def fail(local,remote,overwrite=False):
        if remote.endswith('/latest.json'):raise RuntimeError('network failure')
        return original(local,remote,overwrite)
    disk.upload=fail
    with pytest.raises(RuntimeError):upload(settings,disk,now+timedelta(minutes=1))
    assert disk.files[ROOT+'/latest.json']==old_manifest
    destination=replace(settings,state=tmp_path/'previous');restore(destination,disk)
    assert connect(destination).execute('SELECT name FROM playlists').fetchone()[0]=='Volleyball'
    disk.upload=original
    _,value=upload(settings,disk,now+timedelta(days=1))
    destination=replace(settings,state=tmp_path/'daily');restore(destination,disk,day=now.weekday())
    assert connect(destination).execute('SELECT name FROM playlists').fetchone()[0]=='Volleyball'


def test_bad_snapshot_checksum_cannot_replace_database(setup,tmp_path):
    settings,db,disk,bot,manager,result,p,*_=setup
    _,manifest=upload(settings,disk)
    disk.files[ROOT+'/'+manifest['current']['file']]=b'damaged'
    destination=replace(settings,state=tmp_path/'restore-bad')
    with pytest.raises((BackupError,AssertionError)):restore(destination,disk)
    assert not (destination.state/'bot.sqlite').exists()
