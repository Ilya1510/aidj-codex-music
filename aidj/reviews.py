"""Shared listening pool; guests can vote and propose, never edit the library."""
import json
import random
import re
import secrets
import time
from difflib import SequenceMatcher

from .playlists import track_key
from .sources import normalize,parse_input
from .store import enqueue,meta_get,meta_set
from .web_catalog import asset_id,result_for,sync_assets


class ReviewError(ValueError):
    pass


def remember_profile(db,user):
    username=user.get('username') or ''
    if not re.fullmatch(r'[A-Za-z0-9_]{1,32}',username):username=''
    name=' '.join(str(user.get('first_name') or '').split())[:64]
    db.execute('''INSERT INTO review_profiles VALUES (?,?,?,?) ON CONFLICT(user_id)
        DO UPDATE SET username=excluded.username,display_name=excluded.display_name,updated=excluded.updated''',
        (user['id'],username,name,time.time()))
    db.execute("UPDATE review_removal_notices SET state='pending',retry_at=0 WHERE user_id=? AND state='blocked'",(user['id'],))
    db.execute("UPDATE review_notices SET state='pending',retry_at=0 WHERE user_id=? AND state='blocked'",(user['id'],));db.commit()


def person_label(row):
    return '@'+row['username'] if row['username'] else (row['display_name'] or 'Участник '+str(row['user_id']))


def people(db,playlist_id,item_id):
    return db.execute('''SELECT u.user_id,p.username,COALESCE(p.display_name,'') AS display_name,v.value,
        EXISTS(SELECT 1 FROM review_proposals s WHERE s.user_id=u.user_id AND s.playlist_id=?
            AND s.item_id=? AND s.status='confirmed') AS proposed
        FROM (SELECT user_id FROM review_votes WHERE playlist_id=? AND item_id=? UNION
            SELECT user_id FROM review_proposals WHERE playlist_id=? AND item_id=? AND status='confirmed') u
        LEFT JOIN review_profiles p ON p.user_id=u.user_id
        LEFT JOIN review_votes v ON v.user_id=u.user_id AND v.playlist_id=? AND v.item_id=?
        ORDER BY v.updated DESC,u.user_id''',(playlist_id,item_id,playlist_id,item_id,playlist_id,item_id,playlist_id,item_id)).fetchall()


def shared_playlist(db):
    current=meta_get(db,'review_shared_playlist')
    row=db.execute('SELECT id FROM playlists WHERE id=?',(current,)).fetchone() if current else None
    if row:return row[0]
    row=db.execute("SELECT id FROM playlists ORDER BY CASE WHEN name_key='volleyball' THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
    if not row:raise ReviewError('Плейлист для разметки ещё не готов.')
    meta_set(db,'review_shared_playlist',row[0]);return row[0]


def is_owner(db,user_id,chat_id):
    return meta_get(db,'owner_id')==str(user_id) and meta_get(db,'owner_chat_id')==str(chat_id)


def ensure_user(db,user_id,chat_id):
    row=db.execute('SELECT * FROM review_users WHERE user_id=?',(user_id,)).fetchone()
    if row and row['chat_id']!=chat_id:raise ReviewError('Открой бота в личном чате.')
    default=shared_playlist(db)
    if row and (is_owner(db,user_id,chat_id) or row['playlist_id']==default) and db.execute('SELECT 1 FROM playlists WHERE id=?',(row['playlist_id'],)).fetchone():return row
    db.execute('''INSERT INTO review_users VALUES (?,?,?,'menu',?) ON CONFLICT(user_id)
        DO UPDATE SET playlist_id=excluded.playlist_id''',(user_id,chat_id,default,time.time()));db.commit()
    return db.execute('SELECT * FROM review_users WHERE user_id=?',(user_id,)).fetchone()


def remember(db,result,commit=True):
    key=track_key(result);identity=asset_id(key)
    stored={k:v for k,v in result.items() if k not in ('local_path','telegram_file_id')}
    db.execute('''INSERT INTO review_items VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result,
        telegram_file_id=CASE WHEN json_extract(review_items.result,'$.md5')=json_extract(excluded.result,'$.md5')
            THEN COALESCE(excluded.telegram_file_id,review_items.telegram_file_id) ELSE excluded.telegram_file_id END''',
        (identity,key,json.dumps(stored,ensure_ascii=False),result.get('telegram_file_id'),time.time()))
    if commit:db.commit()
    return identity


def item_result(db,item_id):
    row=db.execute('SELECT * FROM review_items WHERE id=?',(item_id,)).fetchone()
    if not row:raise ReviewError('Песня не найдена.')
    result=json.loads(row['result'])
    # Playlist edits may have moved the original since this card was created.
    asset=db.execute('SELECT * FROM web_assets WHERE track_key=? AND md5=?',(row['track_key'],result['md5'])).fetchone()
    if asset:result.update(remote_path=asset['remote_path'],disk_url=result_for(asset,db)['disk_url'])
    result['telegram_file_id']=row['telegram_file_id']
    if not result['telegram_file_id']:
        cached=db.execute("SELECT telegram_file_id FROM cache WHERE telegram_file_id IS NOT NULL AND json_extract(result,'$.md5')=? LIMIT 1",(result['md5'],)).fetchone()
        if cached:result['telegram_file_id']=cached[0]
    return result


def playlist_items(db,playlist_id):
    sync_assets(db)
    ids=[]
    for row in db.execute("SELECT a.* FROM web_assets a JOIN playlist_tracks p ON p.track_key=a.track_key WHERE p.playlist_id=? AND p.state='complete'",(playlist_id,)).fetchall():
        # No network I/O: snapshot metadata and reuse Telegram's cached audio later.
        ids.append(remember(db,result_for(row,db),commit=False))
    db.commit()
    ids.extend(r[0] for r in db.execute("SELECT DISTINCT item_id FROM review_proposals WHERE playlist_id=? AND status='confirmed'",(playlist_id,)))
    return list(dict.fromkeys(ids))


def busy(db,user_id):
    return db.execute("SELECT 1 FROM jobs WHERE user_id=? AND json_extract(payload,'$.client')='review' AND json_extract(payload,'$.action')!='save' AND status IN ('queued','processing')",(user_id,)).fetchone() is not None


def capacity(db,user_id):
    if busy(db,user_id):raise ReviewError('Твой запрос уже обрабатывается. Подожди немного.')
    if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]>=10:
        raise ReviewError('Очередь заполнена. Попробуй чуть позже.')


def add_job(db,event,user_id,chat_id,query,payload,message_id=0):
    identity='review:'+str(event)
    previous=db.execute('SELECT id FROM jobs WHERE update_id=?',(identity,)).fetchone()
    if previous:return previous[0]
    capacity(db,user_id)
    return enqueue(db,identity,chat_id,user_id,message_id,query,{'client':'review',**payload})


def review_weight(total,dislikes):
    return 1.25 if dislikes else 1/(1+total)


def next_card(db,event,user_id,chat_id):
    user=ensure_user(db,user_id,chat_id);pid=user['playlist_id']
    current=db.execute("SELECT * FROM review_cards WHERE user_id=? AND state IN ('queued','open')",(user_id,)).fetchone()
    if current:return current,False
    capacity(db,user_id)
    pool=playlist_items(db,pid)
    if not pool:return None,False
    # Content aliases share votes and exclusions. A dislike gives a modest fixed
    # bonus over an unrated song, without multiplying the bonus for pile-ons.
    from .review_web import vote_rows
    votes={}
    for row in vote_rows(db,pid):votes.setdefault(row['md5'],[]).append(row)
    skipped={r[0] for r in db.execute("""SELECT json_extract(i.result,'$.md5') FROM review_cards c
        JOIN review_items i ON i.id=c.item_id WHERE c.playlist_id=? AND c.user_id=? AND c.state='skipped'""",(pid,user_id))}
    candidates={}
    digests={r['id']:r['md5'] for r in db.execute("SELECT id,json_extract(result,'$.md5') AS md5 FROM review_items")}
    proposed={digests[r[0]] for r in db.execute("SELECT DISTINCT item_id FROM review_proposals WHERE playlist_id=? AND status='confirmed'",(pid,)) if r[0] in digests}
    for identity in pool:
        digest=digests[identity];ratings=votes.get(digest,[])
        if digest in skipped or any(v['user_id']==user_id for v in ratings):continue
        tier=0 if digest in proposed and not ratings else 1 if digest in proposed else 2
        candidates.setdefault(digest,(identity,review_weight(len(ratings),sum(v['value']==-1 for v in ratings)),tier))
    values=list(candidates.values())
    if values:
        priority=min(v[2] for v in values)
        values=[v for v in values if v[2]==priority]
    item=random.choices([v[0] for v in values],weights=[v[1] for v in values],k=1)[0] if values else None
    if not item:return None,False
    card=secrets.token_hex(8)
    db.execute("INSERT INTO review_cards(id,user_id,chat_id,playlist_id,item_id,created) VALUES (?,?,?,?,?,?)",(card,user_id,chat_id,pid,item,time.time()));db.commit()
    try:
        add_job(db,event,user_id,chat_id,'Песня для разметки',{'action':'listen','card_id':card})
    except BaseException:
        db.execute("UPDATE review_cards SET state='failed' WHERE id=?",(card,));db.commit();raise
    return db.execute('SELECT * FROM review_cards WHERE id=?',(card,)).fetchone(),True


def vote(db,card_id,user_id,chat_id,value):
    if value not in (-1,0,1):raise ReviewError('Выбери одну из оценок.')
    db.execute('BEGIN IMMEDIATE')
    try:
        card=db.execute('SELECT * FROM review_cards WHERE id=? AND user_id=? AND chat_id=?',(card_id,user_id,chat_id)).fetchone()
        if not card or card['state'] not in ('open','voted'):raise ReviewError('Эта карточка недоступна. Нажми «Начать размечать».')
        db.execute('''INSERT INTO review_votes VALUES (?,?,?,?,?) ON CONFLICT(playlist_id,item_id,user_id)
            DO UPDATE SET value=excluded.value,updated=excluded.updated''',(card['playlist_id'],card['item_id'],user_id,value,time.time()))
        db.execute("UPDATE review_cards SET state='voted' WHERE id=?",(card_id,));db.commit()
        return card,card['state']=='open'
    except BaseException:db.rollback();raise


def proposal(db,user_id,chat_id,playlist_id,item_id):
    row=db.execute('SELECT * FROM review_proposals WHERE user_id=? AND playlist_id=? AND item_id=?',(user_id,playlist_id,item_id)).fetchone()
    if not row:
        db.execute('INSERT INTO review_proposals(id,user_id,chat_id,playlist_id,item_id,created) VALUES (?,?,?,?,?,?)',
            (secrets.token_hex(8),user_id,chat_id,playlist_id,item_id,time.time()));db.commit()
        row=db.execute('SELECT * FROM review_proposals WHERE user_id=? AND playlist_id=? AND item_id=?',(user_id,playlist_id,item_id)).fetchone()
    return row


def cleaned(value):
    value=re.sub(r'\b(?:official\s*(?:music\s*)?(?:video|audio|lyric\w*)|lyrics?|visuali[sz]er|hd|hq|4k)\b',' ',value,flags=re.I)
    return normalize(value)


def matches(db,playlist_id,result):
    """Identity/alias/hash are exact; metadata similarity is only an advisory."""
    track=result['track'];key=track_key(result);title=cleaned(track['title']);artist=cleaned(track['artist'])
    version=cleaned(track.get('version',''));requested=cleaned(artist+' '+title+' '+version)
    matches=[]
    for row in db.execute("SELECT * FROM playlist_tracks WHERE playlist_id=? AND state='complete'",(playlist_id,)):
        alias=db.execute('SELECT 1 FROM playlist_aliases WHERE playlist_id=? AND track_key=? AND canonical_key=?',(playlist_id,result.get('origin_key',key),row['track_key'])).fetchone()
        exact=key==row['track_key'] or bool(alias) or (result.get('md5') and result['md5']==row['md5'])
        label=cleaned(row['label']);a,_,t=row['label'].partition(' — ');a,t=cleaned(a),cleaned(t)
        found_title=title
        for prefix in (artist,a):
            if prefix and found_title.startswith(prefix+' '):found_title=found_title[len(prefix)+1:]
        duration_ok=not result.get('duration') or abs(row['duration']-result['duration'])<=max(8,row['duration']*.08)
        tokens=set(requested.split());found=set(label.split())
        score=SequenceMatcher(None,requested,label).ratio()
        title_ratio=SequenceMatcher(None,found_title,t).ratio()
        identity_words=(artist and artist==a and (found_title==t or title_ratio>=.86))
        same_title=len(t.split())>=2 and title_ratio>=.9
        containment=bool(tokens and found and (tokens<=found or found<=tokens))
        if exact or (duration_ok and (identity_words or same_title or containment or score>=.87)):
            matches.append({'exact':bool(exact),'key':row['track_key'],'label':row['label'],'score':1 if exact else score})
    return sorted(matches,key=lambda m:(m['exact'],m['score']),reverse=True)[:4]


def confirm(db,proposal_id,user_id,chat_id):
    row=db.execute('SELECT * FROM review_proposals WHERE id=? AND user_id=? AND chat_id=?',(proposal_id,user_id,chat_id)).fetchone()
    if not row:raise ReviewError('Это предложение недоступно.')
    if row['status']=='archived':raise ReviewError('Это предложение снято с разметки.')
    result=item_result(db,row['item_id'])
    if any(m['exact'] for m in matches(db,row['playlist_id'],result)):
        return row,False
    db.execute("UPDATE review_proposals SET status='confirmed' WHERE id=?",(proposal_id,));db.commit()
    from .review_delivery import schedule_saves
    schedule_saves(db)
    return row,True


def submit_suggestion(db,event,user_id,chat_id,query,message_id):
    user=ensure_user(db,user_id,chat_id);parse_input(query)
    recent=db.execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND json_extract(payload,'$.client')='review' AND json_extract(payload,'$.action')='suggest' AND created>?",(user_id,time.time()-3600)).fetchone()[0]
    if recent>=50:raise ReviewError('За час можно предложить до 50 записей. Продолжи чуть позже.')
    job=add_job(db,event,user_id,chat_id,query,{'action':'suggest','playlist_id':user['playlist_id']},message_id)
    db.execute("UPDATE review_users SET mode='menu' WHERE user_id=?",(user_id,));db.commit();return job


def summary(db,playlist_id,proposals_only=False):
    pool=playlist_items(db,playlist_id);eligible=set(pool)
    if proposals_only:
        eligible={r[0] for r in db.execute("SELECT DISTINCT item_id FROM review_proposals WHERE playlist_id=? AND status='confirmed'",(playlist_id,))}
    rows=db.execute('''SELECT i.*,SUM(CASE WHEN v.value=1 THEN 1 ELSE 0 END) AS likes,
        SUM(CASE WHEN v.value=-1 THEN 1 ELSE 0 END) AS dislikes,
        SUM(CASE WHEN v.value=0 THEN 1 ELSE 0 END) AS neutral,COUNT(v.user_id) AS total
        FROM review_items i LEFT JOIN review_votes v ON v.item_id=i.id AND v.playlist_id=?
        GROUP BY i.id ORDER BY dislikes DESC,likes ASC,total DESC,i.created DESC''',(playlist_id,)).fetchall()
    return [dict(r) for r in rows if r['id'] in eligible and (proposals_only or r['total'])]
