import json
import requests
import pytest

from aidj.bootstrap import activate_pending,SetupError
from aidj.config import Settings,atomic_json


def test_network_failure_preserves_secret_for_retry(tmp_path,monkeypatch):
    settings=Settings(state=tmp_path/'state');settings.prepare()
    pending=settings.state/'telegram.pending.json'
    atomic_json(pending,{'token':'FAKE_SECRET','pair_code':'pair'})
    def fail(*args,**kwargs):raise requests.Timeout('FAKE_SECRET')
    monkeypatch.setattr('aidj.bootstrap.requests.post',fail)
    with pytest.raises(SetupError) as error:activate_pending(settings,attempts=1)
    assert error.value.kind=='network' and 'FAKE_SECRET' not in str(error.value)
    assert pending.exists() and pending.stat().st_mode&0o777==0o600
    assert not (settings.state/'telegram.json').exists()


def test_successful_retry_activates_saved_token_without_another_prompt(tmp_path,monkeypatch):
    settings=Settings(state=tmp_path/'state');settings.prepare()
    atomic_json(settings.state/'telegram.pending.json',{'token':'FAKE_SECRET','pair_code':'pair'})
    class Response:
        def json(self):return {'ok':True,'result':{'is_bot':True,'username':'aidj_codex_music_bot'}}
    monkeypatch.setattr('aidj.bootstrap.requests.post',lambda *args,**kwargs:Response())
    result=activate_pending(settings,attempts=1)
    assert result['username']=='aidj_codex_music_bot'
    assert not (settings.state/'telegram.pending.json').exists()
    active=settings.state/'telegram.json'
    assert active.stat().st_mode&0o777==0o600
    assert json.loads(active.read_text())['token']=='FAKE_SECRET'
