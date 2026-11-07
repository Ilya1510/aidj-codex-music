"""Small, content-addressed DJ metadata and an explicit owner's preparation queue."""
import json
import math
import re
import time

VERSION=1


def validate(value,duration):
    if not isinstance(value,dict):raise ValueError('Invalid DJ metadata')
    cues=[]
    for cue in value.get('cues',[])[:12]:
        if not isinstance(cue,dict):raise ValueError('Invalid cue')
        numbers=[cue.get(k) for k in ('start','end','bpm','fade')]
        if any(type(n) not in (int,float) or not math.isfinite(n) for n in numbers):raise ValueError('Invalid cue numbers')
        start,end,bpm,fade=numbers
        if not (0<=start<end<=duration+.1 and end-start>=30 and 60<=bpm<=210 and .3<=fade<=8):raise ValueError('Invalid cue bounds')
        clean=dict(zip(('start','end','bpm','fade'),numbers))
        if 'grid' in cue:
            grid=cue['grid']
            if type(grid) not in (int,float) or not math.isfinite(grid) or not 0<=grid<60/bpm:raise ValueError('Invalid beat phase')
            clean['grid']=grid
        cues.append(clean)
    return {'version':VERSION,'cues':cues,'method':'listened' if value.get('method')=='listened' else 'analysed'}


def enqueue(db,body):
    from .playlists import PlaylistError
    ids=body.get('trackIds')
    if not isinstance(ids,list) or len(ids)>2000 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{24}',i) for i in ids):
        raise PlaylistError('Некорректный список песен.')
    count=0
    for aid in dict.fromkeys(ids):
        row=db.execute('SELECT md5,bytes,duration FROM web_assets WHERE id=?',(aid,)).fetchone()
        if not row or not 30<=row['duration']<=600 or row['bytes']>40*1024**2:continue
        count+=db.execute('INSERT OR IGNORE INTO dj_analysis(md5,asset_id,status,updated) VALUES (?,?,?,?)',
                         (row['md5'],aid,'pending',time.time())).rowcount
    db.commit();return {'queued':count}


def complete(db,md5,payload,duration):
    value=validate(payload,duration)
    db.execute("UPDATE dj_analysis SET status='complete',payload=?,updated=? WHERE md5=?",(json.dumps(value),time.time(),md5));db.commit()


def metadata(db):
    out={}
    for row in db.execute("SELECT md5,payload FROM dj_analysis WHERE status='complete'"):
        try:out[row['md5']]=json.loads(row['payload'])
        except (ValueError,TypeError):continue
    return out
