"""Owner-only listening labels for physical DJ boundaries, with private backups.

A point is identified by original MD5 and millisecond, shared by its entry/exit
roles. Listening approval is recorded separately and never fabricates ASR proof.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time

from .store import meta_get


class ReviewError(ValueError):pass


def ensure_owner(db,user_id,chat_id):
    if meta_get(db,'owner_id')!=str(user_id) or meta_get(db,'owner_chat_id')!=str(chat_id):
        raise ReviewError('Разметка точек DJ доступна только владельцу в личном чате.')


def point_id(md5,point_ms):
    return hashlib.sha256((md5+':'+str(point_ms)).encode()).hexdigest()[:24]


def rejected_points(db,md5):
    return [row[0] for row in db.execute("SELECT p.point_ms FROM dj_boundary_points p JOIN dj_boundary_votes v ON v.point_id=p.id WHERE p.md5=? AND v.value=-1",(md5,))]


def _asset(db,md5):return db.execute('SELECT * FROM web_assets WHERE md5=? ORDER BY rowid DESC LIMIT 1',(md5,)).fetchone()


def owned_card(db,card_id,user_id,chat_id,states=None):
    ensure_owner(db,user_id,chat_id)
    row=db.execute('''SELECT c.*,p.md5,p.point_ms,p.roles,p.quality,p.status,
        v.value AS vote FROM dj_boundary_cards c JOIN dj_boundary_points p ON p.id=c.point_id
        LEFT JOIN dj_boundary_votes v ON v.point_id=p.id WHERE c.id=? AND c.user_id=? AND c.chat_id=?''',
        (card_id,user_id,chat_id)).fetchone()
    if not row or states is not None and row['state'] not in states:raise ReviewError('Эта карточка DJ уже закрыта или недоступна.')
    card=dict(row);card['roles']=json.loads(card['roles']);card['position']=card['point_ms']/1000
    asset=_asset(db,card['md5'])
    card.update(asset_id=asset['id'] if asset else None,title=asset['title'] if asset else 'Песня',artist=asset['artist'] if asset else '',duration=asset['duration'] if asset else 0)
    return card


def _candidate(db):
    # Stream one source's small graph at a time. Store only the selected point;
    # do not materialize the library's complete Cartesian product or point set.
    query='''SELECT a.md5,COALESCE(s.payload,a.payload) AS raw FROM dj_analysis a
        LEFT JOIN dj_boundary_sources s ON s.md5=a.md5
        WHERE COALESCE(s.payload,a.payload) IS NOT NULL AND EXISTS(SELECT 1 FROM web_assets w WHERE w.md5=a.md5)
        ORDER BY CASE WHEN json_extract(COALESCE(s.payload,a.payload),'$.needsReview')=1 THEN 0
          WHEN json_extract(COALESCE(s.payload,a.payload),'$.reviewRecommended')=1 THEN 1 ELSE 2 END,
        COALESCE((SELECT MAX(c.updated) FROM dj_boundary_cards c JOIN dj_boundary_points p ON p.id=c.point_id WHERE p.md5=a.md5),0),a.updated DESC,a.md5'''
    for graph_key,quality in (('acousticAnchors','acoustic'),('anchors','word')):
        for source in db.execute(query):
            asset=_asset(db,source['md5'])
            if asset is None:continue
            value=json.loads(source['raw']);graph=value.get(graph_key,{})
            points={}
            for role,key in (('entry','entries'),('exit','exits')):
                for row in graph.get(key,[]):
                    if not row or type(row[0]) not in (int,float) or not math.isfinite(row[0]):continue
                    ms=round(row[0]*1000)
                    if not 5000<=ms<=round(asset['duration']*1000)-5000:continue
                    points.setdefault(ms,set()).add(role)
            rejected=rejected_points(db,source['md5'])
            reviewed=db.execute("SELECT COUNT(*) FROM dj_boundary_cards c JOIN dj_boundary_points p ON p.id=c.point_id WHERE p.md5=? AND c.state IN ('voted','skipped')",(source['md5'],)).fetchone()[0]
            target=asset['duration']*1000*(.55,.25,.8,.4)[reviewed%4]
            for ms,roles in sorted(points.items(),key=lambda point:(abs(point[0]-target),point[0])):
                identity=point_id(source['md5'],ms)
                previous=db.execute('SELECT status FROM dj_boundary_points WHERE id=?',(identity,)).fetchone()
                if previous and previous[0]!='unreviewed':continue
                if db.execute('''SELECT 1 FROM dj_boundary_points p JOIN dj_boundary_cards c ON c.point_id=p.id
                    WHERE p.md5=? AND abs(p.point_ms-?)<500 AND c.state IN ('preparing','open','voted','skipped') LIMIT 1''',(source['md5'],ms)).fetchone():continue
                if any(abs(ms-bad)<=20 for bad in rejected):continue
                # One vote covers both roles when the exact same physical point
                # appears in another graph as well.
                for other in ('anchors','acousticAnchors'):
                    for role,key in (('entry','entries'),('exit','exits')):
                        if any(round(row[0]*1000)==ms for row in value.get(other,{}).get(key,[])):roles.add(role)
                return source['md5'],ms,sorted(roles),quality
    return None


def observe_point(db,identity,md5,point_ms):
    """Archive the exact acoustic rows when the listening card is selected.

    Metadata may be regenerated before the owner votes. A point label alone is
    not enough to reconstruct its observed confidence or final-word fade.
    This function stays inside the caller's transaction and never invents rows.
    """
    if not isinstance(md5,str) or not re.fullmatch('[a-f0-9]{32}',md5) or type(point_ms) is not int or point_ms<0 or identity!=point_id(md5,point_ms):return
    if db.execute('SELECT 1 FROM dj_boundary_observations WHERE point_id=?',(identity,)).fetchone():return
    source=db.execute('SELECT payload FROM dj_boundary_sources WHERE md5=?',(md5,)).fetchone()
    if source is None:source=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(md5,)).fetchone()
    asset=_asset(db,md5)
    if not source or not source[0] or asset is None:return
    try:
        from .dj import validate_anchors
        graph=validate_anchors(json.loads(source[0]).get('acousticAnchors'),asset['duration'])
        if graph.get('quality')!='acoustic' or graph.get('wordGuarded') is not False:return
    except (TypeError,ValueError,AttributeError):return
    saved={**graph,'md5':md5,'point_ms':point_ms}
    for role in ('entries','exits'):saved[role]=[row for row in graph[role] if round(row[0]*1000)==point_ms]
    if not saved['entries'] and not saved['exits']:return
    db.execute('INSERT OR IGNORE INTO dj_boundary_observations VALUES (?,?)',(identity,json.dumps(saved)))


def approved_observations(db,md5,duration):
    """Yield bounded original graphs for positively labelled owner points."""
    from .dj import validate_anchors
    query="""SELECT p.id,p.point_ms,o.payload FROM dj_boundary_points p
        JOIN dj_boundary_votes v ON v.point_id=p.id JOIN dj_boundary_observations o ON o.point_id=p.id
        WHERE p.md5=? AND v.value=1
        AND v.user_id=CAST((SELECT value FROM meta WHERE key='owner_id') AS INTEGER)
        AND v.chat_id=CAST((SELECT value FROM meta WHERE key='owner_chat_id') AS INTEGER)
        ORDER BY p.point_ms"""
    for row in db.execute(query,(md5,)):
        try:
            if len(row['payload'])>65536:continue
            saved=json.loads(row['payload'])
            if saved.get('md5')!=md5 or saved.get('point_ms')!=row['point_ms'] or row['id']!=point_id(md5,row['point_ms']):continue
            graph=validate_anchors(saved,duration)
            if graph.get('quality')!='acoustic' or graph.get('wordGuarded') is not False:continue
            if any(round(point[0]*1000)!=row['point_ms'] for role in ('entries','exits') for point in graph[role]):continue
            yield graph
        except (TypeError,ValueError,AttributeError):continue


def next_card(db,event,user_id,chat_id):
    ensure_owner(db,user_id,chat_id);event='dj-review:'+str(event if event is not None else secrets.token_hex(12))[:160]
    db.execute('BEGIN IMMEDIATE')
    try:
        previous=db.execute('SELECT payload FROM jobs WHERE update_id=?',(event,)).fetchone()
        if previous:
            card=owned_card(db,json.loads(previous[0])['card_id'],user_id,chat_id);db.commit();return card,False
        active=db.execute("SELECT id FROM dj_boundary_cards WHERE user_id=? AND state IN ('preparing','open')",(user_id,)).fetchone()
        if active:
            card=owned_card(db,active[0],user_id,chat_id);db.commit();return card,False
        db.execute('UPDATE dj_boundary_cards SET comment_pending=0 WHERE user_id=?',(user_id,))
        candidate=_candidate(db)
        if candidate is None:db.commit();return None,False
        md5,ms,roles,quality=candidate;identity=point_id(md5,ms);now=time.time();card_id=secrets.token_hex(12)
        db.execute('''INSERT INTO dj_boundary_points VALUES (?,?,?,?,?,'unreviewed',?,?) ON CONFLICT(id)
            DO UPDATE SET roles=excluded.roles,quality=excluded.quality,updated=excluded.updated''',(identity,md5,ms,json.dumps(roles),quality,now,now))
        observe_point(db,identity,md5,ms)
        db.execute("INSERT INTO dj_boundary_cards(id,point_id,user_id,chat_id,created,updated) VALUES (?,?,?,?,?,?)",(card_id,identity,user_id,chat_id,now,now))
        db.execute('INSERT INTO jobs(update_id,chat_id,user_id,message_id,query,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                   (event,chat_id,user_id,0,'Проверка точки DJ',json.dumps({'client':'dj-review','card_id':card_id}),now,now))
        db.commit();return owned_card(db,card_id,user_id,chat_id),True
    except BaseException:db.rollback();raise


def review_next(db,user_id=None,chat_id=None,event=None):
    user_id=user_id if user_id is not None else meta_get(db,'owner_id');chat_id=chat_id if chat_id is not None else meta_get(db,'owner_chat_id')
    return next_card(db,event,user_id,chat_id)


def mark_open(db,card_id,user_id,chat_id,message_id,telegram_file_id=None):
    owned_card(db,card_id,user_id,chat_id,('preparing',))
    db.execute("UPDATE dj_boundary_cards SET state='open',message_id=?,telegram_file_id=?,updated=? WHERE id=? AND state='preparing'",(message_id,telegram_file_id,time.time(),card_id));db.commit()
    return owned_card(db,card_id,user_id,chat_id)


def mark_failed(db,card_id,user_id,chat_id):
    owned_card(db,card_id,user_id,chat_id)
    db.execute("UPDATE dj_boundary_cards SET state='failed',updated=? WHERE id=? AND state='preparing'",(time.time(),card_id));db.commit()


def vote(db,card_id,user_id,chat_id,value):
    ensure_owner(db,user_id,chat_id)
    if type(value) is not int or value not in (-1,1):raise ReviewError('Выбери «хорошая точка» или «плохая точка».')
    db.execute('BEGIN IMMEDIATE')
    try:
        card=owned_card(db,card_id,user_id,chat_id,('open','voted'));now=time.time()
        observe_point(db,card['point_id'],card['md5'],card['point_ms'])
        db.execute('''INSERT INTO dj_boundary_votes VALUES (?,?,?,?,?) ON CONFLICT(point_id)
            DO UPDATE SET user_id=excluded.user_id,chat_id=excluded.chat_id,value=excluded.value,updated=excluded.updated''',(card['point_id'],user_id,chat_id,value,now))
        db.execute('UPDATE dj_boundary_points SET status=?,updated=? WHERE id=?',('good' if value==1 else 'bad',now,card['point_id']))
        db.execute("UPDATE dj_boundary_cards SET state='voted',updated=? WHERE id=?",(now,card_id))
        source=db.execute('SELECT payload FROM dj_boundary_sources WHERE md5=?',(card['md5'],)).fetchone()
        if source is None:source=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(card['md5'],)).fetchone()
        if source and source[0] and card['duration']:
            from .dj import complete
            complete(db,card['md5'],json.loads(source[0]),card['duration'],commit=False)
        db.commit();return owned_card(db,card_id,user_id,chat_id)
    except BaseException:db.rollback();raise


def prompt_comment(db,card_id,user_id,chat_id):
    card=owned_card(db,card_id,user_id,chat_id,('open','voted'))
    db.execute('UPDATE dj_boundary_cards SET comment_pending=0 WHERE user_id=?',(user_id,))
    db.execute('UPDATE dj_boundary_cards SET comment_pending=1,updated=? WHERE id=?',(time.time(),card_id));db.commit();return card


def pending_comment(db,user_id,chat_id):
    ensure_owner(db,user_id,chat_id)
    row=db.execute("SELECT id FROM dj_boundary_cards WHERE user_id=? AND chat_id=? AND comment_pending=1 AND state IN ('open','voted') ORDER BY updated DESC LIMIT 1",(user_id,chat_id)).fetchone()
    return owned_card(db,row[0],user_id,chat_id) if row else None


def cancel_comment(db,user_id,chat_id):
    ensure_owner(db,user_id,chat_id);db.execute('UPDATE dj_boundary_cards SET comment_pending=0 WHERE user_id=?',(user_id,));db.commit()


def _comment_event(event):
    if event is None:return None
    if type(event) not in (int,str):raise ReviewError('Не удалось определить сообщение комментария.')
    value=str(event)
    if not re.fullmatch('[A-Za-z0-9:_-]{1,128}',value):raise ReviewError('Не удалось определить сообщение комментария.')
    return value


def comment_handled(db,event,user_id,chat_id):
    ensure_owner(db,user_id,chat_id);event=_comment_event(event)
    if event is None:return False
    return db.execute('SELECT 1 FROM dj_boundary_comments WHERE event_id=? AND user_id=? AND chat_id=?',(event,user_id,chat_id)).fetchone() is not None


def comment(db,card_id,user_id,chat_id,text,event=None):
    ensure_owner(db,user_id,chat_id);event=_comment_event(event)
    db.execute('BEGIN IMMEDIATE')
    try:
        # Telegram can replay this message if the acknowledgement failed. The
        # old event must not consume or clear a newer card's comment prompt.
        if event is not None and comment_handled(db,event,user_id,chat_id):
            card=owned_card(db,card_id,user_id,chat_id);db.commit();return card
        card=owned_card(db,card_id,user_id,chat_id,('open','voted'))
        if not isinstance(text,str) or not 1<=len(text.strip())<=2000:raise ReviewError('Комментарий: от 1 до 2000 символов.')
        db.execute('INSERT INTO dj_boundary_comments(point_id,user_id,chat_id,text,created,event_id) VALUES (?,?,?,?,?,?)',
                   (card['point_id'],user_id,chat_id,text.strip(),time.time(),event))
        db.execute('UPDATE dj_boundary_cards SET comment_pending=0,updated=? WHERE id=?',(time.time(),card_id))
        db.commit();return owned_card(db,card_id,user_id,chat_id)
    except BaseException:db.rollback();raise


def skip(db,card_id,user_id,chat_id):
    card=owned_card(db,card_id,user_id,chat_id,('preparing','open'))
    db.execute("UPDATE dj_boundary_cards SET state='skipped',comment_pending=0,updated=? WHERE id=?",(time.time(),card_id))
    db.execute("UPDATE dj_boundary_points SET status='skipped',updated=? WHERE id=? AND status='unreviewed'",(time.time(),card['point_id']))
    db.execute("UPDATE jobs SET status='cancelled',stage='Отменено',updated=? WHERE status='queued' AND json_extract(payload,'$.client')='dj-review' AND json_extract(payload,'$.card_id')=?",(time.time(),card_id));db.commit()
    return owned_card(db,card_id,user_id,chat_id)


def finish_session(db,user_id,chat_id):
    ensure_owner(db,user_id,chat_id)
    db.execute("UPDATE dj_boundary_cards SET state='closed',comment_pending=0,updated=? WHERE user_id=? AND state IN ('preparing','open')",(time.time(),user_id))
    db.execute('UPDATE dj_boundary_cards SET comment_pending=0 WHERE user_id=?',(user_id,))
    db.execute("UPDATE jobs SET status='cancelled',stage='Отменено',updated=? WHERE user_id=? AND status='queued' AND json_extract(payload,'$.client')='dj-review'",(time.time(),user_id));db.commit()


def recover_jobs(db):
    db.execute("""UPDATE dj_boundary_cards SET state='failed',updated=? WHERE state='preparing' AND NOT EXISTS
        (SELECT 1 FROM jobs j WHERE json_extract(j.payload,'$.client')='dj-review' AND json_extract(j.payload,'$.card_id')=dj_boundary_cards.id AND j.status IN ('queued','processing'))""",(time.time(),));db.commit()


def render_clip(settings,db,card,work,disk=None,ffmpeg_path=None):
    from .audio import duration,ffmpeg
    from .disk import Disk,md5
    card=owned_card(db,card['id'],card['user_id'],card['chat_id'],('preparing','open'))
    asset=_asset(db,card['md5'])
    if asset is None or asset['bytes']>40*1024**2:raise ReviewError('Исходная песня недоступна для проверки.')
    if shutil.disk_usage(settings.state).free<256*1024**2:raise ReviewError('На сервере недостаточно места для проверки точки.')
    center=card['position']
    if not 0<=center<=asset['duration']:raise ReviewError('Точка находится вне исходной песни.')
    work=Path(work);source=work/('dj-source'+Path(asset['remote_path']).suffix)
    disk=disk or Disk(settings);disk.download(asset['remote_path'],source,expected_md5=card['md5'])
    if source.stat().st_size!=asset['bytes']:raise ReviewError('Исходная песня изменилась на Диске.')
    binary=ffmpeg_path or ffmpeg()
    def encode(target,start,padding,seconds):
        available=min(seconds-padding,max(0,asset['duration']-start))
        command=[binary,'-nostdin','-v','error','-threads','1','-filter_threads','1','-max_alloc','67108864']
        if available>0:
            # Accurate input seeking + re-encoding starts from the selected
            # original instant. No copied preceding AAC/MP3 frame or fade.
            command+=['-ss',str(start),'-t',str(available),'-i',str(source)]
        else:
            # At the physical end there is no following audio to pad. Generate
            # silence instead of accidentally seeking to earlier music.
            command+=['-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t',str(seconds)]
        command+=['-vn','-af',f'adelay={round(padding*1000)}:all=1,apad=whole_dur={seconds},atrim=duration={seconds},asetpts=PTS-STARTPTS',
                  '-c:a','aac','-b:a','192k','-ar','44100','-ac','2','-threads','1','-movflags','+faststart','-fs','5242880','-y',str(target)]
        try:process=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45)
        except (OSError,subprocess.TimeoutExpired):raise ReviewError('Не удалось подготовить проверочный фрагмент.') from None
        if process.returncode or not target.is_file() or target.stat().st_size>5*1024**2:raise ReviewError('Не удалось подготовить проверочный фрагмент.')
        if abs(duration(target)-seconds)>.08:raise ReviewError('Проверочный фрагмент не прошёл проверку длительности.')
    context=work/'dj-point.m4a';forward=work/'dj-from-point.m4a'
    encode(context,max(0,center-5),max(0,5-center),10)
    encode(forward,center,0,5)
    def result(file,seconds,point_position,label):
        return {'track':{'source':'dj-review','id':card['point_id'],'title':card['title']+label+str(round(center,3))+' с','artist':card['artist'],'url':'https://t.me/aidj_codex_music_bot'},
                'local_path':str(file),'duration':float(seconds),'md5':md5(file),'bytes':file.stat().st_size,'telegram_file_id':None,
                'pointPosition':float(point_position),'sourcePosition':center}
    answer=result(context,10,5,' · точка ')
    answer['fromPoint']=result(forward,5,0,' · с точки ')
    return answer
