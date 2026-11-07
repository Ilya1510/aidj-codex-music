"""Small, content-addressed DJ metadata and an explicit owner's preparation queue."""
import json
from bisect import bisect_left
import math
import re
import time

VERSION=3
ACTIVE_LIMIT=10
MAX_FULL_DURATION=1800


def validate_anchors(graph,duration):
    if not isinstance(graph,dict) or type(graph.get('version')) is not int or graph['version']!=1:raise ValueError('Invalid anchor graph')
    acoustic=graph.get('quality')=='acoustic' and graph.get('wordGuarded') is False and graph.get('needsReview') is True
    if graph.get('wordGuarded') is not True and not acoustic:raise ValueError('Invalid anchor quality')
    def number(value,lo,hi):
        if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError('Invalid anchor number')
        return value
    bpm=number(graph.get('bpm'),0,210)
    if bpm and bpm<60:raise ValueError('Invalid anchor tempo')
    out={'version':1,'wordGuarded':not acoustic,'bpm':bpm}
    if acoustic:out.update(quality='acoustic',needsReview=True)
    for key,width in (('entries',2),('exits',3)):
        rows=graph.get(key)
        if not isinstance(rows,list) or len(rows)>256:raise ValueError('Invalid anchor list')
        clean=[];previous=-1
        for row in rows:
            if not isinstance(row,(list,tuple)) or len(row)!=width:raise ValueError('Invalid anchor point')
            t=number(row[0],0,duration+.1)
            if t<=previous:raise ValueError('Unsorted anchor points')
            if key=='exits':
                fade=number(row[1],.5 if acoustic else .15,min(8,t));clean.append([t,fade,number(row[2],0,1)])
            else:clean.append([t,number(row[1],0,1)])
            previous=t
        out[key]=clean
    if 'grid' in graph:
        if not bpm:raise ValueError('Invalid anchor phase')
        out['grid']=number(graph['grid'],0,60/bpm)
        if out['grid']>=60/bpm:raise ValueError('Invalid anchor phase')
    return out


def validate(value,duration):
    if not isinstance(value,dict):raise ValueError('Invalid DJ metadata')
    schema=value.get('cueSchema',3)
    if type(schema) is not int or schema not in (3,4):raise ValueError('Invalid cue schema')
    def clean_cues(items,minimum,maximum=MAX_FULL_DURATION):
        if not isinstance(items,list):raise ValueError('Invalid cues')
        result=[]
        limit=32 if schema==4 and minimum==8 else 12
        for cue in items[:limit]:
            if not isinstance(cue,dict):raise ValueError('Invalid cue')
            numbers=[cue.get(k) for k in ('start','end','bpm','fade')]
            if any(type(n) not in (int,float) or not math.isfinite(n) for n in numbers):raise ValueError('Invalid cue numbers')
            start,end,bpm,fade=numbers
            for marker in ('entryVerified','wordGuarded'):
                if marker in cue and type(cue[marker]) is not bool:raise ValueError('Invalid cue marker')
            guarded=schema==4 and cue.get('entryVerified') is True and cue.get('wordGuarded') is True
            min_fade=(.15 if minimum==8 else .25) if guarded else .5
            if not (0<=start<end<=duration+.1 and minimum<=end-start<=maximum and (bpm==0 or 60<=bpm<=210) and min_fade<=fade<=8):raise ValueError('Invalid cue bounds')
            clean=dict(zip(('start','end','bpm','fade'),numbers))
            # Legacy entry timings were not checked against a whole vocal phrase.
            # Only the new explicit evidence marker enables nonzero automatic starts.
            clean['start']=start if cue.get('entryVerified') is True else 0
            if cue.get('entryVerified') is True:clean['entryVerified']=True
            if guarded:clean['wordGuarded']=True
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
    if schema==4:
        result['cueSchema']=4
        if 'anchors' in value:
            result['anchors']=validate_anchors(value['anchors'],duration)
            if result['anchors']['wordGuarded'] is not True:raise ValueError('Primary graph must be word guarded')
        if 'acousticAnchors' in value:
            result['acousticAnchors']=validate_anchors(value['acousticAnchors'],duration)
            if result['acousticAnchors'].get('quality')!='acoustic':raise ValueError('Invalid acoustic graph')
        for marker in ('needsReview','reviewRecommended','fullFallback'):
            if marker in value:
                if type(value[marker]) is not bool:raise ValueError('Invalid review marker')
                result[marker]=value[marker]
    if value.get('reason') in ('no-safe-gap','no-safe-duration','few-recognised-words','too-short','kept-previous'):result['reason']=value['reason']
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


def filter_boundary_points(payload,rejected_ms):
    """Remove owner-rejected physical boundaries from every playback route.

    Entries/exits share a physical millisecond identity. Keep a 20 ms exclusion
    window for recogniser/grid rounding; never move an accepted boundary or its
    final-word fade budget to manufacture a replacement.
    """
    bad=sorted({value for value in rejected_ms if type(value) is int and value>=0})
    if not bad:return payload
    def rejected(seconds):
        millis=seconds*1000
        index=bisect_left(bad,millis-20-1e-6)
        return index<len(bad) and bad[index]<=millis+20+1e-6
    out={**payload};changed=False
    for field in ('anchors','acousticAnchors'):
        graph=payload.get(field)
        if not graph:continue
        clean={**graph}
        for role in ('entries','exits'):
            rows=graph.get(role,[]);clean[role]=[row for row in rows if not rejected(row[0])]
            changed=changed or len(clean[role])!=len(rows)
        out[field]=clean
    for field in ('cues','remix'):
        cues=payload.get(field,[])
        out[field]=[cue for cue in cues if not rejected(cue['start']) and not rejected(cue['end'])]
        changed=changed or len(out[field])!=len(cues)
    if not changed:return payload
    def usable(graph):
        if not graph:return False
        ends=sorted(row[0] for row in graph.get('exits',[]))
        for start,*_ in graph.get('entries',[]):
            at=bisect_left(ends,start+8-1e-7)
            if at<len(ends) and ends[at]<=start+123+1e-7:return True
        return False
    primary=bool(out.get('cues') or out.get('remix') or usable(out.get('anchors')))
    secondary=usable(out.get('acousticAnchors'))
    if out.get('cueSchema')==4:
        out['needsReview']=bool(out.get('needsReview') or not primary)
        out['reviewRecommended']=True
        out['fullFallback']=not primary and not secondary
    return out


def apply_boundary_feedback(db,md5,payload):
    from .dj_review import rejected_points
    return filter_boundary_points(payload,rejected_points(db,md5))


def preserve_approved_boundaries(db,md5,previous,current,duration):
    """Retain exact owner-approved observations as acoustic alternatives only."""
    from .dj_review import approved_observations,observe_point
    def thin(rows,count):
        if count<=0:return []
        if len(rows)<=count:return rows
        if count==1:return [rows[len(rows)//2]]
        return [rows[round(i*(len(rows)-1)/(count-1))] for i in range(count)]
    protected={'entries':{},'exits':{}}
    basis={}
    # Each archived card contains only its original physical point's rows. Read
    # one card at a time and cap retained rows, never materialize label history.
    for archived in approved_observations(db,md5,duration):
        if not basis:basis=archived
        for role in protected:
            for row in archived[role]:protected[role].setdefault(row[0],row)
            if len(protected[role])>256:
                protected[role]={row[0]:row for row in thin(sorted(protected[role].values(),key=lambda row:row[0]),256)}
    # Upgrade fallback for labels created before observations were archived.
    # Keep acoustic rows in their own graph; word boundaries must not be
    # downgraded or receive a manufactured broader final-word fade.
    old=previous.get('acousticAnchors') or {}
    candidates={round(row[0]*1000) for role in protected for row in old.get(role,[])}
    approved=set()
    if candidates:
        for row in db.execute("""SELECT p.id,p.point_ms FROM dj_boundary_points p JOIN dj_boundary_votes v ON v.point_id=p.id
            WHERE p.md5=? AND v.value=1
            AND v.user_id=CAST((SELECT value FROM meta WHERE key='owner_id') AS INTEGER)
            AND v.chat_id=CAST((SELECT value FROM meta WHERE key='owner_chat_id') AS INTEGER)""",(md5,)):
            if row['point_ms'] in candidates:
                approved.add(row['point_ms'])
                observe_point(db,row['id'],md5,row['point_ms'])
    for role in protected:
        for row in old.get(role,[]):
            if round(row[0]*1000) in approved:protected[role].setdefault(row[0],row)
        if len(protected[role])>256:
            protected[role]={row[0]:row for row in thin(sorted(protected[role].values(),key=lambda row:row[0]),256)}
    if not any(protected.values()):return current
    graph=current.get('acousticAnchors')
    if graph is None:
        graph={'version':1,'wordGuarded':False,'quality':'acoustic','needsReview':True,'bpm':(basis or old).get('bpm',0),'entries':[],'exits':[]}
    graph={**graph}
    for role in protected:
        remaining=[row for row in graph.get(role,[]) if row[0] not in protected[role]]
        graph[role]=sorted(list(protected[role].values())+thin(remaining,256-len(protected[role])),key=lambda row:row[0])
    result={**current,'acousticAnchors':validate_anchors(graph,duration)}
    ends=[row[0] for row in graph['exits']]
    for start,*_ in graph['entries']:
        at=bisect_left(ends,start+8-1e-7)
        if at<len(ends) and ends[at]<=start+123+1e-7:
            result['fullFallback']=False;break
    return result


def complete(db,md5,payload,duration,*,commit=True):
    value=validate(payload,duration)
    # Keep the source before feedback so changing a vote can restore its exact
    # observed points. New analysis still passes through the same rejection set.
    previous=db.execute('SELECT payload FROM dj_boundary_sources WHERE md5=?',(md5,)).fetchone()
    if previous is None:previous=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(md5,)).fetchone()
    if previous and previous[0]:
        old=validate(json.loads(previous[0]),duration)
        if old.get('cueSchema')==4:value['cueSchema']=4
        if value.get('cueSchema')==4:value=preserve_approved_boundaries(db,md5,old,value,duration)
        if old.get('anchors') and not value.get('anchors',{}).get('exits'):value['anchors']=old['anchors']
        for field in ('cues','remix'):
            if not value[field] and old[field]:value['reason']='kept-previous'
            # A preferred narrow fade is visible only to APK 20+. Retain the
            # previous broad-fade alternatives for older players as well.
            def identity(cue):return tuple(round(cue.get(k,-1),6) for k in ('start','end','bpm','fade','grid'))
            limit=32 if field=='remix' and value.get('cueSchema')==4 else 12
            fresh=value[field];fresh_keys={identity(c) for c in fresh}
            # Reserve room for broad fades previously usable by APK 19. A full
            # new narrow-fade pool must not remove its last compatible variants.
            protected=[c for c in old[field] if c['fade']>=.5 and identity(c) not in fresh_keys][:(6 if field=='remix' else 3)]
            combined=[];seen=set()
            for cue in fresh[:limit-len(protected)]+protected+old[field]:
                key=identity(cue)
                if key not in seen:combined.append(cue);seen.add(key)
                if len(combined)>=limit:break
            value[field]=combined
    db.execute('INSERT OR REPLACE INTO dj_boundary_sources(md5,payload,updated) VALUES (?,?,?)',(md5,json.dumps(value),time.time()))
    value=apply_boundary_feedback(db,md5,value)
    db.execute("UPDATE dj_analysis SET status='complete',payload=?,updated=? WHERE md5=?",(json.dumps(value),time.time(),md5))
    if commit:db.commit()


def metadata(db):
    out={}
    for row in db.execute("SELECT md5,payload FROM dj_analysis WHERE payload IS NOT NULL"):
        try:out[row['md5']]=json.loads(row['payload'])
        except (ValueError,TypeError):continue
    return out


def compatible_v3(value):
    """APK 19 supports whole-word entries but requires >=0.5s exit fades."""
    result={**value}
    for field in ('cueSchema','anchors','acousticAnchors','needsReview','reviewRecommended','fullFallback'):result.pop(field,None)
    for field in ('cues','remix'):
        candidates=[{k:v for k,v in c.items() if k!='wordGuarded'} for c in value.get(field,[]) if c['fade']>=.5]
        if field=='remix':
            # APK 19 visits clips sequentially. Keep its old small, separated
            # selection instead of handing it the new densely overlapping pool.
            clips=[]
            for c in candidates:
                if all(abs(c['start']-other['start'])>=6 for other in clips):clips.append(c)
                if len(clips)>=6:break
            candidates=clips
        result[field]=candidates[:12]
    return result


def compatible_v2(value):
    """APK 18 forces entry zero; never broaden a verified middle excerpt."""
    result=compatible_v3(value)
    return {**result,'cues':[c for c in result['cues'] if c['start']==0],'remix':[]}


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
