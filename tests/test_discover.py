import json
import time
import threading
import pytest
import requests

from aidj import discover,worker
from aidj.sources import Track
from aidj.pipeline import SelectionNeeded
from aidj.store import claim_job,enqueue,job_update
from aidj.web_catalog import catalog
from aidj.web_auth import create_session
from aidj.webapp import Server,Handler
from tests.test_playlists import env


class Sources:
    def search(self,query,youtube_only=False):
        return [Track('yandex','123','Song','Artist','https://music.yandex.ru/track/123',123),
                Track('yandex','456','Song live','Artist','https://music.yandex.ru/track/456',140)]


def search(settings,db,disk):
    job=discover.submit(db,{'opId':'app-search-001','action':'search','query':'Artist Song'})
    row=claim_job(db);assert row['id']==job['id']
    assert discover.process(settings,db,row,Sources(),disk)==0
    return discover.public_job(discover.owned_job(db,job['id']))


def test_search_then_listen_reuses_library_without_modifying_playlist_or_redownloading(env,tmp_path):
    settings,db,disk,bot,manager,result=env
    playlist=manager.create('Volleyball');manager.add(playlist['id'],result,tmp_path)
    before=catalog(db)
    found=search(settings,db,disk)
    assert found['candidates'][0]['title']=='Song'
    assert found['candidates'][0]['inLibrary']
    assert 'url' not in found['candidates'][0] and 'remote_path' not in json.dumps(found)
    selected=discover.submit(db,{'opId':'app-prepare-001','action':'prepare','jobId':found['id'],'index':0,'track':{'url':'https://evil.invalid'}})
    assert discover.process(settings,db,claim_job(db),Sources(),disk)==0
    ready=discover.public_job(discover.owned_job(db,selected['id']))
    assert ready['trackId']==before['tracks'][0]['id']
    assert catalog(db)==before and disk.copies==1
    assert not bot.sent and not bot.edited


def test_selection_is_server_side_and_requests_are_idempotent(env):
    settings,db,disk,*_=env;found=search(settings,db,disk)
    body={'opId':'prepare-idempotent','action':'prepare','jobId':found['id'],'index':1}
    first=discover.submit(db,body);assert discover.submit(db,body)['id']==first['id']
    assert discover.submit(db,{**body,'opId':'second-double-click'})['id']==first['id']
    row=discover.owned_job(db,first['id']);assert json.loads(row['payload'])['track']['id']=='456'
    with pytest.raises(discover.DiscoverError):discover.submit(db,{**body,'index':0})
    for index in [-1,2,True]:
        with pytest.raises(discover.DiscoverError):discover.submit(db,{**body,'opId':'bad-index-001','index':index})


def test_expired_results_reject_new_preparation_but_allow_lost_response_retry(env):
    settings,db,disk,*_=env;found=search(settings,db,disk)
    body={'opId':'expiry-retry-001','action':'prepare','jobId':found['id'],'index':0}
    first=discover.submit(db,body)
    db.execute('UPDATE jobs SET created=0 WHERE id=?',(found['id'],));db.commit()
    assert discover.submit(db,body)['id']==first['id']
    with pytest.raises(discover.DiscoverError,match='устарели'):discover.submit(db,{**body,'opId':'new-expired-001'})


def test_bot_jobs_and_raw_tracks_cannot_be_used_as_app_selections(env):
    settings,db,disk,*_=env
    job=enqueue(db,'telegram-001',22,11,10,'secret request')
    with pytest.raises(discover.DiscoverError):discover.owned_job(db,job)
    assert discover.recent(db)=={'jobs':[]}
    with pytest.raises(discover.DiscoverError):discover.submit(db,{'opId':'raw-track-001','action':'prepare','track':{'url':'https://evil.invalid'}})


def test_known_link_works_when_source_lookup_is_unavailable(env,tmp_path):
    settings,db,disk,bot,manager,result=env
    playlist=manager.create('Volleyball');manager.add(playlist['id'],result,tmp_path)
    found=discover.submit(db,{'opId':'known-link-001','action':'search','query':'https://music.yandex.ru/track/123'})
    assert discover.process(settings,db,claim_job(db),object(),disk)==0
    assert discover.public_job(discover.owned_job(db,found['id']))['candidates'][0]['inLibrary']


def test_worker_never_initializes_telegram_for_app_jobs(env,monkeypatch):
    settings,db,disk,*_=env
    found=discover.submit(db,{'opId':'no-telegram-001','action':'search','query':'Artist Song'})
    claim_job(db)
    monkeypatch.setattr(discover,'Sources',lambda settings:Sources())
    monkeypatch.setattr(discover,'Disk',lambda settings:disk)
    monkeypatch.setattr(worker,'Telegram',lambda *args:pytest.fail('App jobs must not send Telegram messages'))
    assert worker.run(found['id'])==0


def test_fallback_requires_explicit_version_selection_then_returns_playable_asset(env,monkeypatch):
    settings,db,disk,*_=env;found=search(settings,db,disk)
    prepared=discover.submit(db,{'opId':'fallback-001','action':'prepare','jobId':found['id'],'index':0})
    class Pipeline:
        def __init__(self,*args):pass
        def run(self,*args,**kwargs):raise SelectionNeeded('Artist Song',[Track('youtube','abcdefghijk','Song remix','Artist','url',130)],True)
    monkeypatch.setattr(discover,'Pipeline',Pipeline)
    assert discover.process(settings,db,claim_job(db),Sources(),disk)==0
    choice=discover.public_job(discover.owned_job(db,prepared['id']))
    assert choice['status']=='awaiting_choice' and choice['candidates'][0]['source']=='youtube'
    second=discover.submit(db,{'opId':'fallback-choose-001','action':'prepare','jobId':choice['id'],'index':0})
    assert json.loads(discover.owned_job(db,second['id'])['payload'])['track']['id']=='abcdefghijk'


def test_new_recording_becomes_playable_without_playlist_membership(env,monkeypatch):
    settings,db,disk,bot,manager,result=env;found=search(settings,db,disk)
    prepared=discover.submit(db,{'opId':'fresh-prepare-001','action':'prepare','jobId':found['id'],'index':0})
    class Pipeline:
        def __init__(self,*args):pass
        def run(self,query,payload,folder,notify,need_local):
            assert need_local is False and payload['track']['id']=='123'
            notify('Сохраняю на Диск…')
            db.execute('INSERT INTO cache(key,result,created) VALUES (?,?,?)',('test-cache',json.dumps(result),time.time()));db.commit()
            return result
    monkeypatch.setattr(discover,'Pipeline',Pipeline)
    assert discover.process(settings,db,claim_job(db),Sources(),disk)==0
    answer=discover.public_job(discover.owned_job(db,prepared['id']))
    assert answer['trackId']==catalog(db)['tracks'][0]['id']
    assert catalog(db)['playlists']==[] and not bot.sent


def test_discovery_http_requires_owner_session_and_validates_external_requests(env):
    settings,db,*_=env;server=Server(('127.0.0.1',0),Handler,settings)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_port)+'/api/discover'
    client=requests.Session()
    try:
        assert client.get(base).status_code==401
        assert client.post(base,json={'action':'search','query':'Artist Song','opId':'http-search-001'},headers={'X-AIDJ-App':'1'}).status_code==401
        client.cookies.set('aidj_session',create_session(db))
        assert client.post(base,json={}).status_code==403
        headers={'X-AIDJ-App':'1'}
        reply=client.post(base,json={'action':'search','query':'Artist Song','opId':'http-search-001'},headers=headers)
        assert reply.status_code==200 and reply.json()['status']=='queued'
        assert client.get(base+'/'+str(reply.json()['id'])).json()['query']=='Artist Song'
        assert len(client.get(base).json()['jobs'])==1
        invalid=client.post(base,json={'action':'search','query':'https://evil.invalid/file','opId':'bad-url-001'},headers=headers)
        assert invalid.status_code==400
    finally:client.close();server.shutdown();server.server_close();thread.join()
