"""One daily real download; alert the owner only when the failure changes."""
import fcntl
import json
from pathlib import Path
import shutil
import tempfile
import time

from .config import Settings
from .sources import Sources,SourceError
from .store import connect,meta_get,meta_set
from .telegram import Telegram


def category(error):
    message=str(error)
    if 'обновить подключение YouTube' in message:return 'auth'
    if 'соединения с YouTube' in message:return 'network'
    return 'unavailable'


def probe(settings,db,sources):
    ids=['KBkqRCn4INY']
    for row in db.execute('SELECT result FROM cache ORDER BY created DESC LIMIT 15'):
        result=json.loads(row[0]);track=result['track']
        if track['source']=='youtube' and 30<result['duration']<360 and track['id'] not in ids:ids.append(track['id'])
    if shutil.disk_usage(settings.state).free<2*1024**3:return 'server'
    for video in ids[:2]:
        try:
            with tempfile.TemporaryDirectory(prefix='youtube-health-',dir=settings.state/'work') as work:
                track=sources.youtube_track(video);file=sources.download_youtube(track,Path(work))
                if not file.is_file() or file.stat().st_size<=0:raise SourceError('Нет аудиофайла.')
            return 'ok'
        except SourceError as error:
            result=category(error)
            if result in ('auth','network'):return result
        except Exception:return 'server'
    return 'unavailable'


def record(settings,db,status,bot_factory=Telegram):
    old=json.loads(meta_get(db,'youtube_health','{}'));now=time.time()
    value={'status':status,'checked':now,'last_success':now if status=='ok' else old.get('last_success'),
           'notified':old.get('notified',False) if old.get('status')==status else False}
    meta_set(db,'youtube_health',json.dumps(value))
    if status=='ok' or value['notified']:return 0
    owner=meta_get(db,'owner_chat_id')
    if not owner:return 75
    texts={
        'auth':'Ежедневная проверка: YouTube снова требует вход. Нужно обновить cookies на сервере.\n\nВ терминале VM: python3 ~/aidj_codex_music/connect_youtube.py\nВставь свежие cookies YouTube и нажми Enter.',
        'network':'Ежедневная проверка: не удалось соединиться с YouTube. Это сетевая ошибка; необходимость менять cookies не подтверждена.',
        'unavailable':'Ежедневная проверка: не удалось скачать проверочные записи YouTube. Нужна диагностика; требование обновить cookies не подтверждено.',
        'server':'Ежедневная проверка YouTube прервалась на сервере. Нужно проверить сервис; необходимость менять cookies не подтверждена.',
    }
    try:bot_factory(json.loads((settings.state/'telegram.json').read_text())).message(int(owner),texts[status])
    except Exception:return 75
    value['notified']=True;meta_set(db,'youtube_health',json.dumps(value));return 0


def main():
    settings=Settings();db=connect(settings)
    with (settings.state/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print('YOUTUBE_HEALTH_DEFERRED_BUSY',flush=True);return 75
        previous=json.loads(meta_get(db,'youtube_health','{}'))
        if previous.get('status') not in (None,'ok') and not previous.get('notified') and time.time()-previous.get('checked',0)<3600:
            status=previous['status']
        else:
            try:status=probe(settings,db,Sources(settings))
            except Exception:status='server'
        outcome=record(settings,db,status)
        print('YOUTUBE_HEALTH',status,'notification_retry' if outcome else 'recorded',flush=True)
        return outcome


if __name__=='__main__':raise SystemExit(main())
