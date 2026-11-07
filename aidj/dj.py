"""Small, content-addressed DJ metadata and an explicit owner's preparation queue."""
import json
import math
import re
import time

VERSION=2


def validate(value,duration):
    if not isinstance(value,dict):raise ValueError('Invalid DJ metadata')
    cues=[]
    for cue in value.get('cues',[])[:12]:
        if not isinstance(cue,dict):raise ValueError('Invalid cue')
        numbers=[cue.get(k) for k in ('start','end','bpm','fade')]
        if any(type(n) not in (int,float) or not math.isfinite(n) for n in numbers):raise ValueError('Invalid cue numbers')
        start,end,bpm,fade=numbers
        if not (0<=start<end<=duration+.1 and end-start>=30 and (bpm==0 or 60<=bpm<=210) and .5<=fade<=8):raise ValueError('Invalid cue bounds')
        clean=dict(zip(('start','end','bpm','fade'),numbers))
        if 'grid' in cue:
            grid=cue['grid']
            if bpm==0 or type(grid) not in (int,float) or not math.isfinite(grid) or not 0<=grid<60/bpm:raise ValueError('Invalid beat phase')
            clean['grid']=grid
        # Automatic entries keep the beginning of the original. Manual excerpts
        # are independent and still take precedence in the player.
        clean['start']=0
        if 'mix' in cue:
            mix=cue['mix']
            if type(mix) not in (int,float) or not math.isfinite(mix) or not max(1.5,fade)<=mix<=8:raise ValueError('Invalid overlap')
            clean['mix']=mix
        cues.append(clean)
    result={'version':VERSION,'cues':cues,'method':'listened' if value.get('method')=='listened' else 'analysed'}
    if value.get('reason') in ('no-safe-gap','few-recognised-words','too-short','kept-previous'):result['reason']=value['reason']
    return result


def enqueue(db,body):
    from .playlists import PlaylistError
    ids=body.get('trackIds')
    if not isinstance(ids,list) or len(ids)>2000 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{24}',i) for i in ids):
        raise PlaylistError('Некорректный список песен.')
    next_ids=body.get('nextTrackIds')
    if next_ids is not None and (not isinstance(next_ids,list) or len(next_ids)>8 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{24}',i) for i in next_ids)):
        raise PlaylistError('Некорректная очередь воспроизведения.')
    count=0
    for aid in dict.fromkeys(ids):
        row=db.execute('SELECT md5,bytes,duration FROM web_assets WHERE id=?',(aid,)).fetchone()
        if not row or not 30<=row['duration']<=600 or row['bytes']>40*1024**2:continue
        count+=db.execute('INSERT OR IGNORE INTO dj_analysis(md5,asset_id,status,updated) VALUES (?,?,?,?)',
                         (row['md5'],aid,'pending',time.time())).rowcount
    if next_ids is not None:
        db.execute('DELETE FROM dj_priority')
        for rank,aid in enumerate(dict.fromkeys(next_ids)):
            row=db.execute('SELECT md5 FROM web_assets WHERE id=?',(aid,)).fetchone()
            if row:db.execute('INSERT OR IGNORE INTO dj_priority VALUES (?,?,?)',(row['md5'],rank,time.time()))
    db.commit();return {'queued':count}


def complete(db,md5,payload,duration):
    value=validate(payload,duration)
    previous=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(md5,)).fetchone()
    if not value['cues'] and previous and previous[0]:
        old=validate(json.loads(previous[0]),duration)
        if old['cues']:value={**old,'reason':'kept-previous'}
    db.execute("UPDATE dj_analysis SET status='complete',payload=?,updated=? WHERE md5=?",(json.dumps(value),time.time(),md5));db.commit()


def metadata(db):
    out={}
    for row in db.execute("SELECT md5,payload FROM dj_analysis WHERE payload IS NOT NULL"):
        try:out[row['md5']]=json.loads(row['payload'])
        except (ValueError,TypeError):continue
    return out


def legacy(value):
    """APK <=17 cannot hold the outgoing vocal during a long incoming fade."""
    return {**value,'cues':[{k:v for k,v in c.items() if k!='mix'} for c in value.get('cues',[]) if c['bpm']>=60 and c['fade']>=1.2]}


def next_job(db):
    return db.execute("""SELECT j.* FROM dj_analysis j LEFT JOIN dj_priority p ON p.md5=j.md5 AND p.requested>?
        WHERE j.status='pending' ORDER BY p.md5 IS NULL,p.rank,j.updated LIMIT 1""",(time.time()-3600,)).fetchone()


def heartbeat(db,phase):
    db.execute('INSERT OR REPLACE INTO dj_runtime VALUES (1,?,?)',(time.time(),phase));db.commit()


def service_state(db):
    row=db.execute('SELECT * FROM dj_runtime WHERE id=1').fetchone()
    if not row:return {'state':'not-started'}
    if row['phase']=='stopped':return {'state':'stopped'}
    if time.time()-row['heartbeat']>90:return {'state':'stale'}
    return {'state':row['phase']}


def upgrade_queue(db):
    # Reconsider only old completed empty results after the selection rule changes.
    # Existing usable/listened cues stay available, including during preparation.
    count=db.execute("""UPDATE dj_analysis SET status='pending',updated=0
        WHERE status='complete' AND COALESCE(json_extract(payload,'$.version'),0)<2
        AND json_array_length(payload,'$.cues')=0""").rowcount
    db.commit()
    for row in db.execute("""SELECT j.md5,j.payload,a.duration FROM dj_analysis j JOIN web_assets a ON a.id=j.asset_id
        WHERE j.payload IS NOT NULL AND COALESCE(json_extract(j.payload,'$.version'),0)<2
        AND json_array_length(j.payload,'$.cues')>0"""):
        complete(db,row['md5'],json.loads(row['payload']),row['duration'])
    return count
