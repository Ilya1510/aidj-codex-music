import json
import time
import pytest
from aidj.dj_batch import start,claim,dispatch,BatchError,numeric_evidence,audio
from aidj.dj import next_job
from tests.test_playlists import env


def seed(db,n=3):
    for i in range(1,n+1):
        db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(f'{i:024x}','youtube:'+str(i),'Song '+str(i),'Artist',180,f'{i:032x}',123,'/Музыка/song.mp3'))
    db.commit()


def evidence():
    return {'version':3,'fullComplete':True,'fullDuration':180,'duration':180,'bpm':120,'words':[[s,s+3,True] for s in range(0,180,4)],'beats':list(range(180)),'coverage':[[0,180]],'probes':[[0,180]],'energy':[.8]*180}


def test_claims_are_unique_bounded_and_not_an_app_preparation_queue(env):
    _,db,*_=env;seed(db)
    run=start(db)['runId'];jobs=[claim(db,run)['job'] for _ in range(3)]
    assert len({j['md5'] for j in jobs})==3
    assert claim(db,run)['job'] is None
    assert db.execute("SELECT count(*) FROM dj_analysis WHERE status IN ('pending','working')").fetchone()[0]==0
    assert 'remote_path' not in json.dumps(jobs) and 'access_token' not in json.dumps(jobs)
    assert start(db)['stats']['running']==3


def test_finish_validates_numeric_data_commits_cues_and_survives_lost_ack(env):
    _,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    body={'op':'finish','runId':run,'md5':job['md5'],'token':job['token'],'result':{'evidence':evidence()}}
    result=dispatch(db,body);assert result['stats']['done']==1 and result['regular'] and result['remix']
    saved=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(job['md5'],)).fetchone()[0]
    assert json.loads(saved)['fullComplete'] is True
    assert dispatch(db,body)['alreadyDone'] is True
    assert dispatch(db,{**body,'op':'release'})['alreadyDone'] is True
    assert db.execute('SELECT count(*) FROM dj_analysis').fetchone()[0]==1


def test_bad_or_expired_leases_cannot_overwrite_results_and_stale_jobs_resume(env):
    _,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    db.execute('UPDATE dj_batch_jobs SET lease=0');db.commit()
    with pytest.raises(BatchError):dispatch(db,{'op':'finish','runId':run,'md5':job['md5'],'token':job['token'],'result':{'evidence':evidence()}})
    db.rollback();renewed=claim(db,run)['job'];assert renewed['md5']==job['md5'] and renewed['token']!=job['token']
    assert dispatch(db,{'op':'release','runId':run,'md5':renewed['md5'],'token':renewed['token']})['stats']['pending']==1


def test_vm_and_mac_do_not_claim_the_same_source(env):
    _,db,*_=env;seed(db,1);run=start(db)['runId'];job=claim(db,run)['job']
    db.execute("INSERT INTO dj_analysis VALUES (?,?,'pending',NULL,?)",(job['md5'],job['asset']['id'],time.time()));db.commit()
    assert next_job(db) is None
    dispatch(db,{'op':'release','runId':run,'md5':job['md5'],'token':job['token']})
    assert next_job(db)['md5']==job['md5']
    db.execute("UPDATE dj_analysis SET status='working'");db.commit();assert claim(db,run)['job'] is None


def test_failed_job_is_not_blindly_retried_and_full_results_are_skipped(env):
    _,db,*_=env;seed(db,2)
    db.execute('INSERT INTO dj_evidence VALUES (?,?,?)',(f'{1:032x}',json.dumps(evidence()),time.time()));db.commit()
    run=start(db)['runId'];assert start(db)['stats']['done']==1
    job=claim(db,run)['job'];assert job['md5']==f'{2:032x}'
    dispatch(db,{'op':'fail','runId':run,'md5':job['md5'],'token':job['token'],'error':'RESOURCE_LIMIT'})
    assert claim(db,run)['job'] is None and start(db)['stats']['failed']==1


@pytest.mark.parametrize('change',[{'duration':float('nan')},{'fullComplete':False},{'words':[[1,2,'lyrics']]},{'coverage':[[4,2]]},{'bpm':400}])
def test_unbounded_or_textual_results_are_rejected(change):
    with pytest.raises(BatchError):numeric_evidence({**evidence(),**change},180)


def test_a_parallel_campaign_skips_results_completed_since_it_was_started(env):
    _,db,*_=env;seed(db,1);run=start(db)['runId'];other='b'*24
    db.execute('INSERT INTO dj_batch_runs VALUES (?,?,?)',(other,'other',0))
    db.execute("INSERT INTO dj_batch_jobs SELECT ?,md5,asset_id,state,lease,token,attempts,error,updated FROM dj_batch_jobs WHERE run_id=?",(other,run));db.commit()
    job=claim(db,run)['job']
    dispatch(db,{'op':'finish','runId':run,'md5':job['md5'],'token':job['token'],'result':{'evidence':evidence()}})
    reply=claim(db,other);assert reply['job'] is None and reply['stats']['done']==1


def test_full_scope_survives_upload_for_late_song_entries():
    value=numeric_evidence(evidence(),180)
    assert value['analysisScope']=='full'
