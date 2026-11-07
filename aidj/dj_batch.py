"""Owner's SSH-only coordinator for full DJ analysis on another computer.

No HTTP endpoint or exported Disk credentials. RPC replies contain only song
metadata and numeric analysis; originals cross the authenticated SSH channel.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import time

from .config import Settings
from .store import connect
from .dj import VERSION, complete, validate

MAX_JSON=1024*1024
LEASE_SECONDS=300


class BatchError(RuntimeError):pass


def ident(value,length):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{'+str(length)+'}',value):raise BatchError('INVALID_ID')
    return value


def stats(db,run):
    counts={r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM dj_batch_jobs WHERE run_id=? GROUP BY state',(run,))}
    ready=db.execute("""SELECT COALESCE(sum(json_array_length(a.payload,'$.cues')>0),0),
        COALESCE(sum(json_array_length(a.payload,'$.remix')>0),0) FROM dj_batch_jobs b
        LEFT JOIN dj_analysis a ON a.md5=b.md5 WHERE b.run_id=? AND b.state='done'""",(run,)).fetchone()
    return {**{k:counts.get(k,0) for k in ('pending','running','done','failed')},'total':sum(counts.values()),
            'regularReady':ready[0],'remixReady':ready[1],'withoutRegular':counts.get('done',0)-ready[0],'withoutRemix':counts.get('done',0)-ready[1]}


def start(db,playlist=None):
    from .web_catalog import sync_assets
    sync_assets(db)
    if playlist is not None and (not isinstance(playlist,str) or len(playlist)>150):raise BatchError('INVALID_PLAYLIST')
    scope='all' if playlist is None else 'playlist:'+playlist
    run=hashlib.sha256(('full:'+str(VERSION)+':'+scope).encode()).hexdigest()[:24]
    if playlist is None:query='SELECT a.* FROM web_assets a GROUP BY a.md5';params=()
    else:
        p=db.execute('SELECT id FROM playlists WHERE name=?',(playlist,)).fetchone()
        if not p:raise BatchError('PLAYLIST_NOT_FOUND')
        query="SELECT a.* FROM web_assets a JOIN playlist_tracks t ON t.md5=a.md5 WHERE t.playlist_id=? AND t.state='complete' GROUP BY a.md5";params=(p[0],)
    db.execute('INSERT OR IGNORE INTO dj_batch_runs VALUES (?,?,?)',(run,scope,time.time()))
    for row in db.execute(query,params):
        if not 30<=row['duration']<=600 or row['bytes']>40*1024**2:continue
        cached=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(row['md5'],)).fetchone()
        evidence=json.loads(cached[0]) if cached else {}
        done=evidence.get('fullComplete') is True and evidence.get('version',0)>=VERSION
        db.execute('INSERT OR IGNORE INTO dj_batch_jobs(run_id,md5,asset_id,state,lease,attempts,updated) VALUES (?,?,?,?,0,0,?)',
                   (run,row['md5'],row['id'],'done' if done else 'pending',time.time()))
    db.commit();return {'runId':run,'stats':stats(db,run)}


def owned(db,body):
    run=ident(body.get('runId'),24);md5=ident(body.get('md5'),32);token=ident(body.get('token'),32)
    row=db.execute('SELECT * FROM dj_batch_jobs WHERE run_id=? AND md5=?',(run,md5)).fetchone()
    if not row or row['state']!='running' or not secrets.compare_digest(row['token'] or '',token) or row['lease']<=time.time():raise BatchError('LEASE_EXPIRED')
    return row


def claim(db,run):
    ident(run,24);now=time.time();db.execute('BEGIN IMMEDIATE')
    db.execute("UPDATE dj_batch_jobs SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,token=NULL,lease=0 WHERE run_id=? AND state='running' AND lease<=?",(run,now))
    db.execute("""UPDATE dj_batch_jobs SET state='done' WHERE run_id=? AND state='pending' AND EXISTS
        (SELECT 1 FROM dj_evidence e WHERE e.md5=dj_batch_jobs.md5
         AND json_extract(e.payload,'$.fullComplete')=1 AND json_extract(e.payload,'$.version')>=?)""",(run,VERSION))
    row=db.execute("""SELECT b.* FROM dj_batch_jobs b LEFT JOIN dj_analysis a ON a.md5=b.md5
        WHERE b.run_id=? AND b.state='pending' AND (a.status IS NULL OR a.status!='working')
        AND NOT EXISTS (SELECT 1 FROM dj_batch_jobs other WHERE other.md5=b.md5 AND other.state='running' AND other.lease>?)
        ORDER BY b.updated,b.md5 LIMIT 1""",(run,now)).fetchone()
    if row is None:db.commit();return {'job':None,'stats':stats(db,run)}
    token=secrets.token_hex(16)
    db.execute("UPDATE dj_batch_jobs SET state='running',token=?,lease=?,attempts=attempts+1,updated=? WHERE run_id=? AND md5=?",(token,now+LEASE_SECONDS,now,run,row['md5']))
    asset=db.execute('SELECT * FROM web_assets WHERE id=?',(row['asset_id'],)).fetchone()
    if not asset:db.rollback();raise BatchError('SOURCE_NOT_FOUND')
    cached=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(row['md5'],)).fetchone()
    previous=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(row['md5'],)).fetchone()
    prior=json.loads(previous[0]) if previous and previous[0] else {}
    evidence=json.loads(cached[0]) if cached else {'version':2,'words':[],'duration':asset['duration'],'coverage':[]}
    evidence['safeExits']=[[c['end']-c['fade'],c['end']] for c in prior.get('cues',[])+prior.get('remix',[]) if .5<=c.get('fade',0)<=8]
    db.commit()
    return {'job':{'token':token,'md5':row['md5'],'asset':{k:asset[k] for k in ('id','title','artist','duration','bytes')}|{'format':Path(asset['remote_path']).suffix[1:]},'cached':evidence,'previous':prior},'stats':stats(db,run)}


def numeric_evidence(value,duration):
    """Reject text, nonfinite values and oversized/unbound data before storage."""
    import math
    if not isinstance(value,dict) or value.get('version')!=VERSION or value.get('fullComplete') is not True:raise BatchError('INCOMPLETE_ANALYSIS')
    def number(x,lo=0,hi=duration+.5):
        if type(x) not in (int,float) or not math.isfinite(x) or not lo<=x<=hi:raise BatchError('INVALID_EVIDENCE')
        return x
    out={'version':VERSION,'duration':number(value.get('duration')),'fullComplete':True,'analysisScope':'full'}
    if abs(out['duration']-duration)>1:raise BatchError('DURATION_MISMATCH')
    out['fullDuration']=number(value.get('fullDuration',out['duration']))
    if abs(out['fullDuration']-duration)>1:raise BatchError('DURATION_MISMATCH')
    bpm=number(value.get('bpm'),0,210)
    if bpm and bpm<60:raise BatchError('INVALID_EVIDENCE')
    out['bpm']=bpm
    for key,limit,maximum in [('beats',6000,duration+.5),('energy',601,1)]:
        rows=value.get(key,[])
        if not isinstance(rows,list) or len(rows)>limit:raise BatchError('INVALID_EVIDENCE')
        out[key]=[number(x,0,maximum) for x in rows]
    for key,limit in [('words',16000),('coverage',200),('probes',200),('safeExits',100)]:
        rows=value.get(key,[])
        if not isinstance(rows,list) or len(rows)>limit:raise BatchError('INVALID_EVIDENCE')
        out[key]=[]
        for row in rows:
            if not isinstance(row,(list,tuple)) or len(row)!=(3 if key=='words' else 2):raise BatchError('INVALID_EVIDENCE')
            lo,hi=number(row[0]),number(row[1])
            if hi<lo:raise BatchError('INVALID_EVIDENCE')
            if key=='words' and type(row[2]) is not bool:raise BatchError('INVALID_EVIDENCE')
            out[key].append([lo,hi,row[2]] if key=='words' else [lo,hi])
    if len(json.dumps(out))>MAX_JSON:raise BatchError('RESULT_TOO_LARGE')
    return out


def dispatch(db,body):
    op=body.get('op')
    if op=='start':return start(db,body.get('playlist'))
    if op in ('claim','status'):
        run=ident(body.get('runId'),24)
        return claim(db,run) if op=='claim' else {'stats':stats(db,run)}
    db.execute('BEGIN IMMEDIATE')
    if op in ('finish','release','fail'):
        run=ident(body.get('runId'),24);md5=ident(body.get('md5'),32);token=ident(body.get('token'),32)
        done=db.execute("SELECT token FROM dj_batch_jobs WHERE run_id=? AND md5=? AND state='done'",(run,md5)).fetchone()
        if done and secrets.compare_digest(done[0] or '',token):
            saved=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(md5,)).fetchone()
            result=json.loads(saved[0]) if saved and saved[0] else {}
            db.commit();return {'stats':stats(db,run),'alreadyDone':True,'regular':len(result.get('cues',[])),'remix':len(result.get('remix',[]))}
    row=owned(db,body);run=row['run_id'];md5=row['md5']
    if op=='heartbeat':db.execute('UPDATE dj_batch_jobs SET lease=? WHERE run_id=? AND md5=?',(time.time()+LEASE_SECONDS,run,md5))
    elif op=='release':db.execute("UPDATE dj_batch_jobs SET state='pending',token=NULL,lease=0,attempts=MAX(0,attempts-1) WHERE run_id=? AND md5=?",(run,md5))
    elif op=='fail':
        error=body.get('error')
        if error not in ('DOWNLOAD_FAILED','ANALYSIS_FAILED','RESOURCE_LIMIT','TIMEOUT','RESULT_INVALID'):error='ANALYSIS_FAILED'
        db.execute("UPDATE dj_batch_jobs SET state='failed',token=NULL,lease=0,error=?,updated=? WHERE run_id=? AND md5=?",(error,time.time(),run,md5))
    elif op=='finish':
        from .dj_worker import selection
        asset=db.execute('SELECT duration FROM web_assets WHERE id=?',(row['asset_id'],)).fetchone()
        result=body.get('result')
        if not asset or not isinstance(result,dict):raise BatchError('INVALID_RESULT')
        evidence=numeric_evidence(result.get('evidence'),asset['duration'])
        value=validate(selection(evidence),asset['duration'])
        db.execute("INSERT OR IGNORE INTO dj_analysis VALUES (?,?, 'complete',NULL,?)",(md5,row['asset_id'],time.time()))
        complete(db,md5,value,asset['duration'],commit=False)
        db.execute('INSERT OR REPLACE INTO dj_evidence VALUES (?,?,?)',(md5,json.dumps(evidence),time.time()))
        db.execute("UPDATE dj_batch_jobs SET state='done',lease=0,error=NULL,updated=? WHERE run_id=? AND md5=?",(time.time(),run,md5))
        db.commit();return {'regular':len(value['cues']),'remix':len(value['remix']),'stats':stats(db,run)}
    else:raise BatchError('INVALID_OPERATION')
    db.commit();return {'stats':stats(db,run)}


def audio(settings,db,body,output):
    from .disk import Disk
    row=owned(db,body)
    asset=db.execute('SELECT * FROM web_assets WHERE id=?',(row['asset_id'],)).fetchone()
    if not asset or asset['bytes']>40*1024**2:raise BatchError('SOURCE_NOT_FOUND')
    if shutil.disk_usage(settings.state).free<256*1024**2:raise BatchError('LOW_DISK')
    # A single short-lived server transfer; no persistent copied library.
    with tempfile.TemporaryDirectory(prefix='dj-transfer-',dir=settings.state/'work') as directory:
        file=Path(directory)/'source';Disk(settings).download(asset['remote_path'],file,expected_md5=row['md5'])
        if file.stat().st_size!=asset['bytes']:raise BatchError('SOURCE_CHANGED')
        with file.open('rb') as stream:shutil.copyfileobj(stream,output,65536)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['rpc','audio']);parser.add_argument('--run');parser.add_argument('--md5');parser.add_argument('--token');args=parser.parse_args()
    settings=Settings();db=connect(settings)
    try:
        if args.action=='audio':audio(settings,db,{'runId':args.run,'md5':args.md5,'token':args.token},sys.stdout.buffer);return
        raw=sys.stdin.buffer.read(MAX_JSON+1)
        if len(raw)>MAX_JSON:raise BatchError('REQUEST_TOO_LARGE')
        body=json.loads(raw)
        if not isinstance(body,dict):raise BatchError('INVALID_REQUEST')
        print(json.dumps({'ok':True,**dispatch(db,body)},ensure_ascii=False))
    except Exception as exc:
        db.rollback();code=str(exc) if isinstance(exc,BatchError) else 'REMOTE_FAILED'
        if args.action=='audio':print(code,file=sys.stderr)
        else:print(json.dumps({'ok':False,'error':code}))
        raise SystemExit(1)
    finally:db.close()


if __name__=='__main__':main()
