"""Bounded, credential-free data snapshots in the owner's private Yandex Disk."""
import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo
import fcntl

from .config import Settings
from .disk import Disk,md5
from .store import connect

ROOT='/Музыка/_aidj_state'
MAX_BYTES=128*1024**2
TABLES=('playlists','playlist_tracks','playlist_aliases','playlist_selections','playlist_clicks',
        'cache','web_assets','web_operations','app_deliveries','app_delivery_acks','app_keyboard_updates',
        'library_added','audio_clips','playback_ranges','review_users','review_profiles','review_items',
        'review_cards','review_votes','review_proposals','review_saves','review_notices','review_removal_notices','review_web_ops')
META=('owner_id','owner_chat_id','review_shared_playlist','review_suggestions_playlist','update_offset')


class BackupError(RuntimeError):pass


def sha256(file):
    with Path(file).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def snapshot(settings,work):
    source=settings.state/'bot.sqlite'
    if not source.is_file():raise BackupError('База ещё не создана.')
    if source.stat().st_size>MAX_BYTES:raise BackupError('База больше лимита резервного копирования.')
    if shutil.disk_usage(settings.state).free<max(256*1024**2,source.stat().st_size*3):raise BackupError('Недостаточно места для снимка.')
    destination=replace(settings,state=Path(work)/'snapshot');out=connect(destination)
    src=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)
    counts={}
    try:
        src.execute('BEGIN')
        available={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES:
            if table=='review_removal_notices' and table not in available:
                counts[table]=0;continue
            cursor=src.execute('SELECT * FROM '+table)
            columns=[col[0] for col in cursor.description]
            statement='INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')'
            total=0
            while rows:=cursor.fetchmany(100):out.executemany(statement,rows);total+=len(rows)
            counts[table]=total
        placeholders=','.join('?' for _ in META)
        out.executemany('INSERT INTO meta VALUES (?,?)',src.execute('SELECT key,value FROM meta WHERE key IN ('+placeholders+')',META))
        out.commit();out.execute('PRAGMA wal_checkpoint(TRUNCATE)');out.execute('PRAGMA journal_mode=DELETE')
    finally:src.rollback();src.close();out.close()
    file=destination.state/'bot.sqlite';file.chmod(0o600)
    if file.stat().st_size>MAX_BYTES:raise BackupError('Снимок больше лимита резервного копирования.')
    return file,counts


def valid_entry(value):
    if not isinstance(value,dict):raise BackupError('Некорректный манифест резервной копии.')
    if not re.fullmatch(r'(?:state-[ab]|daily-[0-6])\.sqlite',str(value.get('file',''))):raise BackupError('Некорректный путь резервной копии.')
    if not re.fullmatch(r'[a-f0-9]{64}',str(value.get('sha256',''))):raise BackupError('Некорректная сумма резервной копии.')
    if not re.fullmatch(r'[a-f0-9]{32}',str(value.get('md5',''))):raise BackupError('Некорректная сумма резервной копии.')
    if type(value.get('bytes')) is not int or not 0<value['bytes']<=MAX_BYTES:raise BackupError('Некорректный размер резервной копии.')
    return value


def manifest(disk,work):
    path=ROOT+'/latest.json';metadata=disk.metadata(path)
    if not metadata:return None
    if metadata.get('size',0)>65536:raise BackupError('Слишком большой манифест.')
    local=Path(work)/'manifest.json';disk.download(path,local,metadata.get('md5'))
    value=json.loads(local.read_text())
    if value.get('format')!=1:raise BackupError('Неизвестный формат резервной копии.')
    valid_entry(value.get('current'));return value


def upload(settings,disk=None,now=None):
    settings.prepare();disk=disk or Disk(replace(settings,max_bytes=MAX_BYTES))
    now=now or datetime.now(ZoneInfo('Europe/Moscow'))
    with tempfile.TemporaryDirectory(prefix='state-backup-',dir=settings.state/'work') as work:
        file,counts=snapshot(settings,work);checksum=sha256(file);digest=md5(file);size=file.stat().st_size
        disk.mkdir(ROOT);old=manifest(disk,work)
        current=old['current'] if old else None
        history=dict(old.get('daily',{})) if old else {};day=str(now.weekday())
        same=bool(current and current['sha256']==checksum and disk.verify(ROOT+'/'+current['file'],digest,size))
        daily_due=history.get(day,{}).get('date')!=now.date().isoformat()
        if same and not daily_due:return 'unchanged',old
        if not same:
            name='state-b.sqlite' if current and current['file']=='state-a.sqlite' else 'state-a.sqlite'
            disk.upload(file,ROOT+'/'+name,overwrite=True)
            current={'file':name,'sha256':checksum,'md5':digest,'bytes':size,'created':now.isoformat()}
        if daily_due:
            name='daily-'+day+'.sqlite';disk.upload(file,ROOT+'/'+name,overwrite=True)
            history[day]={'file':name,'sha256':checksum,'md5':digest,'bytes':size,'date':now.date().isoformat()}
        value={'format':1,'current':current,'daily':history,'counts':counts,'generated':now.isoformat()}
        local=Path(work)/'latest.json';local.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        disk.upload(local,ROOT+'/latest.json',overwrite=True)
        return 'uploaded',value


def restore(settings,disk=None,day=None):
    settings.prepare();target=settings.state/'bot.sqlite'
    if target.exists() or target.with_name('bot.sqlite-wal').exists():raise BackupError('В папке уже есть база. Восстанавливай в пустую папку при остановленных службах.')
    disk=disk or Disk(replace(settings,max_bytes=MAX_BYTES))
    with tempfile.TemporaryDirectory(prefix='state-restore-',dir=settings.state/'work') as work:
        value=manifest(disk,work)
        if value is None:raise BackupError('Резервная копия на Диске не найдена.')
        entry=value['current'] if day is None else value.get('daily',{}).get(str(day))
        entry=valid_entry(entry);local=Path(work)/'restored.sqlite'
        disk.download(ROOT+'/'+entry['file'],local,entry['md5'])
        if local.stat().st_size!=entry['bytes'] or sha256(local)!=entry['sha256']:raise BackupError('Снимок повреждён. Попробуй дневную копию.')
        db=sqlite3.connect(local)
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise BackupError('База не прошла проверку целостности.')
            for table in ('web_sessions','web_login_codes','native_transfer','jobs','choices','review_choices'):
                if db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]:raise BackupError('В снимке есть временные сессии или задания.')
            if any(key not in META for key, in db.execute('SELECT key FROM meta')):raise BackupError('В снимке есть служебные секреты.')
        finally:db.close()
        local.chmod(0o600);local.replace(target)
        return entry


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['upload','restore'])
    parser.add_argument('--to',type=Path);parser.add_argument('--day',type=int,choices=range(7))
    args=parser.parse_args();settings=Settings()
    if args.to:settings=replace(settings,state=args.to.expanduser().resolve())
    settings.prepare()
    if args.action=='restore':
        restore(settings,day=args.day);print('STATE_RESTORED',flush=True);return 0
    with (settings.state/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print('BACKUP_DEFERRED_BUSY',flush=True);return 0
        status,_=upload(settings);print('STATE_BACKUP',status,flush=True);return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print('STATE_BACKUP_FAILED',type(error).__name__,flush=True);raise SystemExit(1)
