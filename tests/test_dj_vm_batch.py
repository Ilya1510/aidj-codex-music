import json
from pathlib import Path
import threading
from types import SimpleNamespace as N
import pytest

from aidj.dj_batch import start,claim,dispatch,local_job,recover_local,retry_failed,serve
from aidj.dj_remote import RemoteError
from aidj.config import atomic_json
from tests.test_dj_batch import seed,evidence
from tests.test_playlists import env


class FakeDisk:
    downloads=[]
    def __init__(self,settings):pass
    def download(self,remote,file,expected_md5=None):
        self.downloads.append((remote,expected_md5));file.write_bytes(b'x'*123)


def fake_runner(command,environment,stopped,lost,**kwargs):
    output=Path(command[command.index('--output')+1]);checkpoint=kwargs['checkpoint']
    assert '--full' in command and command[command.index('--threads')+1]=='1'
    assert environment['OMP_NUM_THREADS']==environment['OPENBLAS_NUM_THREADS']=='1'
    atomic_json(checkpoint,evidence());output.write_text(json.dumps({'evidence':evidence()}))
    return {'peakRssBytes':200*1024**2,'limitBytes':1024**3,'elapsedSeconds':1,'stage':'complete','reason':'completed','returnCode':0}


def test_vm_full_job_commits_shared_campaign_and_reuses_only_one_scratch_source(env):
    settings,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    FakeDisk.downloads=[]
    def runner(command,*args,**kwargs):
        assert command[0]=='/isolated/python'
        assert db.execute('SELECT status FROM dj_analysis WHERE md5=?',(job['md5'],)).fetchone()[0]=='working'
        return fake_runner(command,*args,**kwargs)
    result=local_job(settings,db,run,job,'/isolated/python','ffmpeg',threading.Event(),runner,FakeDisk)
    assert result['state']=='done' and result['regular'] and result['remix']
    assert result['stats']['done']==1 and result['observation']['peakRssBytes']==200*1024**2
    assert FakeDisk.downloads==[('/Музыка/song.mp3',job['md5'])]
    assert not list((settings.state/'work').iterdir())
    assert not list((settings.state/'dj-batch-checkpoints').iterdir())
    assert not (settings.state/'dj-batch-local.json').exists()
    assert json.loads(db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(job['md5'],)).fetchone()[0])['fullComplete']


@pytest.mark.parametrize('old_ready',[False,True])
def test_cancel_restores_only_own_lease_and_retains_numeric_checkpoint(env,old_ready):
    settings,db,*_=env;seed(db,1)
    if old_ready:
        db.execute("INSERT INTO dj_analysis VALUES (?,?,'complete',?,0)",('0'*31+'1','0'*23+'1',json.dumps({'version':3,'cues':[{'start':0,'end':110,'bpm':120,'fade':1.8}]})));db.commit()
    run=start(db)['runId'];job=claim(db,run)['job'];stop=threading.Event()
    partial={'version':3,'words':[],'probes':[[0,28]],'analysisStage':'asr'}
    def interrupted(command,env,stopped,lost,**kwargs):
        atomic_json(kwargs['checkpoint'],partial);stopped.set();raise RemoteError('INTERRUPTED')
    result=local_job(settings,db,run,job,'python','ffmpeg',stop,interrupted,FakeDisk)
    assert result['state']=='stopped'
    row=db.execute('SELECT state,attempts FROM dj_batch_jobs WHERE run_id=?',(run,)).fetchone()
    assert tuple(row)==('pending',0)
    assert json.loads((settings.state/'dj-batch-checkpoints'/(job['md5']+'.json')).read_text())==partial
    actual=db.execute('SELECT status FROM dj_analysis WHERE md5=?',(job['md5'],)).fetchone()
    assert actual[0]=='complete' if old_ready else actual is None
    assert not list((settings.state/'work').iterdir())


def test_one_failed_resource_job_is_quarantined_and_next_source_is_claimable(env):
    settings,db,*_=env;seed(db,2);run=start(db)['runId'];job=claim(db,run)['job']
    detail={'peakRssBytes':1100*1024**2,'limitBytes':1024**3,'elapsedSeconds':3,'stage':'asr','reason':'rss_limit'}
    def failed(*args,**kwargs):
        atomic_json(kwargs['checkpoint'],{'version':3,'probes':[[0,28]]});raise RemoteError('RESOURCE_LIMIT',detail)
    result=local_job(settings,db,run,job,'python','ffmpeg',threading.Event(),failed,FakeDisk)
    assert result['state']=='failed' and result['error']=='RESOURCE_LIMIT'
    assert json.loads((settings.state/'diagnostics'/(job['md5']+'.json')).read_text())==detail
    assert db.execute('SELECT state FROM dj_batch_jobs WHERE md5=?',(job['md5'],)).fetchone()[0]=='failed'
    next_job=claim(db,run)['job'];assert next_job['md5']!=job['md5']
    assert retry_failed(db,run,job['md5'])['retried']==1


def test_crash_recovery_uses_recorded_token_and_never_cancels_a_mac_lease(env):
    settings,db,*_=env;seed(db,1);run=start(db)['runId'];mac=claim(db,run)['job']
    scratch=settings.state/'work'/'dj-full-owned';scratch.mkdir();(scratch/'source').write_bytes(b'temporary')
    atomic_json(settings.state/'dj-batch-local.json',{'runId':run,'md5':mac['md5'],'token':'f'*32,'scratch':str(scratch),'previousStatus':None,'hadPayload':False})
    recover_local(settings,db)
    row=db.execute('SELECT state,token FROM dj_batch_jobs WHERE md5=?',(mac['md5'],)).fetchone()
    assert row['state']=='running' and row['token']==mac['token']
    assert not scratch.exists() and not (settings.state/'dj-batch-local.json').exists()


def test_serve_resumes_existing_full_campaign_without_touching_other_mac_work(env,monkeypatch):
    settings,db,*_=env;seed(db,2);run=start(db)['runId'];mac=claim(db,run)['job'];seen=[]
    def local(settings,db,selected,job,python,ffmpeg,stopped,cpu_threads=1):
        seen.append(job['md5']);return {'state':'done',**dispatch(db,{'op':'finish','runId':selected,'md5':job['md5'],'token':job['token'],'result':{'evidence':evidence()}})}
    monkeypatch.setattr('aidj.dj_batch.local_job',local)
    monkeypatch.setattr('aidj.dj_batch.shutil.disk_usage',lambda path:N(free=2*1024**3))
    serve(settings,'python','ffmpeg',run=run,once=True)
    assert len(seen)==1 and seen[0]!=mac['md5']
    assert db.execute('SELECT state FROM dj_batch_jobs WHERE md5=?',(mac['md5'],)).fetchone()[0]=='running'


def test_launcher_starts_light_controller_with_separate_heavy_interpreter(monkeypatch):
    from aidj import dj_launcher
    called=[]
    monkeypatch.setattr('aidj.dj_launcher.ffmpeg',lambda:'ffmpeg')
    monkeypatch.setattr('aidj.dj_launcher.os.execv',lambda executable,args:called.append((executable,args)))
    dj_launcher.main();executable,args=called[0]
    assert args[:4]==[executable,'-m','aidj.dj_batch','serve']
    assert args[args.index('--analysis-python')+1].endswith('/.dj-venv/bin/python')
    unit=(Path(__file__).resolve().parent.parent/'deploy/units/aidj-dj-prepare.service').read_text()
    assert 'MemoryMax=1G' in unit and 'MemoryLimit=1G' in unit and 'TasksMax=64' in unit and 'Restart=no' in unit


def test_explicit_refine_stamps_rich_cache_and_requeues_deficient_completed_source_once(env,monkeypatch):
    from aidj.dj_batch import refine_completed
    settings,db,*_=env;seed(db,2);run=start(db)['runId']
    for i in (1,2):
        job=claim(db,run)['job'];value=evidence();value['bpm']=119+i
        dispatch(db,{'op':'finish','runId':run,'md5':job['md5'],'token':job['token'],'result':{'evidence':value}})
    monkeypatch.setattr('aidj.dj_worker.needs_rescue',lambda value:value['bpm']==120,raising=False)
    refined=refine_completed(db,run)
    assert refined['queuedForRefinement']==1 and refined['stampedFromCache']==1
    assert dispatch(db,{'op':'claim','runId':run})['job'] is None
    poor=dispatch(db,{'op':'claim','runId':run,'boundaryPass':1})['job'];assert poor is not None and poor['cached']['bpm']==120
    value={**evidence(),'boundaryPass':1}
    dispatch(db,{'op':'finish','runId':run,'md5':poor['md5'],'token':poor['token'],'result':{'evidence':value}})
    assert refine_completed(db,run)['queuedForRefinement']==0
    assert claim(db,run)['job'] is None


def test_server_preserves_bounded_numeric_acoustic_features_and_pass_marker():
    from aidj.dj_batch import numeric_evidence,BatchError
    original={**evidence(),'boundaryPass':1,'acousticFeatures':{'hop':.02,'regions':[[1,2,.7],[4,5,.6]],'text':'not stored'}}
    value=numeric_evidence(original,180)
    assert value['boundaryPass']==1 and value['acousticFeatures']=={'hop':.02,'regions':[[1,2,.7],[4,5,.6]]}
    for features in ({'hop':0,'regions':[]},{'hop':.02,'regions':[[1,2,float('nan')]]},{'hop':.02,'regions':[[2,1,.7]]}):
        with pytest.raises(BatchError):numeric_evidence({**evidence(),'acousticFeatures':features},180)


def test_stats_separate_audible_anchor_choices_from_legacy_cue_counts_and_review(env):
    from aidj.dj_batch import stats
    settings,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    dispatch(db,{'op':'finish','runId':run,'md5':job['md5'],'token':job['token'],'result':{'evidence':evidence()}})
    payload={'cues':[],'remix':[],'needsReview':True,'acousticAnchors':{'entries':[[0,.5]],'exits':[[15,.5,.5]],'needsReview':True}}
    db.execute('UPDATE dj_analysis SET payload=? WHERE md5=?',(json.dumps(payload),job['md5']));db.commit()
    counts=stats(db,run)
    assert counts['regularReady']==counts['remixReady']==0
    assert counts['anchorReady']==counts['acousticReady']==counts['needsReview']==1 and counts['wordAnchorsReady']==0


def test_acoustic_backup_alone_does_not_label_a_good_word_graph_for_review(env):
    from aidj.dj_batch import stats
    settings,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    dispatch(db,{'op':'finish','runId':run,'md5':job['md5'],'token':job['token'],'result':{'evidence':evidence()}})
    graph={'entries':[[0,.5]],'exits':[[15,.5,.5]]}
    value={'cues':[],'remix':[],'anchors':graph,'acousticAnchors':{**graph,'needsReview':True},'needsReview':False}
    db.execute('UPDATE dj_analysis SET payload=? WHERE md5=?',(json.dumps(value),job['md5']));db.commit()
    assert stats(db,run)['needsReview']==0 and stats(db,run)['fullFallback']==0
    value.update(reviewRecommended=True,fullFallback=True)
    db.execute('UPDATE dj_analysis SET payload=? WHERE md5=?',(json.dumps(value),job['md5']));db.commit()
    assert stats(db,run)['needsReview']==1 and stats(db,run)['fullFallback']==1


def test_vm_two_threads_still_use_one_child_and_the_same_memory_budget(env):
    settings,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    def runner(command,environment,stopped,lost,**kwargs):
        assert command[command.index('--threads')+1]=='2'
        assert environment['OPENBLAS_NUM_THREADS']=='1'
        output=Path(command[command.index('--output')+1]);output.write_text(json.dumps({'evidence':evidence()}))
        return {'peakRssBytes':200*1024**2,'limitBytes':1024**3}
    result=local_job(settings,db,run,job,'python','ffmpeg',threading.Event(),runner,FakeDisk,cpu_threads=2)
    assert result['state']=='done' and result['observation']['limitBytes']==1024**3


def test_idle_controller_refines_old_mac_evidence_once_before_finishing(env,monkeypatch):
    settings,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    dispatch(db,{'op':'finish','runId':run,'md5':job['md5'],'token':job['token'],'result':{'evidence':evidence()}})
    monkeypatch.setattr('aidj.dj_worker.needs_rescue',lambda value:value.get('boundaryPass',0)<1)
    seen=[]
    def local(settings,db,selected,job,python,ffmpeg,stopped,cpu_threads=1):
        seen.append(job['md5']);value={**evidence(),'boundaryPass':1}
        return {'state':'done',**dispatch(db,{'op':'finish','runId':selected,'md5':job['md5'],'token':job['token'],'result':{'evidence':value}})}
    monkeypatch.setattr('aidj.dj_batch.local_job',local)
    monkeypatch.setattr('aidj.dj_batch.shutil.disk_usage',lambda path:N(free=2*1024**3))
    serve(settings,'python','ffmpeg',run=run,once=True,cpu_threads=2)
    assert seen==[job['md5']]
    assert json.loads(db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(job['md5'],)).fetchone()[0])['boundaryPass']==1
