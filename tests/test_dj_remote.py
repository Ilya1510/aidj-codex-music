import io
import json
from pathlib import Path
from types import SimpleNamespace as N
import threading
import pytest

from aidj.dj_remote import (Transport,RemoteError,ssh_command,worker_count,valid_asset,
                           checkpoint_for,analyse_command,complete_job,run_analysis,process_rss,
                           job_identity,status_line)


def test_ssh_remote_paths_are_quoted_and_options_cannot_be_injected():
    args,command=ssh_command('user@host','/path with spaces/$(touch nope)',Path('/tmp/control'))
    assert args[-2:]==['--','user@host']
    assert "cd -- '/path with spaces/$(touch nope)' && exec .venv/bin/python -m aidj.dj_batch"==command
    assert 'StrictHostKeyChecking=no' not in ' '.join(args)
    assert args[args.index('ControlMaster=auto')-1]=='-o'
    for host in ['-oProxyCommand=oops','host bad','x;echo bad']:
        with pytest.raises(ValueError):ssh_command(host,'/valid')


def test_worker_budget_leaves_memory_and_cpu_for_macos():
    assert worker_count(None,16,16*1024**3)==4
    assert worker_count(8,16,32*1024**3)==8
    assert worker_count(8,4,8*1024**3)==2
    assert worker_count(8,16,4*1024**3)==1
    assert worker_count(1,32,64*1024**3)==1


def test_audio_is_streamed_and_only_verified_complete_bytes_survive(tmp_path,monkeypatch):
    import hashlib
    raw=b'cached audio'*10000;digest=hashlib.md5(raw).hexdigest()
    class Child:
        pid=999999
        def __init__(self,*args,**kwargs):self.stdout=io.BytesIO(raw)
        def wait(self,timeout=None):return 0
        def poll(self):return 0
    monkeypatch.setattr('aidj.dj_remote.subprocess.Popen',Child)
    t=Transport('host','/project');file=tmp_path/'audio.mp3'
    identity={'runId':'a'*24,'md5':digest,'token':'b'*32}
    asset={'bytes':len(raw),'duration':180,'format':'mp3'}
    t.audio(identity,asset,file,threading.Event());assert file.read_bytes()==raw
    with pytest.raises(RemoteError,match='DOWNLOAD_MISMATCH'):
        t.audio({**identity,'md5':'f'*32},asset,file,threading.Event())
    assert not file.exists()
    with pytest.raises(RemoteError,match='DOWNLOAD_TOO_LARGE'):
        t.audio(identity,{**asset,'bytes':10},file,threading.Event())
    assert not file.exists()


def test_client_never_exposes_raw_ssh_stderr(monkeypatch):
    def run(*args,**kwargs):return N(returncode=1,stdout=b'',stderr=b'SECRET_VALUE private URL')
    monkeypatch.setattr('aidj.dj_remote.subprocess.run',run)
    with pytest.raises(RemoteError) as error:Transport('host','/project').rpc({'op':'start'})
    assert str(error.value)=='SSH_FAILED'
    assert 'SECRET' not in str(error.value)


def test_finish_retries_lost_ack_with_identical_identity_and_payload():
    class Remote:
        def __init__(self):self.calls=[]
        def rpc(self,value):
            self.calls.append(value)
            if len(self.calls)==1:raise RemoteError('SSH_FAILED')
            return {'ok':True,'alreadyDone':True}
    remote=Remote();stopped=N(wait=lambda seconds:False)
    result={'evidence':{'fullComplete':True}}
    assert complete_job(remote,{'runId':'a','token':'b','md5':'c'},result,stopped)['alreadyDone']
    assert remote.calls[0]==remote.calls[1]


def test_analysis_command_is_full_bounded_and_reuses_numeric_checkpoint():
    command=analyse_command(Path('/tmp/a.mp3'),Path('/tmp/out'),Path('/tmp/check'),Path('/tmp/cache'),1,'/ffmpeg')
    assert '--full' in command and command[command.index('--threads')+1]=='1'
    assert command[command.index('--cached')+1]=='/tmp/cache'
    server={'version':3,'probes':[[0,28]]};local={'version':3,'probes':[[0,28],[24,52]]}
    assert checkpoint_for(server,local) is local
    assert checkpoint_for(local,server) is local
    assert not valid_asset({'bytes':40*1024**2+1,'duration':180,'format':'mp3'})
    assert not valid_asset({'bytes':100,'duration':180,'format':'../../a'})


def test_memory_watchdog_kills_only_owned_analysis_process_group(monkeypatch):
    child=N(pid=12345,poll=lambda:None)
    monkeypatch.setattr('aidj.dj_remote.subprocess.Popen',lambda *a,**kw:child)
    monkeypatch.setattr('aidj.dj_remote.process_rss',lambda pid:2048)
    killed=[];monkeypatch.setattr('aidj.dj_remote.kill',lambda p:killed.append(p))
    stopped=N(wait=lambda seconds:False)
    with pytest.raises(RemoteError,match='RESOURCE_LIMIT'):
        run_analysis(['worker'],{},stopped,threading.Event(),memory_limit=1024)
    assert killed==[child]


def test_rss_counts_own_descendants_and_never_inspects_command_lines(monkeypatch):
    def ps(args,**kwargs):
        assert args==['ps','-axo','pid=,ppid=,rss=']
        return '1 0 500\n10 1 100\n11 10 200\n12 11 300\n99 1 9000\n'
    monkeypatch.setattr('aidj.dj_remote.subprocess.check_output',ps)
    assert process_rss(10)==600*1024


def test_identity_validation_and_status_are_small_and_nonsecret():
    job={'md5':'a'*32,'token':'b'*32}
    assert job_identity('c'*24,job)=={'runId':'c'*24,**job}
    with pytest.raises(RemoteError,match='INVALID_JOB'):job_identity('invalid',job)
    assert '3/10' in status_line({'done':3,'total':10,'running':4,'failed':1})


def test_trial_claim_budget_is_shared_across_workers_and_empty_claim_does_not_spend_it():
    from aidj.dj_remote import ClaimBudget
    class Remote:
        def __init__(self):self.calls=0
        def rpc(self,value):
            self.calls+=1
            return {'ok':True,'job':None if self.calls==1 else {'md5':'a'*32}}
    remote=Remote();budget=ClaimBudget(1)
    assert budget.claim(remote,'run')['job'] is None
    assert budget.claim(remote,'run')['job']['md5']=='a'*32
    assert budget.claim(remote,'run') is None and remote.calls==2
