import hmac
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import Settings,atomic_json
from .bootstrap import activate_pending,SetupError
from .store import connect,meta_get,meta_set,enqueue,claim_job,job_update
from .telegram import Telegram,TelegramError
from .playlists import Playlists,PlaylistError
from .playlist_ui import PlaylistUI
from .disk import web_url
from .webapp import public_url
from .android_release import version as android_version,connection_url
from .clip_ui import ClipUI
from .review_ui import ReviewUI
from .dj_review_ui import DjReviewUI
from .app_install import AppInstallUI

HELP=('Пришли исполнителя и название песни или ссылку на один трек.\n\n'
      'Сначала ищу в Яндекс Музыке, затем — в YouTube Music. '
      'Сохраняю песню на Яндекс Диск и присылаю аудио сюда. '
      'Под аудио нажми на плейлист или «Скачать в приложение» — одного нажатия достаточно.\n\n'
      '«Обрезать» — сохранить отдельный фрагмент по интервалу, например 0:00–1:30.\n\n'
      'Если нашёл несколько версий — предложу выбрать.\n'
      '/app — плеер\n/apk — плеер для друзей: Android / iPhone\n/playlists — мои плейлисты\n/newplaylist — создать плейлист\n/status — последние запросы.\n\n'
      '/review — слушать и оценивать песни\n/suggest — предложить песню\n'
      '/djreview — проверить DJ-точки\n'
      '/reviewstats — сводка оценок\n/suggestions — предложенные песни\n/reviewlink — ссылка для друзей.')


def owner_matches(db,chat_id,user_id):
    return meta_get(db,'owner_id')==str(user_id) and meta_get(db,'owner_chat_id')==str(chat_id)


def bind_owner(db,config,chat_id,user_id,text):
    if meta_get(db,'owner_id'):
        return False
    value=text.partition(' ')[2].strip() if text.startswith('/start ') else ''
    expected=config.get('pair_code') or ''
    if not expected or not hmac.compare_digest(value.encode(),expected.encode()):
        return False
    db.execute('BEGIN IMMEDIATE')
    if meta_get(db,'owner_id'):
        db.rollback()
        return False
    db.execute('INSERT INTO meta VALUES (?,?)',('owner_id',str(user_id)))
    db.execute('INSERT INTO meta VALUES (?,?)',('owner_chat_id',str(chat_id)))
    db.commit()
    return True


def queue(db,bot,update_id,chat_id,user_id,text,payload=None):
    if db.execute('SELECT 1 FROM jobs WHERE update_id=?',(str(update_id),)).fetchone():
        return
    if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]>=10:
        bot.message(chat_id,'В очереди уже десять запросов. Дождись обработки и пришли следующий.')
        return
    sent=bot.message(chat_id,'Принял. Ищу песню…')
    enqueue(db,update_id,chat_id,user_id,sent['message_id'],text,payload)


def handle_update(db,config,bot,update,settings):
    if 'callback_query' in update:
        callback=update['callback_query']
        message=callback.get('message') or {}
        chat=message.get('chat') or {}
        user=callback.get('from') or {}
        if AppInstallUI(db,bot,settings).callback(update):return
        if DjReviewUI(db,bot,settings).callback(update):return
        if meta_get(db,'owner_id') and ReviewUI(db,bot,settings).callback(update):return
        if chat.get('type')!='private' or not owner_matches(db,chat.get('id'),user.get('id')):
            bot.call('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Это личный бот.'})
            return
        db.execute("UPDATE review_users SET mode='menu' WHERE user_id=?",(user['id'],));db.commit()
        parts=callback.get('data','').split(':')
        try:
            if ClipUI(db,bot).callback(update):return
            if PlaylistUI(db,bot,settings).callback(update):
                return
        except PlaylistError as exc:
            bot.call('answerCallbackQuery',{'callback_query_id':callback['id'],'text':str(exc)})
            return
        if len(parts) not in (2,3) or parts[0] not in ('pick','yt'):
            return
        selection=db.execute('SELECT * FROM choices WHERE id=?',(parts[1],)).fetchone()
        if (not selection or selection['used'] or time.time()-selection['created']>3600 or
            selection['chat_id']!=chat['id'] or selection['user_id']!=user['id']):
            bot.call('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Этот выбор уже использован или устарел. Пришли запрос заново.'})
            return
        if parts[0]=='pick':
            try:
                index=int(parts[2]);tracks=json.loads(selection['tracks'])
                if index<0:raise IndexError()
                chosen=tracks[index]
            except (ValueError,IndexError):
                return
            payload={'track':chosen}
        else:
            payload={'youtube_only':True}
        # The poller is single-threaded; marking the choice used precedes enqueue.
        db.execute('UPDATE choices SET used=1 WHERE id=?',(selection['id'],));db.commit()
        bot.call('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Принял'})
        queue(db,bot,update['update_id'],chat['id'],user['id'],selection['query'],payload)
        return
    message=update.get('message') or {}
    chat=message.get('chat') or {}
    user=message.get('from') or {}
    if chat.get('type')!='private' or not user.get('id') or user.get('is_bot'):
        return
    text=(message.get('text') or '').strip()
    if bind_owner(db,config,chat['id'],user['id'],text):
        config.pop('pair_code',None)
        atomic_json(settings.state/'telegram.json',config)
        bot.message(chat['id'],'Подключён к твоему аккаунту.\n\n'+HELP)
        return
    if DjReviewUI(db,bot,settings).message(update):return
    if AppInstallUI(db,bot,settings).message(update,owner_matches(db,chat['id'],user['id'])):return
    if not owner_matches(db,chat['id'],user['id']):
        if meta_get(db,'owner_id'):
            ReviewUI(db,bot,settings).message(update,False)
            return
        if text.startswith('/start'):
            bot.message(chat['id'],'Это личный бот. Для первого подключения открой одноразовую ссылку из терминала VM.')
        return
    if ReviewUI(db,bot,settings).message(update,True):return
    command=text.split(' ',1)[0].split('@',1)[0]
    if command=='/start' and text.partition(' ')[2].strip()=='import':
        origin=public_url(settings)
        if origin:bot.message(chat['id'],'Перенос скачанных песен',[[{'text':'Перенести скачанное','web_app':{'url':origin+'/#import=1'}}]])
    elif command=='/android' or (command=='/start' and text.partition(' ')[2].strip()=='android'):
        origin=public_url(settings);info=android_version(settings,origin) if origin else None
        if info:
            bot.message(chat['id'],'AI DJ для Android',[[{'text':'Скачать APK '+info['versionName'],'url':info['url']}],
                [{'text':'Подключить приложение','url':connection_url(db,origin)}]])
        else:
            bot.message(chat['id'],'Android-приложение ещё собирается.')
    elif command in ('/start','/help'):
        bot.message(chat['id'],HELP,[[{'text':'Разметка и предложения','callback_data':'rv:menu'}],
                                   [{'text':'Проверить DJ-точки','callback_data':'djrv:start'}]])
    elif command=='/app':
        url=public_url(settings)
        if url:
            bot.message(chat['id'],'Моя музыка',[[{'text':'Открыть плеер','web_app':{'url':url}}]])
        else:
            bot.message(chat['id'],'Плеер ещё запускается. Попробуй чуть позже.')
    elif command=='/newplaylist':
        ui=PlaylistUI(db,bot,settings)
        name=text.partition(' ')[2].strip()
        if name:
            ui.create(update['update_id'],chat['id'],user['id'],name)
        else:
            ui.prompt(chat['id'],user['id'])
    elif command=='/cancel':
        meta_set(db,'playlist_name_prompt','')
        meta_set(db,'clip_prompt','')
        bot.message(chat['id'],'Отменено.')
    elif command=='/playlists':
        playlists=Playlists(settings,db).list()
        if not playlists:
            bot.message(chat['id'],'Плейлистов пока нет. /newplaylist — создать.')
        else:
            bot.message(chat['id'],'Мои плейлисты:\n'+'\n'.join(f'{p["name"]} · {p["tracks"]}' for p in playlists),
                        [[{'text':p['name'],'url':web_url(p['path'])}] for p in playlists])
    elif command=='/status':
        rows=db.execute('SELECT id,query,status,stage FROM jobs ORDER BY id DESC LIMIT 5').fetchall()
        if not rows:
            bot.message(chat['id'],'Запросов пока нет. Пришли песню или ссылку.')
        else:
            bot.message(chat['id'],'Последние запросы:\n'+ '\n'.join(
                f'#{r["id"]} · {r["query"][:100]}\n{r["stage"] or r["status"]}' for r in rows))
    elif not text or len(text)>512:
        bot.message(chat['id'],'Пришли название с исполнителем или ссылку на одну песню, до 512 символов.')
    elif text.startswith('/'):
        bot.message(chat['id'],HELP)
    elif ClipUI(db,bot).pending(update['update_id'],chat['id'],user['id'],text):
        pass
    elif PlaylistUI(db,bot,settings).pending_name(update['update_id'],chat['id'],user['id'],text):
        pass
    else:
        queue(db,bot,update['update_id'],chat['id'],user['id'],text)


def process_queue(settings,stop):
    db=connect(settings)
    while not stop.is_set():
        job=claim_job(db)
        if not job:
            stop.wait(1)
            continue
        process=subprocess.Popen([sys.executable,'-m','aidj.worker',str(job['id'])],
                                 cwd=Path(__file__).resolve().parent.parent,
                                 stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            process.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.communicate()
        current=db.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()
        if current and current['status']=='processing':
            job_update(db,job['id'],status='failed',stage='Обработка прервалась',error='Задание прервалось или превысило 15 минут.')
            payload=json.loads(job['payload'])
            if payload.get('action')=='playlist_add':
                db.execute("UPDATE playlist_selections SET state='open' WHERE id=?",(payload['selection_id'],));db.commit()
            if payload.get('action') in ('playlist_add_one','playlist_device'):
                db.execute('INSERT OR IGNORE INTO app_keyboard_updates VALUES (?)',(payload['selection_id'],));db.commit()
            if payload.get('client')=='dj-review' and payload.get('card_id'):
                from . import dj_review
                try:dj_review.mark_failed(db,payload['card_id'],job['user_id'],job['chat_id'])
                except dj_review.ReviewError:pass
            if payload.get('client')=='review' and payload.get('card_id'):
                db.execute("UPDATE review_cards SET state='failed' WHERE id=? AND state='queued'",(payload['card_id'],));db.commit()
            try:
                config=json.loads((settings.state/'telegram.json').read_text())
                if payload.get('client')!='app' and job['message_id']:Telegram(config).edit(job['chat_id'],job['message_id'],'Обработка прервалась или превысила 15 минут. Пришли запрос заново.')
            except Exception:
                pass


def configure_profile(db,bot):
    if meta_get(db,'profile_version')=='8':return True
    try:
        bot.call('setMyDescription',{'description':'Слушай песни из плейлиста и ставь оценки. Предлагай свои записи: сначала послушай, затем отправь на общую разметку.'})
        bot.call('setMyShortDescription',{'short_description':'Слушаем, оцениваем и предлагаем песни для общего плейлиста.'})
        review_commands=[{'command':'apk','description':'Установить плеер: Android / iPhone'},{'command':'review','description':'Слушать и оценивать песни'},
            {'command':'suggest','description':'Предложить песню'},{'command':'help','description':'Помощь'}]
        bot.call('setMyCommands',{'commands':review_commands})
        owner_chat=meta_get(db,'owner_chat_id')
        if owner_chat:bot.call('setMyCommands',{'scope':{'type':'chat','chat_id':int(owner_chat)},'commands':[
            *review_commands,{'command':'djreview','description':'Проверить DJ-точки'},
            {'command':'reviewstats','description':'Сводка оценок'},
            {'command':'suggestions','description':'Предложенные песни'},{'command':'reviewlink','description':'Ссылка для друзей'},
            {'command':'start','description':'Как пользоваться'},
            {'command':'app','description':'Музыка: плеер и плейлисты'},
            {'command':'android','description':'Android-приложение: установка и подключение'},
            {'command':'playlists','description':'Мои плейлисты'},{'command':'newplaylist','description':'Создать плейлист'},
            {'command':'cancel','description':'Отменить ввод названия'},{'command':'status','description':'Последние запросы'}]})
        meta_set(db,'profile_version','8')
        return True
    except TelegramError:
        print('BOT_PROFILE_UPDATE_DEFERRED',flush=True)
        return False


def main():
    settings=Settings();settings.prepare();db=connect(settings)
    instance_lock=(settings.state/'bot.lock').open('a')
    fcntl.flock(instance_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    config_path=settings.state/'telegram.json'
    if not config_path.exists():
        print('WAITING_FOR_BOT_TOKEN',flush=True)
        while not config_path.exists():
            if (settings.state/'telegram.pending.json').exists():
                try:activate_pending(settings,attempts=1)
                except SetupError:pass
            time.sleep(2)
    config=json.loads(config_path.read_text())
    bot=Telegram(config)
    me=bot.call('getMe')
    if me.get('username')!=config.get('username'):
        raise RuntimeError('BOT_IDENTITY_MISMATCH')
    if bot.call('getWebhookInfo').get('url'):
        raise RuntimeError('BOT_ALREADY_HAS_WEBHOOK')
    db.execute("UPDATE jobs SET status='failed',stage='Обработка прервалась',error='Сервис был перезапущен. Пришли запрос заново.',updated=? WHERE status='processing'",(time.time(),));db.commit()
    db.execute("""UPDATE playlist_selections SET state='open' WHERE state='queued' AND NOT EXISTS
        (SELECT 1 FROM jobs WHERE status IN ('queued','processing')
         AND json_extract(payload,'$.selection_id')=playlist_selections.id)""");db.commit()
    db.execute("""UPDATE review_cards SET state='failed' WHERE state='queued' AND NOT EXISTS
        (SELECT 1 FROM jobs WHERE json_extract(payload,'$.card_id')=review_cards.id AND status IN ('queued','processing'))""");db.commit()
    configure_profile(db,bot)
    profile_retry=time.monotonic()+300
    stop=threading.Event()
    worker=threading.Thread(target=process_queue,args=(settings,stop),daemon=True);worker.start()
    from .dj_review import recover_jobs
    recover_jobs(db)
    from .review_delivery import maintenance
    delivery=threading.Thread(target=maintenance,args=(settings,stop),daemon=True);delivery.start()
    print('BOT_READY @'+me['username'],flush=True)
    failures=0
    while not stop.is_set():
        try:
            if time.monotonic()>=profile_retry:
                configure_profile(db,bot);profile_retry=time.monotonic()+300
            for pending in db.execute('SELECT sid FROM app_keyboard_updates LIMIT 20').fetchall():
                Playlists(settings,db).refresh(bot,pending['sid'])
                db.execute('DELETE FROM app_keyboard_updates WHERE sid=?',(pending['sid'],));db.commit()
            url=public_url(settings)
            if url and meta_get(db,'miniapp_url')!=url:
                owner=meta_get(db,'owner_chat_id')
                if owner:
                    bot.call('setChatMenuButton',{'chat_id':int(owner),'menu_button':{'type':'web_app','text':'Музыка','web_app':{'url':url}}})
                    meta_set(db,'miniapp_url',url)
            offset=int(meta_get(db,'update_offset','0'))
            updates=bot.call('getUpdates',{'offset':offset,'timeout':25,'allowed_updates':['message','callback_query']},timeout=40)
            for update in updates:
                handle_update(db,config,bot,update,settings)
                meta_set(db,'update_offset',update['update_id']+1)
            failures=0
        except TelegramError:
            failures+=1
            if failures==1 or failures%12==0:print('TELEGRAM_CONNECTION_RETRY',flush=True)
            stop.wait(5)
        except Exception as exc:
            print('UPDATE_ERROR',type(exc).__name__,flush=True)
            stop.wait(2)


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print('BOT_STOPPED',type(exc).__name__,flush=True)
        raise SystemExit(1)
