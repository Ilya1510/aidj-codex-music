"""Small, content-addressed DJ metadata and an explicit owner's preparation queue."""
import json
import math
import re
import time

VERSION=3
ACTIVE_LIMIT=10


def validate(value,duration):
    if not isinstance(value,dict):raise ValueError('Invalid DJ metadata')
    def clean_cues(items,minimum,maximum=600):
        if not isinstance(items,list):raise ValueError('Invalid cues')
        result=[]
        for cue in items[:12]:
            if not isinstance(cue,dict):raise ValueError('Invalid cue')
            numbers=[cue.get(k) for k in ('start','end','bpm','fade')]
            if any(type(n) not in (int,float) or not math.isfinite(n) for n in numbers):raise ValueError('Invalid cue numbers')
            start,end,bpm,fade=numbers
            if not (0<=start<end<=duration+.1 and minimum<=end-start<=maximum and (bpm==0 or 60<=bpm<=210) and .5<=fade<=8):raise ValueError('Invalid cue bounds')
            clean=dict(zip(('start','end','bpm','fade'),numbers))
            # Legacy entry timings were not checked against a whole vocal phrase.
            # Only the new explicit evidence marker enables nonzero automatic starts.
            clean['start']=start if cue.get('entryVerified') is True else 0
            if cue.get('entryVerified') is True:clean['entryVerified']=True
            if 'grid' in cue:
                grid=cue['grid']
                if bpm==0 or type(grid) not in (int,float) or not math.isfinite(grid) or not 0<=grid<60/bpm:raise ValueError('Invalid beat phase')
                clean['grid']=grid
            if 'mix' in cue:
                mix=cue['mix']
                if type(mix) not in (int,float) or not math.isfinite(mix) or not max(1.5,fade)<=mix<=8:raise ValueError('Invalid overlap')
                clean['mix']=mix
            if 'energy' in cue:
                energy=cue['energy']
                if type(energy) not in (int,float) or not math.isfinite(energy) or not 0<=energy<=1:raise ValueError('Invalid energy')
                clean['energy']=energy
            result.append(clean)
        return result
    remix=clean_cues(value.get('remix',[]),8,30)
    if any(c.get('entryVerified') is not True for c in remix):raise ValueError('Unverified remix entry')
    result={'version':VERSION,'cues':clean_cues(value.get('cues',[]),30),'remix':remix,'method':'listened' if value.get('method')=='listened' else 'analysed'}
    if value.get('reason') in ('no-safe-gap','few-recognised-words','too-short','kept-previous'):result['reason']=value['reason']
    return result


def enqueue(db,body):
    from .playlists import PlaylistError
    ids=body.get('trackIds')
    if not isinstance(ids,list) or len(ids)>2000 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{24}',i) for i in ids):
        raise PlaylistError('Некорректный список песен.')
    next_ids=body.get('nextTrackIds')
    if next_ids is not None and (not isinstance(next_ids,list) or len(next_ids)>ACTIVE_LIMIT or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{24}',i) for i in next_ids)):
        raise PlaylistError('Некорректная очередь воспроизведения.')
    # Old APKs may still send the entire playlist. Bound work here, where all
    # clients share the same queue. An already running job uses one of ten slots.
    wanted=list(dict.fromkeys(next_ids if next_ids is not None else ids))[:ACTIVE_LIMIT]
    working=[r[0] for r in db.execute("SELECT asset_id FROM dj_analysis WHERE status='working' LIMIT 1")]
    keep=list(dict.fromkeys(working+wanted))[:ACTIVE_LIMIT]
    _trim_queue(db,keep)
    count=0
    for aid in keep:
        row=db.execute('SELECT md5,bytes,duration FROM web_assets WHERE id=?',(aid,)).fetchone()
        if not row or not 30<=row['duration']<=600 or row['bytes']>40*1024**2:continue
        previous=db.execute('SELECT status,payload FROM dj_analysis WHERE md5=?',(row['md5'],)).fetchone()
        if previous:
            payload=json.loads(previous['payload']) if previous['payload'] else {}
            # Refine an old result once, only when it enters the audible window.
            if previous['status']=='complete' and payload.get('version',0)<VERSION and payload.get('method')!='listened':
                count+=db.execute("UPDATE dj_analysis SET status='pending',updated=? WHERE md5=?",(time.time(),row['md5'])).rowcount
        else:
            count+=db.execute('INSERT INTO dj_analysis(md5,asset_id,status,updated) VALUES (?,?,?,?)',
                             (row['md5'],aid,'pending',time.time())).rowcount
    db.execute('DELETE FROM dj_priority')
    for rank,aid in enumerate(keep):
        row=db.execute('SELECT md5 FROM web_assets WHERE id=?',(aid,)).fetchone()
        if row:db.execute('INSERT OR IGNORE INTO dj_priority VALUES (?,?,?)',(row['md5'],rank,time.time()))
    db.commit();return {'queued':count}


def _trim_queue(db,keep):
    # Completed evidence remains cached even when a track leaves the active
    # window. Only requests that have not begun are withdrawn.
    outside='asset_id NOT IN ('+','.join('?' for _ in keep)+')' if keep else '1'
    db.execute(f"UPDATE dj_analysis SET status='complete' WHERE status='pending' AND payload IS NOT NULL AND {outside}",keep)
    return db.execute(f"DELETE FROM dj_analysis WHERE status='pending' AND payload IS NULL AND {outside}",keep).rowcount


def complete(db,md5,payload,duration,*,commit=True):
    value=validate(payload,duration)
    previous=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(md5,)).fetchone()
    if previous and previous[0]:
        old=validate(json.loads(previous[0]),duration)
        for field in ('cues','remix'):
            if not value[field] and old[field]:value={**value,field:old[field],'reason':'kept-previous'}
    db.execute("UPDATE dj_analysis SET status='complete',payload=?,updated=? WHERE md5=?",(json.dumps(value),time.time(),md5))
    if commit:db.commit()


def metadata(db):
    out={}
    for row in db.execute("SELECT md5,payload FROM dj_analysis WHERE payload IS NOT NULL"):
        try:out[row['md5']]=json.loads(row['payload'])
        except (ValueError,TypeError):continue
    return out


def compatible_v2(value):
    """APK 18 forces entry zero; never broaden a verified middle excerpt."""
    return {**value,'cues':[c for c in value.get('cues',[]) if c['start']==0],'remix':[]}


def legacy(value):
    """APK <=17 cannot hold the outgoing vocal during a long incoming fade."""
    return {**compatible_v2(value),'cues':[{k:v for k,v in c.items() if k!='mix'} for c in compatible_v2(value).get('cues',[]) if c['bpm']>=60 and c['fade']>=1.2]}


def next_job(db):
    return db.execute("""SELECT j.* FROM dj_analysis j LEFT JOIN dj_priority p ON p.md5=j.md5 AND p.requested>?
        WHERE j.status='pending' AND NOT EXISTS (SELECT 1 FROM dj_batch_jobs b WHERE b.md5=j.md5 AND b.state='running' AND b.lease>?) ORDER BY p.md5 IS NULL,p.rank,j.updated LIMIT 1""",(time.time()-3600,time.time())).fetchone()


def heartbeat(db,phase):
    db.execute('INSERT OR REPLACE INTO dj_runtime VALUES (1,?,?)',(time.time(),phase));db.commit()


def service_state(db):
    row=db.execute('SELECT * FROM dj_runtime WHERE id=1').fetchone()
    if not row:return {'state':'not-started'}
    if row['phase']=='stopped':return {'state':'stopped'}
    if time.time()-row['heartbeat']>90:return {'state':'stale'}
    return {'state':row['phase']}


def upgrade_queue(db):
    # Never populate the whole library at startup. Retain only a fresh audible
    # window requested by the player, and keep every finished result/evidence.
    keep=[r[0] for r in db.execute("SELECT j.asset_id FROM dj_priority p JOIN dj_analysis j ON j.md5=p.md5 WHERE p.requested>? ORDER BY p.rank LIMIT ?",(time.time()-3600,ACTIVE_LIMIT))]
    removed=_trim_queue(db,keep)
    db.execute('DELETE FROM dj_priority WHERE requested<?',(time.time()-3600,));db.commit()
    return removed
