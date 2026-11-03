"""Owner-only app views of the same votes and suggestions as Telegram."""
import json
import re
import time

from . import reviews as R
from .store import meta_get
from .web_catalog import result_for


def context(db):
    pid=meta_get(db,'review_shared_playlist')
    playlist=db.execute('SELECT id,name FROM playlists WHERE id=?',(pid,)).fetchone() if pid else None
    if not playlist:playlist=db.execute("SELECT id,name FROM playlists ORDER BY CASE WHEN name_key='volleyball' THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
    return playlist


def vote_rows(db,pid,md5=None):
    rows=db.execute('''SELECT v.*,json_extract(i.result,'$.md5') AS md5 FROM review_votes v
        JOIN review_items i ON i.id=v.item_id WHERE v.playlist_id=?
        AND (? IS NULL OR json_extract(i.result,'$.md5')=?) ORDER BY v.updated DESC,v.rowid DESC''',(pid,md5,md5))
    seen=set();answer=[]
    for row in rows:
        identity=(row['md5'],row['user_id'])
        if identity not in seen:seen.add(identity);answer.append(row)
    return answer


def empty_rating():return {'likes':0,'dislikes':0,'neutral':0,'total':0,'mine':None,'proposals':0}


def snapshot(db):
    playlist=context(db);pid=playlist['id'] if playlist else None
    owner=int(meta_get(db,'owner_id','0'));ratings={};participants=set();total=0
    if pid is not None:
        for row in vote_rows(db,pid):
            value=ratings.setdefault(row['md5'],empty_rating());value[{1:'likes',-1:'dislikes',0:'neutral'}[row['value']]]+=1
            value['total']+=1;total+=1;participants.add(row['user_id'])
            if row['user_id']==owner:value['mine']=row['value']
        for row in db.execute("""SELECT json_extract(i.result,'$.md5') AS md5,COUNT(DISTINCT p.user_id) AS n
            FROM review_proposals p JOIN review_items i ON i.id=p.item_id
            WHERE p.playlist_id=? AND p.status='confirmed' GROUP BY md5""",(pid,)):
            ratings.setdefault(row['md5'],empty_rating())['proposals']=row['n']
    pool=set(r[0] for r in db.execute("SELECT md5 FROM playlist_tracks WHERE playlist_id=? AND state='complete'",(pid,)))
    pool.update(digest for digest,value in ratings.items() if value['proposals'])
    return ratings,{'playlistId':pid,'playlistName':playlist['name'] if playlist else '',
        'suggestionsPlaylistId':int(meta_get(db,'review_suggestions_playlist','0')) or None,
        'total':total,'participants':len(participants)},pool


def details(db,asset):
    playlist=context(db)
    if not playlist:raise R.ReviewError('Общий плейлист пока не готов.')
    pid=playlist['id'];owner=int(meta_get(db,'owner_id','0'));digest=asset['md5']
    ratings,info,_=snapshot(db)
    profiles={r['user_id']:r for r in db.execute('SELECT * FROM review_profiles')}
    people={}
    def person(uid):
        if uid not in people:
            profile=profiles.get(uid)
            people[uid]={'label':R.person_label(profile) if profile else ('Ты' if uid==owner else 'Участник '+str(uid)),
                'username':(profile['username'] or None) if profile else None,'isMine':uid==owner,'vote':None,'proposed':False}
        return people[uid]
    for row in vote_rows(db,pid,digest):person(row['user_id'])['vote']=row['value']
    for row in db.execute("""SELECT DISTINCT p.user_id FROM review_proposals p JOIN review_items i ON i.id=p.item_id
        WHERE p.playlist_id=? AND p.status='confirmed' AND json_extract(i.result,'$.md5')=?""",(pid,digest)):
        person(row['user_id'])['proposed']=True
    from .playlists import Playlists
    result=result_for(asset,db)
    return {'trackId':asset['id'],'rating':ratings.get(digest,empty_rating()),'people':list(people.values()),
        'playlistId':pid,'playlistName':playlist['name'],
        'inPlaylist':Playlists(None,db).contains(pid,result),
        'similar':[{'label':m['label'],'exact':m['exact']} for m in R.matches(db,pid,result)]}


def request(db,body):
    action=body.get('action');track_id=body.get('trackId')
    if not isinstance(track_id,str) or not re.fullmatch(r'[a-f0-9]{24}',track_id):raise R.ReviewError('Песня не найдена.')
    asset=db.execute('SELECT * FROM web_assets WHERE id=?',(track_id,)).fetchone()
    if not asset:raise R.ReviewError('Песня не найдена.')
    if action=='details':return details(db,asset)
    if action not in ('vote','archive','propose'):raise R.ReviewError('Неизвестное действие.')
    op=body.get('opId','')
    if not isinstance(op,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',op):raise R.ReviewError('Некорректный запрос.')
    value=body.get('value')
    if action=='vote' and value is not None and (type(value) is not int or value not in (-1,0,1)):raise R.ReviewError('Выбери одну из оценок.')
    playlist=context(db)
    if not playlist:raise R.ReviewError('Общий плейлист пока не готов.')
    pid=playlist['id'];uid=int(meta_get(db,'owner_id','0'));chat=int(meta_get(db,'owner_chat_id','0'))
    if not uid or not chat:raise R.ReviewError('Владелец не подключён.')
    encoded=json.dumps({'action':action,'trackId':track_id,'value':value},sort_keys=True)
    previous=db.execute('SELECT payload FROM review_web_ops WHERE id=?',(op,)).fetchone()
    if previous:
        if previous[0]!=encoded:raise R.ReviewError('Этот запрос уже использован.')
        return details(db,asset)
    item=R.remember(db,result_for(asset,db))
    if action=='propose':
        proposal=R.proposal(db,uid,chat,pid,item)
        _,accepted=R.confirm(db,proposal['id'],uid,chat)
        db.execute('INSERT INTO review_web_ops VALUES (?,?,?)',(op,encoded,time.time()));db.commit()
        return {**details(db,asset),'alreadyInPlaylist':not accepted}
    db.execute('BEGIN IMMEDIATE')
    try:
        previous=db.execute('SELECT payload FROM review_web_ops WHERE id=?',(op,)).fetchone()
        if previous:
            if previous[0]!=encoded:raise R.ReviewError('Этот запрос уже использован.')
        else:
            if action=='vote':
                db.execute("""DELETE FROM review_votes WHERE playlist_id=? AND user_id=? AND item_id IN
                    (SELECT id FROM review_items WHERE json_extract(result,'$.md5')=?)""",(pid,uid,asset['md5']))
                if value is not None:db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(pid,item,uid,value,time.time()))
            else:
                db.execute("""UPDATE review_proposals SET status='archived' WHERE playlist_id=? AND status='confirmed'
                    AND item_id IN (SELECT id FROM review_items WHERE json_extract(result,'$.md5')=?)""",(pid,asset['md5']))
            db.execute('INSERT INTO review_web_ops VALUES (?,?,?)',(op,encoded,time.time()))
        db.commit()
    except BaseException:db.rollback();raise
    return details(db,asset)
