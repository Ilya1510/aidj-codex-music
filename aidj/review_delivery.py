"""Durable suggestion copies, native downloads and author acceptance notices."""
import json
from pathlib import Path
import tempfile
import time

from .config import Settings
from .disk import Disk,DiskError
from .locking import worker_lock
from .playlists import Playlists,PlaylistError,track_key
from .store import connect,job_update,meta_get,meta_set
from .telegram import Telegram,TelegramError
from .web_catalog import sync_assets


def suggestions_playlist(settings,db,disk=None):
    pid=meta_get(db,'review_suggestions_playlist')
    row=db.execute('SELECT * FROM playlists WHERE id=?',(pid,)).fetchone() if pid else None
    if row:return row
    row=Playlists(settings,db,disk).create('Предложка')
    meta_set(db,'review_suggestions_playlist',row['id']);return row


def schedule_saves(db,limit=3):
    """Confirmations survive full queues, retries and process restarts."""
    uid=meta_get(db,'owner_id');chat=meta_get(db,'owner_chat_id')
    if not uid or not chat:return 0
    now=time.time();queued=0
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute("""INSERT OR IGNORE INTO review_saves(item_id,updated)
            SELECT DISTINCT item_id,? FROM review_proposals WHERE status='confirmed'""",(now,))
        slots=max(0,10-db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0])
        rows=db.execute("""SELECT s.* FROM review_saves s WHERE s.state!='saved' AND s.retry_at<=?
            AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.status IN ('queued','processing')
                AND json_extract(j.payload,'$.client')='review' AND json_extract(j.payload,'$.action')='save'
                AND json_extract(j.payload,'$.item_id')=s.item_id)
            ORDER BY s.updated LIMIT ?""",(now,min(limit,slots))).fetchall()
        for row in rows:
            attempt=row['attempts']+1
            payload=json.dumps({'client':'review','action':'save','item_id':row['item_id']})
            db.execute('INSERT INTO jobs(update_id,chat_id,user_id,message_id,query,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                (f'review-save:{row["item_id"]}:{attempt}',int(chat),int(uid),0,'Сохранить предложение',payload,now,now))
            db.execute("UPDATE review_saves SET state='queued',attempts=?,retry_at=?,updated=? WHERE item_id=?",(attempt,now+60,now,row['item_id']))
            queued+=1
        db.commit();return queued
    except BaseException:db.rollback();raise


def process_save(settings,db,row,disk=None):
    """Owner-scoped job saves the suggestion without requesting a phone download."""
    from . import reviews as R
    item=json.loads(row['payload']).get('item_id');job_id=row['id']
    try:
        if not R.is_owner(db,row['user_id'],row['chat_id']):raise R.ReviewError('Сохранение доступно только владельцу.')
        save=db.execute('SELECT * FROM review_saves WHERE item_id=?',(item,)).fetchone()
        if not save:raise R.ReviewError('Подтверждённое предложение не найдено.')
        with worker_lock(settings),tempfile.TemporaryDirectory(prefix=f'suggestion-{job_id}-',dir=settings.state/'work') as folder:
            if save['state']!='saved':
                result=R.item_result(db,item);manager=Playlists(settings,db,disk)
                playlist=suggestions_playlist(settings,db,disk)
                manager.add(playlist['id'],result,Path(folder))
                sync_assets(db)
                db.execute("UPDATE review_saves SET state='saved',error=NULL,updated=? WHERE item_id=?",(time.time(),item));db.commit()
            job_update(db,job_id,status='completed',stage='Сохранено в Предложке')
        return 0
    except Exception as exc:
        message=str(exc) if isinstance(exc,(R.ReviewError,DiskError,PlaylistError)) else 'Не удалось сохранить предложение. Повтор запланирован.'
        # Forged or stale jobs cannot mutate a real save's retry state.
        if R.is_owner(db,row['user_id'],row['chat_id']):
            attempts=db.execute('SELECT attempts FROM review_saves WHERE item_id=?',(item,)).fetchone()
            delay=min(1800,60*2**min(5,max(0,(attempts[0] if attempts else 1)-1)))
            db.execute("UPDATE review_saves SET state='failed',error=?,retry_at=?,updated=? WHERE item_id=? AND state!='saved'",(message,time.time()+delay,time.time(),item));db.commit()
        job_update(db,job_id,status='failed',stage='Повтор сохранения запланирован',error=message)
        print('REVIEW_SAVE_FAILED',job_id,type(exc).__name__,flush=True);return 1


def accepted(db,playlist_id,result):
    """Only a verified addition to the shared playlist creates an author notice."""
    if str(playlist_id)!=meta_get(db,'review_shared_playlist'):return
    key=track_key(result);origin=result.get('origin_key',key)
    rows=db.execute("""SELECT p.user_id,p.chat_id,p.item_id FROM review_proposals p
        JOIN review_items i ON i.id=p.item_id WHERE p.status='confirmed'
        AND (i.track_key IN (?,?) OR json_extract(i.result,'$.md5')=?)""",(key,origin,result['md5'])).fetchall()
    label=result['track']['artist']+' — '+result['track']['title']
    for row in rows:
        db.execute('''INSERT OR IGNORE INTO review_notices
            (playlist_id,user_id,md5,chat_id,item_id,label,created) VALUES (?,?,?,?,?,?,?)''',
            (playlist_id,row['user_id'],result['md5'],row['chat_id'],row['item_id'],label,time.time()))
    db.commit()


def deliver_notices(db,bot,limit=3):
    rows=db.execute("SELECT * FROM review_notices WHERE state='pending' AND retry_at<=? ORDER BY created LIMIT ?",(time.time(),limit)).fetchall()
    sent=0
    for row in rows:
        playlist=db.execute('SELECT name FROM playlists WHERE id=?',(row['playlist_id'],)).fetchone()
        name=playlist[0] if playlist else 'Volleyball'
        text='Поздравляю! 🎉 Твоя песня «'+row['label'][:250]+'» была добавлена в общий плейлист «'+name+'».'
        where=(row['playlist_id'],row['user_id'],row['md5'])
        try:bot.message(row['chat_id'],text)
        except TelegramError as exc:
            state='blocked' if '(код 403)' in str(exc) else 'pending'
            retry=time.time()+min(3600,60*2**min(6,row['attempts']))
            db.execute('UPDATE review_notices SET state=?,attempts=attempts+1,retry_at=? WHERE playlist_id=? AND user_id=? AND md5=?',(state,retry,*where))
        else:
            db.execute("UPDATE review_notices SET state='sent',sent=? WHERE playlist_id=? AND user_id=? AND md5=?",(time.time(),*where));sent+=1
        db.commit()
    return sent


def maintenance(settings,stop):
    db=connect(settings)
    bot=Telegram(json.loads((settings.state/'telegram.json').read_text()))
    while not stop.is_set():
        try:
            schedule_saves(db)
            deliver_notices(db,bot)
        except Exception as exc:
            db.rollback();print('REVIEW_DELIVERY_RETRY',type(exc).__name__,flush=True)
        stop.wait(5)
