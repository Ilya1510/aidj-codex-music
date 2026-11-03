"""Persistent app requests share the bot's single processing queue."""
import json
import re
import tempfile
import time
from pathlib import Path

from .audio import AudioError
from .disk import Disk, DiskError
from .locking import worker_lock
from .pipeline import Pipeline, SelectionNeeded
from .playlists import PlaylistError
from .sources import Sources, SourceError, Unavailable, Track, parse_input, ranked, normalize
from .store import job_update, meta_get
from .web_catalog import asset_id, sync_assets
from .web_catalog import result_for
from .clips import bounds,create as create_clip


class DiscoverError(ValueError):
    pass


def owned_job(db, job_id):
    row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row or json.loads(row['payload']).get('client')!='app':
        raise DiscoverError('Запрос не найден.')
    return row


def public_job(row):
    payload=json.loads(row['payload']);result=json.loads(row['result'] or '{}')
    return {'id':row['id'],'query':row['query'],'kind':payload['action'],
            'status':row['status'],'stage':row['stage'] or 'В очереди…',
            'error':row['error'],'youtubeOnly':payload.get('youtube_only',False),
            'candidates':[{k:t.get(k) for k in ('title','artist','duration','version','source','available')}
                          | {'index':i,'inLibrary':t['source']+':'+t['id'] in result.get('libraryKeys',[])} for i,t in enumerate(result.get('candidates',[]))],
            'trackId':result.get('trackId'),
            'clip':{'trackId':payload['asset_id'],'start':payload['start'],'end':payload['end']} if payload['action']=='clip' else None}


def recent(db):
    rows=db.execute("SELECT * FROM jobs WHERE json_extract(payload,'$.client')='app' ORDER BY id DESC LIMIT 12").fetchall()
    return {'jobs':[public_job(row) for row in rows]}


def submit(db,body):
    op=body.get('opId','')
    if not isinstance(op,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',op):
        raise DiscoverError('Некорректный запрос.')
    action=body.get('action')
    if action=='search':
        query=body.get('query','')
        if not isinstance(query,str):raise DiscoverError('Введи название песни или ссылку.')
        query=query.strip();parse_input(query)
        if type(body.get('youtubeOnly',False)) is not bool:raise DiscoverError('Некорректный источник.')
        payload={'client':'app','action':'search','youtube_only':body.get('youtubeOnly',False)}
    elif action=='clip':
        asset=db.execute('SELECT * FROM web_assets WHERE id=?',(body.get('trackId'),)).fetchone()
        if not asset:raise DiscoverError('Песня не найдена.')
        start,end=bounds(body.get('start'),body.get('end'),asset['duration'])
        query=asset['artist']+' — '+asset['title'];payload={'client':'app','action':'clip','asset_id':asset['id'],'start':start,'end':end}
    elif action=='prepare':
        if type(body.get('jobId')) is not int or type(body.get('index')) is not int:raise DiscoverError('Выбери найденную запись.')
        parent=owned_job(db,body['jobId']);choices=json.loads(parent['result'] or '{}').get('candidates',[])
        index=body['index']
        if index<0 or index>=len(choices):raise DiscoverError('Выбери найденную запись.')
        query=parent['query'];payload={'client':'app','action':'prepare','parent':parent['id'],'index':index,'track':choices[index]}
    else:raise DiscoverError('Неизвестное действие.')
    # Retry identity is checked before expiry and capacity; lost responses are safe.
    db.execute('BEGIN IMMEDIATE')
    try:
        previous=db.execute('SELECT * FROM jobs WHERE update_id=?',('app:'+op,)).fetchone()
        if previous:
            if previous['query']!=query or json.loads(previous['payload'])!=payload:raise DiscoverError('Этот запрос уже использован.')
            db.commit();return public_job(previous)
        if action=='prepare' and time.time()-parent['created']>3600:raise DiscoverError('Результаты устарели. Повтори поиск.')
        if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]>=10:
            raise DiscoverError('В очереди уже десять запросов. Дождись обработки.')
        if action=='prepare':
            same=db.execute("SELECT * FROM jobs WHERE json_extract(payload,'$.client')='app' AND json_extract(payload,'$.parent')=? AND json_extract(payload,'$.index')=? AND status IN ('queued','processing') ORDER BY id DESC LIMIT 1",(parent['id'],index)).fetchone()
            if same:db.commit();return public_job(same)
        now=time.time()
        cur=db.execute('INSERT INTO jobs(update_id,chat_id,user_id,message_id,query,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                       ('app:'+op,int(meta_get(db,'owner_chat_id','0')),int(meta_get(db,'owner_id','0')),0,query,json.dumps(payload,ensure_ascii=False),now,now))
        db.commit();return public_job(owned_job(db,cur.lastrowid))
    except BaseException:db.rollback();raise


def existing_asset(db,track):
    key=track['source']+':'+track['id']
    row=db.execute('SELECT * FROM web_assets WHERE track_key=?',(key,)).fetchone()
    if row:return row
    return db.execute('SELECT a.* FROM web_assets a JOIN playlist_aliases p ON p.canonical_key=a.track_key WHERE p.track_key=? LIMIT 1',(key,)).fetchone()


def process(settings,db,row,sources=None,disk=None):
    """No Telegram calls: app jobs report progress only through SQLite."""
    payload=json.loads(row['payload']);job_id=row['id']
    def notify(text):job_update(db,job_id,stage=text)
    try:
        with worker_lock(settings),tempfile.TemporaryDirectory(prefix=f'app-{job_id}-',dir=settings.state/'work') as folder:
            if payload['action']!='clip':sources=sources or Sources(settings)
            disk=disk or Disk(settings)
            if payload['action']=='search':
                notify('Ищу песню…');kind,value=parse_input(row['query'])
                library=[];sync_assets(db)
                known=existing_asset(db,{'source':kind,'id':value}) if kind in ('yandex','youtube') else None
                if known:
                    source,_,key=known['track_key'].partition(':')
                    url=('https://music.yandex.ru/track/' if source=='yandex' else 'https://music.youtube.com/watch?v=')+key
                    library=[Track(source,key,known['title'],known['artist'],url,known['duration'])];choices=library
                elif kind=='yandex':choices=[sources.yandex_track(value)]
                elif kind=='youtube':choices=[sources.youtube_track(value)]
                else:
                    wanted=set(normalize(value).split())
                    for asset in db.execute('SELECT * FROM web_assets'):
                        source,_,key=asset['track_key'].partition(':')
                        if source not in ('yandex','youtube') or (payload.get('youtube_only') and source!='youtube'):continue
                        if wanted and wanted<=set(normalize(asset['artist']+' '+asset['title']).split()):
                            url=('https://music.yandex.ru/track/' if source=='yandex' else 'https://music.youtube.com/watch?v=')+key
                            library.append(Track(source,key,asset['title'],asset['artist'],url,asset['duration']))
                    try:external=sources.search(value,youtube_only=payload.get('youtube_only',False))
                    except Exception:
                        if not library:raise
                        external=[]
                    unique={}
                    for track in [*library,*external]:unique.setdefault(track.source+':'+track.id,track)
                    choices=ranked(value,list(unique.values()))[:8]
                if not choices:raise Unavailable('Ничего не нашёл. Укажи исполнителя или пришли ссылку.')
                result={'candidates':[t.to_dict() for t in choices],'libraryKeys':[t.source+':'+t.id for t in library]}
                stage='Выбери запись для прослушивания'
            elif payload['action']=='clip':
                notify('Готовлю фрагмент…');asset=db.execute('SELECT * FROM web_assets WHERE id=?',(payload['asset_id'],)).fetchone()
                if not asset:raise Unavailable('Песня не найдена.')
                answer=create_clip(settings,db,result_for(asset,db),payload['start'],payload['end'],Path(folder),disk)
                sync_assets(db);result={'trackId':asset_id('clip:'+answer['track']['id'])};stage='Фрагмент готов'
            else:
                sync_assets(db);asset=existing_asset(db,payload['track'])
                if asset and disk.verify(asset['remote_path'],asset['md5'],asset['bytes']):
                    result={'trackId':asset['id']}
                else:
                    answer=Pipeline(settings,db,sources,disk).run(row['query'],{'track':payload['track']},Path(folder),notify,need_local=False)
                    sync_assets(db);track=answer['track'];result={'trackId':asset_id(track['source']+':'+track['id'])}
                stage='Можно слушать и добавлять в плейлисты'
            job_update(db,job_id,status='completed',stage=stage,result=json.dumps(result,ensure_ascii=False))
        return 0
    except SelectionNeeded as choice:
        job_update(db,job_id,status='awaiting_choice',stage='Выбери подходящую версию из YouTube Music',result=json.dumps({'candidates':[t.to_dict() for t in choice.tracks]},ensure_ascii=False))
        return 0
    except Exception as exc:
        message=str(exc) if isinstance(exc,(SourceError,AudioError,DiskError,PlaylistError)) else 'Не удалось обработать запрос. Повтори позже.'
        job_update(db,job_id,status='failed',stage='Ошибка',error=message)
        print('APP_JOB_FAILED',job_id,type(exc).__name__,flush=True);return 1
