"""Owner's SSH-only coordinator for full DJ analysis on another computer.

No HTTP endpoint or exported Disk credentials. RPC replies contain only song
metadata and numeric analysis; originals cross the authenticated SSH channel.
"""
import argparse
from bisect import bisect_left,bisect_right
import fcntl
import os
import signal
import threading
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


def failure_diagnostics(value):
    import math
    if not isinstance(value,dict):return {}
    out={}
    for key,maximum in [('peakRssBytes',16*1024**3),('limitBytes',16*1024**3),('elapsedSeconds',3600)]:
        number=value.get(key)
        if type(number) in (int,float) and math.isfinite(number) and 0<=number<=maximum:out[key]=number
    if value.get('stage') in ('decode','rhythm','asr','selection','complete','unknown'):out['stage']=value['stage']
    if value.get('reason') in ('rss_limit','process_killed','monitor_failed'):out['reason']=value['reason']
    if value.get('returnCode') in (-9,137):out['returnCode']=value['returnCode']
    return out


def ident(value,length):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{'+str(length)+'}',value):raise BatchError('INVALID_ID')
    return value


def usable_graph(graph):
    if not isinstance(graph,dict):return False
    starts=sorted(row[0] for row in graph.get('entries',[]))
    return any(bisect_right(starts,row[0]-10)>bisect_left(starts,row[0]-120) for row in graph.get('exits',[]))


def stats(db,run):
    counts={r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM dj_batch_jobs WHERE run_id=? GROUP BY state',(run,))}
    ready=db.execute("""SELECT COALESCE(sum(json_array_length(a.payload,'$.cues')>0),0),
        COALESCE(sum(json_array_length(a.payload,'$.remix')>0),0) FROM dj_batch_jobs b
        LEFT JOIN dj_analysis a ON a.md5=b.md5 WHERE b.run_id=? AND b.state='done'""",(run,)).fetchone()
    anchors=0;word_anchors=0;acoustic_anchors=0;review=0;full_fallback=0
    for row in db.execute("SELECT a.payload FROM dj_batch_jobs b JOIN dj_analysis a ON a.md5=b.md5 WHERE b.run_id=? AND b.state='done' AND a.payload IS NOT NULL",(run,)):
        value=json.loads(row[0]);graph=value.get('anchors',{});acoustic=value.get('acousticAnchors',{})
        word=usable_graph(graph);signal_ready=usable_graph(acoustic)
        if word or signal_ready:anchors+=1
        if word:word_anchors+=1
        if signal_ready:acoustic_anchors+=1
        if value.get('needsReview') is True or value.get('reviewRecommended') is True:review+=1
        if value.get('fullFallback') is True:full_fallback+=1
    return {**{k:counts.get(k,0) for k in ('pending','running','done','failed')},'total':sum(counts.values()),
            'regularReady':ready[0],'remixReady':ready[1],'withoutRegular':counts.get('done',0)-ready[0],'withoutRemix':counts.get('done',0)-ready[1],
            'anchorReady':anchors,'wordAnchorsReady':word_anchors,'acousticReady':acoustic_anchors,'needsReview':review,'fullFallback':full_fallback}


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
         AND json_extract(e.payload,'$.fullComplete')=1 AND json_extract(e.payload,'$.version')>=?
         AND (COALESCE(json_extract(e.payload,'$.boundaryPass'),0)>=1 OR COALESCE(dj_batch_jobs.error,'')!='REFINE_BOUNDARIES'))""",(run,VERSION))
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
    marker=value.get('boundaryPass',0)
    if type(marker) is not int or marker not in (0,1):raise BatchError('INVALID_EVIDENCE')
    out['boundaryPass']=marker
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
    if 'acousticFeatures' in value:
        features=value['acousticFeatures']
        if not isinstance(features,dict):raise BatchError('INVALID_EVIDENCE')
        hop=number(features.get('hop'),.001,1);regions=features.get('regions',[])
        if not isinstance(regions,list) or len(regions)>512:raise BatchError('INVALID_EVIDENCE')
        clean=[]
        for region in regions:
            if not isinstance(region,(list,tuple)) or len(region)!=3:raise BatchError('INVALID_EVIDENCE')
            lo,hi=number(region[0]),number(region[1])
            if hi<=lo:raise BatchError('INVALID_EVIDENCE')
            clean.append([lo,hi,number(region[2],0,1)])
        out['acousticFeatures']={'hop':hop,'regions':clean}
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
        detail=failure_diagnostics(body.get('diagnostics'))
        if detail:db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('dj_failure_diagnostics:'+md5,json.dumps(detail)))
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


def _restore_local(db,active):
    """Recover only the lease token recorded by this local controller."""
    try:
        run=ident(active.get('runId'),24);md5=ident(active.get('md5'),32);token=ident(active.get('token'),32)
    except (BatchError,AttributeError):return
    row=db.execute('SELECT state,token FROM dj_batch_jobs WHERE run_id=? AND md5=?',(run,md5)).fetchone()
    if row and row['state']=='running' and not secrets.compare_digest(row['token'] or '',token):
        return
    if row and row['state']=='running' and secrets.compare_digest(row['token'] or '',token):
        db.execute("UPDATE dj_batch_jobs SET state='pending',token=NULL,lease=0,attempts=MAX(0,attempts-1) WHERE run_id=? AND md5=?",(run,md5))
    old=active.get('previousStatus')
    if old is None and not active.get('hadPayload'):
        db.execute("DELETE FROM dj_analysis WHERE md5=? AND status='working' AND payload IS NULL",(md5,))
    else:
        if old not in ('pending','complete','failed'):old='complete' if active.get('hadPayload') else 'pending'
        db.execute("UPDATE dj_analysis SET status=? WHERE md5=? AND status='working'",(old,md5))
    db.commit()


def recover_local(settings,db):
    journal=settings.state/'dj-batch-local.json'
    try:
        if not journal.is_file() or journal.stat().st_size>4096:return
        active=json.loads(journal.read_text());_restore_local(db,active)
        # Cleanup is an exact path from this controller's own private journal,
        # never a scan of other work directories or the user's home.
        scratch=Path(active.get('scratch',''))
        if scratch.name.startswith('dj-full-') and not scratch.is_symlink() and scratch.parent.resolve()==(settings.state/'work').resolve():
            shutil.rmtree(scratch,ignore_errors=True)
    except (OSError,ValueError,TypeError):pass
    finally:journal.unlink(missing_ok=True)


def _checkpoint(path):
    if path.is_file() and 0<path.stat().st_size<=MAX_JSON:
        try:
            value=json.loads(path.read_text())
            return value if isinstance(value,dict) else None
        except (ValueError,OSError):pass
    return None


def local_job(settings,db,run,job,analysis_python,ffmpeg,stopped,runner=None,disk_factory=None,cpu_threads=1):
    """One original, one bounded child, one persistent numeric checkpoint."""
    from .config import atomic_json
    from .disk import Disk
    from .dj import heartbeat as runtime_heartbeat
    from .dj_remote import RemoteError,analyse_command,run_analysis,json_file,checkpoint_for,save_diagnostics
    runner=runner or run_analysis;disk_factory=disk_factory or Disk
    identity={'runId':run,'md5':job['md5'],'token':job['token']}
    asset=db.execute('SELECT * FROM web_assets WHERE id=?',(job['asset']['id'],)).fetchone()
    if not asset or asset['bytes']>40*1024**2:raise BatchError('SOURCE_NOT_FOUND')
    checkpoints=settings.state/'dj-batch-checkpoints';checkpoints.mkdir(exist_ok=True,mode=0o700)
    checkpoint=checkpoints/(job['md5']+'.json');journal=settings.state/'dj-batch-local.json'
    previous=db.execute('SELECT status,payload FROM dj_analysis WHERE md5=?',(job['md5'],)).fetchone()
    active={**identity,'previousStatus':previous['status'] if previous else None,'hadPayload':bool(previous and previous['payload'])}
    done=threading.Event();lost=threading.Event();phase='DOWNLOAD_FAILED'
    def renew():
        beat=connect(settings)
        try:
            while not done.wait(20):
                try:dispatch(beat,{'op':'heartbeat',**identity});runtime_heartbeat(beat,'working')
                except Exception:lost.set();return
        finally:beat.close()
    keeper=None
    try:
        with tempfile.TemporaryDirectory(prefix='dj-full-',dir=settings.state/'work') as directory:
            folder=Path(directory);active['scratch']=str(folder);atomic_json(journal,active)
            db.execute("INSERT INTO dj_analysis VALUES (?,?,'working',NULL,?) ON CONFLICT(md5) DO UPDATE SET status='working',updated=excluded.updated",(job['md5'],asset['id'],time.time()));db.commit()
            runtime_heartbeat(db,'working');keeper=threading.Thread(target=renew,daemon=True);keeper.start()
            if stopped.is_set():raise RemoteError('INTERRUPTED')
            source=folder/('source'+Path(asset['remote_path']).suffix)
            disk_factory(settings).download(asset['remote_path'],source,expected_md5=job['md5'])
            if source.stat().st_size!=asset['bytes']:raise BatchError('SOURCE_CHANGED')
            if stopped.is_set():raise RemoteError('INTERRUPTED')
            cached=checkpoint_for(job.get('cached'),_checkpoint(checkpoint))
            if cached:
                cached=dict(cached);known=(job.get('cached') or {}).get('safeExits',[])
                cached['safeExits']=list({tuple(x) for x in cached.get('safeExits',[])+known})[:100]
                seed=folder/'cached.json';atomic_json(seed,cached)
            else:seed=None
            output=folder/'result.json';command=analyse_command(source,output,checkpoint,seed,cpu_threads,ffmpeg);command[0]=str(analysis_python)
            if re.search('[А-Яа-яЁё]',asset['artist']+' '+asset['title']):command+=['--language','ru']
            env=dict(os.environ);env['AIDJ_STATE_DIR']=str(settings.state)
            env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',NUMBA_NUM_THREADS='1')
            phase='ANALYSIS_FAILED'
            observation=runner(command,env,stopped,lost,checkpoint=checkpoint,timeout=900)
            if stopped.is_set():raise RemoteError('INTERRUPTED')
            phase='RESULT_INVALID';result=json_file(output)
            reply=dispatch(db,{'op':'finish',**identity,'result':result})
            checkpoint.unlink(missing_ok=True);return {'state':'done',**reply,'observation':observation}
    except Exception as exc:
        db.rollback()
        if stopped.is_set() or isinstance(exc,RemoteError) and exc.kind in ('INTERRUPTED','LEASE_LOST'):
            _restore_local(db,active);return {'state':'stopped'}
        kind=exc.kind if isinstance(exc,RemoteError) else phase
        if kind not in ('RESOURCE_LIMIT','TIMEOUT','RESULT_INVALID'):kind=phase
        details=exc.diagnostics if isinstance(exc,RemoteError) else {}
        try:dispatch(db,{'op':'fail',**identity,'error':kind,'diagnostics':details})
        except BatchError:db.rollback()
        if details:save_diagnostics(settings.state,job['md5'],details)
        db.execute("UPDATE dj_analysis SET status=CASE WHEN payload IS NOT NULL THEN 'complete' ELSE 'failed' END WHERE md5=? AND status='working'",(job['md5'],));db.commit()
        return {'state':'failed','error':kind,'diagnostics':details,'stats':stats(db,run)}
    finally:
        done.set()
        if keeper is not None:keeper.join(timeout=1)
        journal.unlink(missing_ok=True)


def retry_failed(db,run,md5):
    """Explicit owner action after fixing a diagnosed failure, never automatic."""
    ident(run,24);ident(md5,32)
    count=db.execute("UPDATE dj_batch_jobs SET state='pending',token=NULL,lease=0,attempts=0,error=NULL,updated=? WHERE run_id=? AND md5=? AND state='failed'",(time.time(),run,md5)).rowcount
    db.commit();return {'retried':count,'stats':stats(db,run)}


def refine_completed(db,run):
    """An explicit one-time quality pass; existing Mac receipts do not loop."""
    from .dj_worker import selection,needs_rescue
    ident(run,24);queued=0;stamped=0
    # Snapshot only identifiers: evidence can be up to 1 MiB per song. Read
    # one payload at a time while updates change the selected jobs' states.
    identifiers=[row[0] for row in db.execute("""SELECT b.md5 FROM dj_batch_jobs b
        JOIN dj_evidence e ON e.md5=b.md5 WHERE b.run_id=? AND b.state='done'
        AND json_extract(e.payload,'$.fullComplete')=1
        AND COALESCE(json_extract(e.payload,'$.boundaryPass'),0)<1""",(run,))]
    for md5 in identifiers:
        row=db.execute("""SELECT b.md5,a.duration,e.payload FROM dj_batch_jobs b
            JOIN web_assets a ON a.id=b.asset_id JOIN dj_evidence e ON e.md5=b.md5
            WHERE b.run_id=? AND b.md5=?""",(run,md5)).fetchone()
        if row is None:continue
        evidence=json.loads(row['payload'])
        complete(db,row['md5'],selection(evidence),row['duration'],commit=False)
        if needs_rescue(evidence):
            queued+=db.execute("UPDATE dj_batch_jobs SET state='pending',token=NULL,lease=0,attempts=0,error='REFINE_BOUNDARIES',updated=? WHERE run_id=? AND md5=? AND state='done'",(time.time(),run,row['md5'])).rowcount
        else:
            evidence['boundaryPass']=1
            db.execute('UPDATE dj_evidence SET payload=?,updated=? WHERE md5=?',(json.dumps(evidence),time.time(),row['md5']));stamped+=1
    db.commit();return {'queuedForRefinement':queued,'stampedFromCache':stamped,'stats':stats(db,run)}


def serve(settings,analysis_python,ffmpeg,run=None,once=False,stopped=None,pause=None,cpu_threads=1):
    """Continue the shared full-library campaign, one child at a time on VM."""
    from .dj import heartbeat as runtime_heartbeat
    stopped=stopped or threading.Event();pause=pause or stopped.wait
    with (settings.state/'dj-worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);db=connect(settings)
        recover_local(settings,db)
        selected=ident(run,24) if run is not None else start(db)['runId']
        refreshed=time.monotonic();failures=0
        try:
            while not stopped.is_set():
                if shutil.disk_usage(settings.state).free<768*1024**2:raise BatchError('LOW_DISK')
                if run is None and time.monotonic()-refreshed>60:
                    start(db);refreshed=time.monotonic()
                reply=claim(db,selected);job=reply['job']
                if job is None:
                    # Older Mac workers may finish while this controller is
                    # running. Admit their evidence, then give deficient old
                    # boundaries one resumable refinement pass after the main
                    # sweep. boundaryPass prevents repeating that pass.
                    refined=refine_completed(db,selected)
                    if refined['queuedForRefinement']:
                        print('DJ_BATCH_REFINEMENT',json.dumps(refined),flush=True)
                        continue
                    runtime_heartbeat(db,'idle')
                    if once:break
                    pause(10);continue
                outcome=local_job(settings,db,selected,job,analysis_python,ffmpeg,stopped,cpu_threads=cpu_threads)
                # Logs contain bounded counts, numeric observations and error
                # enums, never transcripts, access URLs or lease tokens.
                print('DJ_BATCH_'+outcome['state'].upper(),json.dumps({k:v for k,v in outcome.items() if k in ('stats','regular','remix','error','observation','diagnostics')}),flush=True)
                if outcome['state']=='stopped':break
                failures=failures+1 if outcome['state']=='failed' else 0
                if failures>=3:raise BatchError('CONSECUTIVE_FAILURES_REVIEW')
                if once:break
        finally:
            db.rollback();recover_local(settings,db);runtime_heartbeat(db,'stopped');db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['rpc','audio','serve','retry','refine']);parser.add_argument('--run');parser.add_argument('--md5');parser.add_argument('--token');parser.add_argument('--analysis-python',type=Path);parser.add_argument('--ffmpeg');parser.add_argument('--once',action='store_true');parser.add_argument('--threads',type=int,choices=(1,2),default=1);args=parser.parse_args()
    settings=Settings();settings.prepare()
    if args.action=='serve':
        stopped=threading.Event()
        signal.signal(signal.SIGTERM,lambda signum,frame:stopped.set());signal.signal(signal.SIGINT,lambda signum,frame:stopped.set())
        if args.analysis_python is None or not args.ffmpeg:parser.error('serve requires --analysis-python and --ffmpeg')
        try:serve(settings,args.analysis_python,args.ffmpeg,args.run,args.once,stopped,cpu_threads=args.threads)
        except Exception as exc:
            print('DJ_BATCH_STOPPED',str(exc) if isinstance(exc,BatchError) else type(exc).__name__,flush=True);raise SystemExit(1)
        return
    db=connect(settings)
    try:
        if args.action=='retry':print(json.dumps(retry_failed(db,args.run,args.md5)));return
        if args.action=='refine':print(json.dumps(refine_completed(db,args.run or start(db)['runId'])));return
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
