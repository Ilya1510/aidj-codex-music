import argparse
import json
from pathlib import Path
import secrets
import tempfile
import time

from .config import Settings
from .locking import worker_lock
from .pipeline import Pipeline, SelectionNeeded
from .sources import SourceError
from .audio import AudioError
from .disk import DiskError
from .store import connect,job_update
from .telegram import Telegram,TelegramError
from .playlists import Playlists,PlaylistError
from .disk import web_url


def playlist_job(settings,db,bot,row,payload,work,notify):
    catalog=Playlists(settings,db)
    sid=payload.get('selection_id')
    selection=db.execute('SELECT * FROM playlist_selections WHERE id=?',(sid,)).fetchone() if sid else None
    if sid and (not selection or selection['chat_id']!=row['chat_id'] or selection['user_id']!=row['user_id']):
        raise PlaylistError('Этот выбор недоступен.')
    if payload['action'] in ('playlist_add_one','playlist_device'):
        if not selection:raise PlaylistError('Выбор песни не найден.')
        result=json.loads(selection['result'])
        if payload['action']=='playlist_add_one':catalog.add(payload['playlist_id'],result,work)
        else:
            from .device_inbox import publish
            publish(db,result)
        job_update(db,row['id'],status='completed',stage='Готово')
        try:catalog.refresh(bot,sid)
        except TelegramError:pass
        return
    if payload['action']=='playlist_create':
        playlist=catalog.create(payload['name'])
        if selection:
            db.execute('BEGIN IMMEDIATE')
            try:
                current=db.execute('SELECT * FROM playlist_selections WHERE id=?',(sid,)).fetchone()
                if current['state']=='open':
                    selected=json.loads(current['selected'])
                    if playlist['id'] not in selected and not catalog.contains(playlist['id'],json.loads(current['result'])):
                        selected.append(playlist['id'])
                    db.execute('UPDATE playlist_selections SET selected=? WHERE id=?',(json.dumps(selected),sid))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        playlists=[playlist]
        text='Плейлист «'+playlist['name']+'» готов.'
    else:
        if not selection:
            raise PlaylistError('Выбор песни не найден.')
        selected=json.loads(selection['selected'])
        if not selected:
            raise PlaylistError('Не выбрано ни одного плейлиста.')
        result=json.loads(selection['result'])
        playlists=[]
        for playlist_id in selected:
            notify('Добавляю в плейлист…')
            playlists.append(catalog.add(playlist_id,result,work))
        db.execute("UPDATE playlist_selections SET selected='[]',state='open' WHERE id=?",(sid,));db.commit()
        text='Добавлено: '+', '.join(p['name'] for p in playlists)
    job_update(db,row['id'],status='completed',stage='Готово',
               result=json.dumps({'playlists':[p['name'] for p in playlists]},ensure_ascii=False))
    try:
        bot.edit(row['chat_id'],row['message_id'],text,[[{'text':p['name'],'url':web_url(p['path'])}] for p in playlists])
        if selection:
            catalog.refresh(bot,sid)
    except TelegramError:
        pass


def run(job_id):
    settings=Settings();db=connect(settings)
    row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row or row['status']!='processing':
        return 1
    if json.loads(row['payload']).get('client')=='app':
        from .discover import process
        return process(settings,db,row)
    bot=Telegram(json.loads((settings.state/'telegram.json').read_text()))
    if json.loads(row['payload']).get('client')=='dj-review':
        from .dj_review_ui import process
        return process(settings,db,row,bot)
    if json.loads(row['payload']).get('client')=='review':
        from .review_worker import process
        return process(settings,db,row,bot)
    def notify(text):
        job_update(db,job_id,stage=text)
        if not row['message_id']:return
        try:
            bot.edit(row['chat_id'],row['message_id'],text)
        except TelegramError:
            pass
    try:
        notify('В очереди обработки…')
        with worker_lock(settings):
            with tempfile.TemporaryDirectory(prefix=f'job-{job_id}-',dir=settings.state/'work') as work:
                payload=json.loads(row['payload'])
                if payload.get('action') in ('playlist_add','playlist_create','playlist_add_one','playlist_device'):
                    playlist_job(settings,db,bot,row,payload,Path(work),notify)
                    return 0
                if payload.get('action')=='clip':
                    from .clips import create
                    from .disk import Disk
                    selection=db.execute('SELECT * FROM playlist_selections WHERE id=?',(payload['selection_id'],)).fetchone()
                    if not selection or selection['chat_id']!=row['chat_id'] or selection['user_id']!=row['user_id']:raise PlaylistError('Выбор песни недоступен.')
                    result=create(settings,db,json.loads(selection['result']),payload['start'],payload['end'],Path(work),Disk(settings))
                else:result=Pipeline(settings,db).run(row['query'],payload,Path(work),notify)
                catalog=Playlists(settings,db)
                sid=catalog.selection(row['chat_id'],row['user_id'],result)
                keyboard=catalog.keyboard(sid)
                try:
                    sent=bot.audio(row['chat_id'],result,keyboard)
                    file_id=sent.get('audio',{}).get('file_id')
                    if file_id and result.get('cache_key'):
                        db.execute('UPDATE cache SET telegram_file_id=? WHERE key=?',(file_id,result['cache_key']))
                        db.commit()
                except TelegramError:
                    sent=bot.message(row['chat_id'],'Песня сохранена на Диск. Отправить аудио в Telegram сейчас не удалось.',keyboard)
                db.execute('UPDATE playlist_selections SET message_id=? WHERE id=?',(sent['message_id'],sid));db.commit()
                result.pop('local_path',None)
                job_update(db,job_id,status='completed',stage='Готово',result=json.dumps(result,ensure_ascii=False))
                try:
                    bot.edit(row['chat_id'],row['message_id'],'Готово: '+result['track']['artist']+' — '+result['track']['title'],
                             [[{'text':'Файл на Диске','url':result['disk_url']}]])
                except TelegramError:
                    pass
        return 0
    except SelectionNeeded as selection:
        choice_id=secrets.token_hex(8)
        tracks=[t.to_dict() for t in selection.tracks]
        db.execute('INSERT INTO choices(id,chat_id,user_id,query,tracks,created) VALUES (?,?,?,?,?,?)',
                   (choice_id,row['chat_id'],row['user_id'],selection.query,json.dumps(tracks,ensure_ascii=False),time.time()))
        db.commit()
        keyboard=[[{'text':t.label[:60],'callback_data':f'pick:{choice_id}:{i}'}] for i,t in enumerate(selection.tracks)]
        if not selection.youtube:
            keyboard.append([{'text':'Поискать в YouTube Music','callback_data':'yt:'+choice_id}])
        bot.edit(row['chat_id'],row['message_id'],'Нашёл несколько записей. Выбери нужную:',keyboard)
        job_update(db,job_id,status='awaiting_choice',stage='Выбери запись')
        return 0
    except Exception as exc:
        payload=json.loads(row['payload'])
        if payload.get('action')=='playlist_add':
            db.execute("UPDATE playlist_selections SET state='open' WHERE id=?",(payload['selection_id'],));db.commit()
        message=str(exc) if isinstance(exc,(SourceError,AudioError,DiskError,PlaylistError)) else 'Не удалось обработать запрос. Повтори позже.'
        job_update(db,job_id,status='failed',error=message,stage='Ошибка')
        if payload.get('action') in ('playlist_add_one','playlist_device'):
            try:Playlists(settings,db).refresh(bot,payload['selection_id'])
            except TelegramError:pass
            print('JOB_FAILED',job_id,type(exc).__name__,flush=True);return 1
        try:
            bot.edit(row['chat_id'],row['message_id'],message)
        except TelegramError:
            pass
        print('JOB_FAILED',job_id,type(exc).__name__,flush=True)
        return 1


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('job_id',type=int)
    try:
        raise SystemExit(run(parser.parse_args().job_id))
    except Exception as exc:
        print('WORKER_STOPPED',type(exc).__name__,flush=True)
        raise SystemExit(1)
