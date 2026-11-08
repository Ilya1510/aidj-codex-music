"""Dynamic app-only playlist unions; source memberships and Disk stay unchanged."""
import hashlib
import json
import re
import time

from .playlists import PlaylistError,playlist_name

BASE=900001000
ORDINARY_LIMIT=900000000
ACTIONS=('combine','rename-combined','delete-combined')


class CombinedConflict(PlaylistError):pass


def name_exists(db,key,exclude=None):
    return db.execute('SELECT 1 FROM playlist_combined WHERE name_key=? AND id!=?',
                      (key,-1 if exclude is None else exclude)).fetchone() is not None


def _name(value):
    if not isinstance(value,str):raise PlaylistError('Некорректное название плейлиста.')
    return playlist_name(value)


def _sources(value):
    if (not isinstance(value,list) or not 2<=len(value)<=30 or
        any(type(x) is not int or not 0<x<ORDINARY_LIMIT for x in value) or len(set(value))!=len(value)):
        raise PlaylistError('Выбери от 2 до 30 разных обычных плейлистов.')
    return value


def _internal(value):
    if type(value) is not int or not BASE<value<2**53:raise PlaylistError('Объединённый плейлист не найден.')
    return value-BASE


def _available_name(db,name,exclude=None):
    if db.execute('SELECT 1 FROM playlists WHERE name_key=?',(name.casefold(),)).fetchone() or name_exists(db,name.casefold(),exclude):
        raise PlaylistError('Такое название уже есть.')


def record(db,row):
    from .web_catalog import asset_id
    sources=json.loads(row['sources']);seen=set();tracks=[];fingerprint=[]
    for source in sources:
        for track in db.execute("SELECT track_key,md5 FROM playlist_tracks WHERE playlist_id=? AND state='complete' ORDER BY position",(source,)):
            if track['md5'] in seen:continue
            seen.add(track['md5']);key=asset_id(track['track_key']);tracks.append(key);fingerprint.append([key,track['md5']])
    revision=hashlib.sha256(json.dumps([row['name'],sources,fingerprint],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:20]
    return {'id':BASE+row['id'],'name':row['name'],'trackIds':tracks,'revision':revision,
            'combined':True,'sourcePlaylistIds':sources}


def records(db):
    return [record(db,row) for row in db.execute('SELECT * FROM playlist_combined ORDER BY id')]


def mutate(db,body):
    """The definition and replay receipt commit together; never call Disk."""
    action=body.get('action');op=body.get('opId')
    if action not in ACTIONS or not isinstance(op,str) or not re.fullmatch('[A-Za-z0-9_-]{8,100}',op):
        raise PlaylistError('Некорректный запрос.')
    request={'action':action}
    if action=='combine':request.update(name=_name(body.get('name')),sourcePlaylistIds=_sources(body.get('sourcePlaylistIds')))
    else:
        request['playlistId']=BASE+_internal(body.get('playlistId'))
        revision=body.get('revision')
        if not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{20}',revision):raise PlaylistError('Некорректная версия плейлиста.')
        request['revision']=revision
        if action=='rename-combined':request['name']=_name(body.get('name'))
    payload=json.dumps(request,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    db.execute('BEGIN IMMEDIATE')
    try:
        previous=db.execute('SELECT kind,payload,state FROM web_operations WHERE id=?',(op,)).fetchone()
        if previous:
            if previous['kind']!=action or previous['payload']!=payload or previous['state']!='complete':
                raise PlaylistError('Этот запрос уже использован для другого изменения.')
            db.commit();return
        if action=='combine':
            if db.execute('SELECT COUNT(*) FROM playlist_combined').fetchone()[0]>=30:raise PlaylistError('Уже создано 30 объединённых плейлистов.')
            for source in request['sourcePlaylistIds']:
                if not db.execute('SELECT 1 FROM playlists WHERE id=?',(source,)).fetchone():raise PlaylistError('Исходный плейлист не найден.')
            _available_name(db,request['name'])
            db.execute('INSERT INTO playlist_combined(name,name_key,sources,created) VALUES (?,?,?,?)',
                (request['name'],request['name'].casefold(),json.dumps(request['sourcePlaylistIds']),time.time()))
        else:
            internal=request['playlistId']-BASE
            row=db.execute('SELECT * FROM playlist_combined WHERE id=?',(internal,)).fetchone()
            if row is None:raise PlaylistError('Объединённый плейлист не найден.')
            if request['revision']!=record(db,row)['revision']:raise CombinedConflict('Плейлист уже изменился. Обнови его и повтори.')
            if action=='delete-combined':db.execute('DELETE FROM playlist_combined WHERE id=?',(internal,))
            else:
                _available_name(db,request['name'],internal)
                db.execute('UPDATE playlist_combined SET name=?,name_key=? WHERE id=?',(request['name'],request['name'].casefold(),internal))
        db.execute("INSERT INTO web_operations(id,kind,payload,state,created) VALUES (?,?,?,'complete',?)",(op,action,payload,time.time()))
        db.commit()
    except BaseException:db.rollback();raise
