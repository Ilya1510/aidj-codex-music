"""Bounded worker jobs for public listening; all edits recheck the owner."""
from dataclasses import replace
import json
from pathlib import Path
import secrets
import tempfile
import time

from . import reviews as R
from .audio import AudioError
from .disk import Disk,DiskError
from .locking import worker_lock
from .pipeline import Pipeline,SelectionNeeded
from .playlists import Playlists,PlaylistError
from .review_ui import owner,vote_keyboard
from .sources import SourceError,parse_input
from .store import job_update
from .telegram import TelegramError
from .web_catalog import sync_assets,result_for,revision,mutate


def send_audio(settings,db,bot,chat,item_id,work,keyboard,disk=None,prepared=None):
    result=R.item_result(db,item_id)
    if prepared and prepared.get('md5')==result['md5'] and prepared.get('local_path'):
        result['local_path']=prepared['local_path']
    if result.get('telegram_file_id'):
        try:sent=bot.audio(chat,result,keyboard)
        except TelegramError as exc:
            # Only a rejected cached identifier is retried; blocked/network failures are not.
            if '(код 400)' not in str(exc):raise
            result['telegram_file_id']=None
        else:return sent
    if not result.get('local_path'):
        disk=disk or Disk(settings)
        suffix=Path(result['remote_path']).suffix
        local=work/('review-audio'+(suffix if suffix in ('.mp3','.m4a') else '.m4a'))
        disk.download(result['remote_path'],local,result['md5'])
        if local.stat().st_size!=result['bytes']:raise DiskError('Размер файла на Диске изменился.')
        result['local_path']=str(local)
    sent=bot.audio(chat,result,keyboard)
    file_id=sent.get('audio',{}).get('file_id')
    if file_id:
        db.execute('UPDATE review_items SET telegram_file_id=? WHERE id=?',(file_id,item_id))
        db.execute("UPDATE cache SET telegram_file_id=? WHERE json_extract(result,'$.md5')=?",(file_id,result['md5']));db.commit()
    return sent


def preview_keyboard(proposal_id,already=False):
    keyboard=[]
    if not already:keyboard.append([{'text':'Да, отправить на разметку','callback_data':'rv:confirm:'+proposal_id}])
    keyboard.extend([[{'text':'Предложить другую','callback_data':'rv:suggest'}],[{'text':'Начать размечать','callback_data':'rv:next'},{'text':'Меню','callback_data':'rv:menu'}]])
    return keyboard


def suggest(settings,db,bot,row,payload,work,notify,disk=None,sources=None):
    pid=payload['playlist_id'];sync_assets(db)
    if not db.execute('SELECT 1 FROM playlists WHERE id=?',(pid,)).fetchone():raise R.ReviewError('Плейлист больше не существует.')
    from .discover import existing_asset
    kind,value=parse_input(row['query']);asset=None
    if payload.get('track'):asset=existing_asset(db,payload['track'])
    elif kind in ('yandex','youtube'):asset=existing_asset(db,{'source':kind,'id':value})
    if asset:
        result=result_for(asset,db)
    else:
        private=replace(settings,disk_folder='/Музыка/_aidj_suggestions')
        pipeline=Pipeline(private,db,sources=sources,disk=disk,namespace='review:')
        chosen=pipeline.select(row['query'],payload)
        asset=existing_asset(db,chosen.to_dict())
        if asset:result=result_for(asset,db)
        else:result=pipeline.run(row['query'],{**payload,'track':chosen.to_dict()},work,notify)
    item=R.remember(db,result);proposal=R.proposal(db,row['user_id'],row['chat_id'],pid,item)
    found=R.matches(db,pid,result);exact=any(m['exact'] for m in found)
    name=db.execute('SELECT name FROM playlists WHERE id=?',(pid,)).fetchone()[0]
    if exact:
        text=f'Эта запись уже есть в плейлисте «{name}». Повторно добавлять её на разметку не нужно. Можно послушать ниже.'
    elif found:
        text=f'В плейлисте «{name}» есть похожие записи:\n'+'\n'.join('• '+m['label'][:180] for m in found)+f'\n\nЭто может быть другая версия. Послушай и проверь. Если нужна именно эта запись — отправь её на разметку кнопкой под аудио.'
    else:text='Послушай запись ниже. Если это нужная песня, нажми «Да, отправить на разметку». После подтверждения она сохранится в «Предложке» и будет ждать решения для общего плейлиста.'
    sent=send_audio(settings,db,bot,row['chat_id'],item,work,preview_keyboard(proposal['id'],exact),disk,result)
    bot.edit(row['chat_id'],row['message_id'],text)
    return {'proposal_id':proposal['id'],'item_id':item,'message_id':sent['message_id']}


def process(settings,db,row,bot,disk=None,sources=None):
    payload=json.loads(row['payload']);job_id=row['id'];card=None
    if payload.get('action')=='save':
        from .review_delivery import process_save
        return process_save(settings,db,row,disk)
    def notify(text):
        job_update(db,job_id,stage=text)
        if row['message_id']:
            try:bot.edit(row['chat_id'],row['message_id'],text)
            except TelegramError:pass
    try:
        user=db.execute('SELECT * FROM review_users WHERE user_id=? AND chat_id=?',(row['user_id'],row['chat_id'])).fetchone()
        if not user:raise R.ReviewError('Открой /review, чтобы начать.')
        with worker_lock(settings),tempfile.TemporaryDirectory(prefix=f'review-{job_id}-',dir=settings.state/'work') as folder:
            work=Path(folder);action=payload.get('action');answer={}
            if action=='listen':
                card=db.execute("SELECT * FROM review_cards WHERE id=? AND user_id=? AND chat_id=? AND state='queued'",(payload.get('card_id'),row['user_id'],row['chat_id'])).fetchone()
                if not card:raise R.ReviewError('Эта карточка уже закрыта.')
                if R.own_suggestion(db,card['playlist_id'],row['user_id'],card['item_id']):
                    db.execute("UPDATE review_cards SET state='skipped' WHERE id=?",(card['id'],));db.commit()
                    raise R.ReviewError('Это твоё предложение. Нажми «Следующая песня», чтобы оценить другую запись.')
                if card['item_id'] not in R.playlist_items(db,card['playlist_id']):raise R.ReviewError('Эту песню уже убрали из разметки. Нажми «Следующая песня».')
                sent=send_audio(settings,db,bot,row['chat_id'],card['item_id'],work,vote_keyboard(card['id']),disk)
                db.execute("UPDATE review_cards SET state='open',message_id=? WHERE id=?",(sent['message_id'],card['id']));db.commit()
                answer={'card_id':card['id']}
            elif action=='suggest':answer=suggest(settings,db,bot,row,payload,work,notify,disk,sources)
            elif action in ('inspect','remove'):
                if not owner(db,row['user_id'],row['chat_id']):raise R.ReviewError('Эта операция доступна владельцу.')
                pid=payload['playlist_id'];item=payload['item_id']
                if item not in R.playlist_items(db,pid):raise R.ReviewError('Песни больше нет в этой разметке.')
                result=R.item_result(db,item)
                key=db.execute('SELECT track_key FROM review_items WHERE id=?',(item,)).fetchone()[0]
                member=db.execute("SELECT 1 FROM playlist_tracks WHERE playlist_id=? AND track_key=? AND state='complete'",(pid,key)).fetchone()
                if action=='remove':
                    if not member:raise R.ReviewError('Песни уже нет в плейлисте.')
                    mutate(settings,db,disk or Disk(settings),{'opId':'review-remove-'+str(job_id),'action':'remove',
                        'playlistId':pid,'trackId':item,'revision':revision(db,pid)},work)
                    db.execute("UPDATE review_proposals SET status='archived' WHERE playlist_id=? AND item_id=?",(pid,item));db.commit()
                    bot.message(row['chat_id'],'Убрано из плейлиста: '+result['track']['artist']+' — '+result['track']['title'],
                        [[{'text':'Оценки','callback_data':'rv:stats:0'}]])
                else:
                    manager=Playlists(settings,db,disk);sid=manager.selection(row['chat_id'],row['user_id'],result)
                    keyboard=manager.keyboard(sid)
                    keyboard.append([{'text':'Кто оценил и предложил','callback_data':'rv:people:'+item+':0'}])
                    if member:keyboard.append([{'text':'Убрать из плейлиста','callback_data':'rv:remove:'+item}])
                    else:keyboard.append([{'text':'Снять предложение с разметки','callback_data':'rv:archive:'+item}])
                    sent=send_audio(settings,db,bot,row['chat_id'],item,work,keyboard,disk)
                    db.execute('UPDATE playlist_selections SET message_id=? WHERE id=?',(sent['message_id'],sid));db.commit()
            else:raise R.ReviewError('Неизвестный запрос разметки.')
            job_update(db,job_id,status='completed',stage='Готово',result=json.dumps(answer));return 0
    except SelectionNeeded as selection:
        choice=secrets.token_hex(8);tracks=[t.to_dict() for t in selection.tracks]
        db.execute('INSERT INTO review_choices VALUES (?,?,?,?,?,?,?,?,?)',(choice,row['user_id'],row['chat_id'],payload['playlist_id'],selection.query,json.dumps(tracks,ensure_ascii=False),int(selection.youtube),0,time.time()));db.commit()
        keyboard=[[{'text':t.label[:60],'callback_data':f'rv:pick:{choice}:{i}'}] for i,t in enumerate(selection.tracks)]
        if not selection.youtube:keyboard.append([{'text':'Искать в YouTube','callback_data':'rv:yt:'+choice}])
        bot.edit(row['chat_id'],row['message_id'],'Выбери нужную запись:',keyboard)
        job_update(db,job_id,status='awaiting_choice',stage='Выбери запись');return 0
    except Exception as exc:
        if payload.get('action')=='listen':
            db.execute("UPDATE review_cards SET state='failed' WHERE id=? AND state='queued'",(payload.get('card_id'),));db.commit()
        text=str(exc) if isinstance(exc,(R.ReviewError,SourceError,AudioError,DiskError,PlaylistError)) else 'Не получилось подготовить песню. Попробуй ещё раз.'
        job_update(db,job_id,status='failed',stage='Ошибка',error=text)
        try:
            keyboard=[[{'text':'Повторить','callback_data':'rv:next' if payload.get('action')=='listen' else 'rv:suggest'},{'text':'Меню','callback_data':'rv:menu'}]]
            if row['message_id']:bot.edit(row['chat_id'],row['message_id'],text,keyboard)
            else:bot.message(row['chat_id'],text,keyboard)
        except TelegramError:pass
        print('REVIEW_JOB_FAILED',job_id,type(exc).__name__,flush=True);return 1
