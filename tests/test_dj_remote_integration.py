"""Exercise the real SSH JSON protocol using a local fake ssh executable.

Only the audio supplier and expensive ASR are fixtures. Leasing, streaming,
checksum verification, RPC parsing, validation and SQLite commits are real.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

from aidj.config import Settings
from aidj.store import connect
from aidj.dj_remote import Transport,work_job


def test_fake_ssh_stream_and_real_server_rpc_commit_full_numeric_result(tmp_path,monkeypatch):
    project=Path(__file__).resolve().parent.parent
    server=Settings(state=tmp_path/'server');server.prepare();db=connect(server)
    audio=b'fixture audio bytes\x00'*10000
    original=tmp_path/'fixture.mp3';original.write_bytes(audio)
    digest=hashlib.md5(audio).hexdigest();aid='a'*24
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',
               (aid,'youtube:fixture','Fixture','Artist',180,digest,len(audio),'/Music/fixture.mp3'))
    db.commit();db.close()
    fakebin=tmp_path/'bin';fakebin.mkdir();ssh=fakebin/'ssh'
    ssh.write_text('#!'+sys.executable+'\n'+'''
import os,sys,shutil
if '-O' in sys.argv:raise SystemExit(0)
remote=sys.argv[-1]
if remote.endswith(' rpc'):
    os.execv(sys.executable,[sys.executable,'-m','aidj.dj_batch','rpc'])
if ' audio --run ' in remote:
    with open(os.environ['AIDJ_TEST_ORIGINAL'],'rb') as source:
        shutil.copyfileobj(source,sys.stdout.buffer,65536)
    raise SystemExit(0)
raise SystemExit(10)
''')
    ssh.chmod(0o700)
    monkeypatch.setenv('PATH',str(fakebin)+os.pathsep+os.environ.get('PATH',''))
    monkeypatch.setenv('PYTHONPATH',str(project))
    monkeypatch.setenv('AIDJ_STATE_DIR',str(server.state))
    monkeypatch.setenv('AIDJ_TEST_ORIGINAL',str(original))
    evidence={'version':3,'fullComplete':True,'fullDuration':180,'duration':180,'analysisScope':'full',
              'bpm':120,'words':[[s,s+3,True] for s in range(0,180,4)],'beats':list(range(180)),
              'coverage':[[0,180]],'probes':[[0,180]],'energy':[.8]*180}
    analyzer=tmp_path/'analysis_fixture.py'
    analyzer.write_text('import json,sys\nfrom pathlib import Path\n'+
                        'result='+repr({'evidence':evidence})+'\n'+
                        'Path(sys.argv[1]).write_text(json.dumps(result))\n'+
                        'Path(sys.argv[2]).write_text(json.dumps(result["evidence"]))\n')
    monkeypatch.setattr('aidj.dj_remote.analyse_command',
                        lambda file,out,checkpoint,cached,threads,ffmpeg:[sys.executable,str(analyzer),str(out),str(checkpoint)])
    local=tmp_path/'mac';local.mkdir();(local/'work').mkdir();(local/'checkpoints').mkdir()
    transport=Transport('fixture-vm',str(project),tmp_path/'control')
    started=transport.rpc({'op':'start','playlist':None});run=started['runId']
    assert started['stats']['pending']==1
    job=transport.rpc({'op':'claim','runId':run})['job']
    assert job['md5']==digest and job['asset']['bytes']==len(audio)
    identity={'runId':run,'md5':digest,'token':job['token']}
    transport.rpc({'op':'heartbeat',**identity})
    work_job(transport,run,job,local,1,'unused-ffmpeg',threading.Event())
    saved=connect(server)
    try:
        payload=json.loads(saved.execute('SELECT payload FROM dj_analysis WHERE md5=?',(digest,)).fetchone()[0])
        features=json.loads(saved.execute('SELECT payload FROM dj_evidence WHERE md5=?',(digest,)).fetchone()[0])
        assert payload['cues'] and payload['remix'] and features['fullComplete'] is True
        assert features['analysisScope']=='full'
        assert saved.execute('SELECT state FROM dj_batch_jobs WHERE run_id=?',(run,)).fetchone()[0]=='done'
    finally:saved.close()
    assert not list((local/'work').iterdir()) and not list((local/'checkpoints').iterdir())
    assert transport.rpc({'op':'finish',**identity,'result':{'evidence':evidence}})['alreadyDone'] is True
    assert transport.rpc({'op':'start'})['stats']['done']==1
    transport.close()
