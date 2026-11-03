import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time

from .disk import web_url
from .playlists import Playlists, PlaylistError, playlist_name, track_key


def asset_id(key):
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def sync_assets(db):
    db.execute('BEGIN IMMEDIATE')
    for row in db.execute("SELECT * FROM playlist_tracks WHERE state='complete' ORDER BY playlist_id,position").fetchall():
        artist,sep,title=row['label'].partition(' — ')
        if not sep:title,artist=artist,''
        db.execute('''INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,artist=excluded.artist,duration=excluded.duration,md5=excluded.md5,
            bytes=excluded.bytes,remote_path=excluded.remote_path''',
            (asset_id(row['track_key']),row['track_key'],title,artist,row['duration'],row['md5'],row['bytes'],row['remote_path']))
    for cache in db.execute("SELECT result,created FROM cache WHERE key NOT LIKE 'review:%' UNION ALL SELECT result,created FROM audio_clips").fetchall():
        result=json.loads(cache[0]);track=result['track'];key=track_key(result)
        title=track['title']+(' ('+track['version']+')' if track.get('version') else '')
        db.execute('INSERT OR IGNORE INTO web_assets VALUES (?,?,?,?,?,?,?,?)',
            (asset_id(key),key,title,track['artist'],result['duration'],result['md5'],result['bytes'],result['remote_path']))
        db.execute('INSERT OR IGNORE INTO library_added VALUES (?,?)',(key,cache['created']))
    db.commit()


def revision(db,playlist_id):
    p=db.execute('SELECT name FROM playlists WHERE id=?',(playlist_id,)).fetchone()
    if not p:raise PlaylistError('Плейлист не найден.')
    rows=db.execute("SELECT track_key,md5 FROM playlist_tracks WHERE playlist_id=? AND state='complete' ORDER BY position",(playlist_id,)).fetchall()
    return hashlib.sha256(json.dumps([p[0],[tuple(r) for r in rows]],ensure_ascii=False).encode()).hexdigest()[:20]


def catalog(db):
    sync_assets(db)
    from .review_web import snapshot,empty_rating
    ratings,review,pool=snapshot(db)
    tracks=[]
    ranges={r['track_id']:r for r in db.execute('SELECT * FROM playback_ranges')}
    for row in db.execute('SELECT a.* FROM web_assets a LEFT JOIN library_added n ON n.track_key=a.track_key ORDER BY COALESCE(n.added,0) DESC,a.rowid DESC'):
        value={k:row[k] for k in ['id','title','artist','duration','md5','bytes']} | {'format':PurePosixPath(row['remote_path']).suffix[1:],
            'rating':ratings.get(row['md5'],empty_rating())}
        if row['id'] in ranges:value.update(rangeStart=ranges[row['id']]['start'],rangeEnd=ranges[row['id']]['end'])
        tracks.append(value)
    playlists=[]
    for p in db.execute('SELECT * FROM playlists ORDER BY id').fetchall():
        ids=[asset_id(r[0]) for r in db.execute("SELECT track_key FROM playlist_tracks WHERE playlist_id=? AND state='complete' ORDER BY position",(p['id'],))]
        playlists.append({'id':p['id'],'name':p['name'],'trackIds':ids,'revision':revision(db,p['id']),'diskUrl':web_url(p['path'])})
    review['poolTrackIds']=[t['id'] for t in tracks if t['md5'] in pool]
    return {'tracks':tracks,'playlists':playlists,'review':review}


def result_for(row,db=None):
    source,_,value=row['track_key'].partition(':')
    if source=='clip' and db is not None:
        original=db.execute('SELECT result FROM audio_clips WHERE track_key=?',(row['track_key'],)).fetchone()
        if original:
            result=json.loads(original[0]);result.update(remote_path=row['remote_path'],disk_url=web_url(row['remote_path']))
            return result
    url=('https://music.yandex.ru/track/' if source=='yandex' else 'https://music.youtube.com/watch?v=')+value
    return {'track':{'source':source,'id':value,'title':row['title'],'artist':row['artist'],'url':url},
            'remote_path':row['remote_path'],'disk_url':web_url(row['remote_path']),
            'md5':row['md5'],'bytes':row['bytes'],'duration':row['duration']}


class Conflict(PlaylistError):
    pass


def mutate(settings,db,disk,body,work):
    op_id=body.get('opId','')
    if not isinstance(op_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',op_id):
        raise PlaylistError('Некорректный запрос.')
    previous=db.execute('SELECT * FROM web_operations WHERE id=?',(op_id,)).fetchone()
    if previous and previous['kind']!=body.get('action'):raise PlaylistError('Некорректный запрос.')
    if previous and previous['state']=='complete':return
    action=body.get('action')
    if action not in ('create','add','reorder','remove','rename'):raise PlaylistError('Неизвестное действие.')
    if previous and action in ('create','add','reorder'):
        body=json.loads(previous['payload'])
    manager=Playlists(settings,db,disk)
    if action=='create':
        if not previous:
            db.execute('INSERT INTO web_operations(id,kind,payload,created) VALUES (?,?,?,?)',(op_id,action,json.dumps(body),time.time()));db.commit()
        manager.create(body.get('name',''))
    else:
        playlist=db.execute('SELECT * FROM playlists WHERE id=?',(body.get('playlistId'),)).fetchone()
        if not playlist:raise PlaylistError('Плейлист не найден.')
        if not previous and body.get('revision')!=revision(db,playlist['id']):
            raise Conflict('Плейлист уже изменился. Обнови его и повтори.')
        if action in ('add','reorder') and not previous:
            db.execute('INSERT INTO web_operations(id,kind,payload,created) VALUES (?,?,?,?)',(op_id,action,json.dumps(body),time.time()));db.commit()
        if action=='add':
            asset=db.execute('SELECT * FROM web_assets WHERE id=?',(body.get('trackId'),)).fetchone()
            if not asset:raise PlaylistError('Песня не найдена.')
            manager.add(playlist['id'],result_for(asset,db),work)
        elif action=='reorder':
            rows=db.execute("SELECT track_key FROM playlist_tracks WHERE playlist_id=? AND state='complete' ORDER BY position",(playlist['id'],)).fetchall()
            keys={asset_id(r[0]):r[0] for r in rows};ids=body.get('trackIds',[])
            if not isinstance(ids,list) or len(ids)!=len(keys) or set(ids)!=set(keys):
                raise Conflict('Список песен изменился. Обнови плейлист.')
            pending=[r[0] for r in db.execute("SELECT track_key FROM playlist_tracks WHERE playlist_id=? AND state!='complete' ORDER BY position",(playlist['id'],))]
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('UPDATE playlist_tracks SET position=-position-10000 WHERE playlist_id=?',(playlist['id'],))
                for position,key in enumerate([keys[x] for x in ids]+pending,1):
                    db.execute('UPDATE playlist_tracks SET position=? WHERE playlist_id=? AND track_key=?',(position,playlist['id'],key))
                db.commit()
            except BaseException:db.rollback();raise
            manager.write_index(playlist['id'],work)
        elif action in ('remove','rename'):
            if previous:
                if previous['kind']!=action:raise PlaylistError('Некорректный запрос.')
                plan=json.loads(previous['payload'])
            elif action=='remove':
                asset=db.execute('SELECT * FROM web_assets WHERE id=?',(body.get('trackId'),)).fetchone()
                row=db.execute('SELECT * FROM playlist_tracks WHERE playlist_id=? AND track_key=?',
                               (playlist['id'],asset['track_key'] if asset else '')).fetchone()
                if not row:raise PlaylistError('В плейлисте нет этой песни.')
                parent=f'/Музыка/_aidj_removed/{playlist["id"]}'
                disk.mkdir('/Музыка/_aidj_removed');disk.mkdir(parent)
                plan={'source':row['remote_path'],'target':parent+'/'+op_id+'-'+PurePosixPath(row['remote_path']).name,
                      'md5':row['md5'],'bytes':row['bytes'],'key':row['track_key'],'asset':asset['id']}
            else:
                name=playlist_name(body.get('name',''))
                other=db.execute('SELECT id FROM playlists WHERE name_key=? AND id!=?',(name.casefold(),playlist['id'])).fetchone()
                if other:raise PlaylistError('Такое название уже есть.')
                target='/Музыка/'+name
                if target!=playlist['path'] and disk.metadata(target):raise PlaylistError('На Диске уже есть такая папка.')
                plan={'source':playlist['path'],'target':target,'name':name}
            if not previous:
                db.execute('INSERT INTO web_operations(id,kind,payload,created) VALUES (?,?,?,?)',
                           (op_id,action,json.dumps(plan,ensure_ascii=False),time.time()));db.commit()
            if plan['source']!=plan['target']:
                target=disk.metadata(plan['target'])
                if not target:
                    if action=='remove' and not disk.verify(plan['source'],plan['md5'],plan['bytes']):
                        raise PlaylistError('Файл изменён вручную. Удаление отменено.')
                    disk.move(plan['source'],plan['target'])
                if action=='remove' and not disk.verify(plan['target'],plan['md5'],plan['bytes']):
                    raise PlaylistError('Диск не подтвердил изменение.')
            db.execute('BEGIN IMMEDIATE')
            try:
                if action=='remove':
                    db.execute('DELETE FROM playlist_tracks WHERE playlist_id=? AND track_key=?',(playlist['id'],plan['key']))
                    db.execute('DELETE FROM playlist_aliases WHERE playlist_id=? AND canonical_key=?',(playlist['id'],plan['key']))
                    db.execute('UPDATE web_assets SET remote_path=? WHERE remote_path=?',(plan['target'],plan['source']))
                else:
                    prefix=plan['source']+'/'
                    for table,column in [('playlist_tracks','remote_path'),('web_assets','remote_path')]:
                        rows=db.execute('SELECT rowid,remote_path FROM '+table+' WHERE substr(remote_path,1,?)=?',(len(prefix),prefix)).fetchall()
                        for row in rows:db.execute('UPDATE '+table+' SET remote_path=? WHERE rowid=?',(plan['target']+'/'+row['remote_path'][len(prefix):],row['rowid']))
                    db.execute('UPDATE playlists SET name=?,name_key=?,path=? WHERE id=?',(plan['name'],plan['name'].casefold(),plan['target'],playlist['id']))
                db.commit()
            except BaseException:db.rollback();raise
            if action=='rename' and plan['source']!=plan['target']:
                old=plan['target']+'/'+PurePosixPath(plan['source']).name+'.m3u8'
                new=plan['target']+'/'+plan['name']+'.m3u8'
                if old!=new and disk.metadata(old) and not disk.metadata(new):disk.move(old,new)
            manager.write_index(playlist['id'],work)
        else:raise PlaylistError('Неизвестное действие.')
    db.execute("INSERT INTO web_operations(id,kind,payload,state,created) VALUES (?,?,?,'complete',?) ON CONFLICT(id) DO UPDATE SET state='complete'",
               (op_id,action,'{}',time.time()));db.commit()
