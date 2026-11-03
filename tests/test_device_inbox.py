import json
import threading
import pytest
import requests
from aidj.bot import handle_update
from aidj.device_inbox import pending,acknowledge,publish,state,library_revision
from aidj.store import claim_job
from aidj import worker
from aidj.web_catalog import catalog
from aidj.webapp import Server,Handler
from aidj.web_auth import create_session
from tests.test_playlists import env,update

DEVICE='phone-000000000001'


def receipt(item,status='ready'):
    return {'id':item['id'],'revision':item['revision'],'md5':item['track']['md5'],'status':status}


def test_single_bot_click_queues_phone_download_without_confirmation_or_messages(env):
    settings,db,disk,bot,manager,result=env;sid=manager.selection(22,11,result)
    db.execute('UPDATE playlist_selections SET message_id=50 WHERE id=?',(sid,));db.commit()
    handle_update(db,{},bot,update(data='pd:'+sid,n=10),settings)
    handle_update(db,{},bot,update(data='pd:'+sid,n=10),settings)
    handle_update(db,{},bot,update(data='pd:'+sid,n=11),settings)
    assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    assert worker.run(claim_job(db)['id'])==0
    assert not bot.sent and not bot.edited and bot.markups
    values=pending(db,{'deviceId':DEVICE})
    assert len(values['items'])==1 and values['items'][0]['track']['md5']==result['md5']
    assert catalog(db)['playlists']==[]
    buttons=[b for row in manager.keyboard(sid) for b in row]
    assert not any(b.get('callback_data','').startswith('pa:') for b in buttons)
    assert any('Загрузка в приложение' in b['text'] for b in buttons)


def test_only_verified_ready_receipt_marks_downloaded_and_updates_existing_keyboard(env):
    settings,db,disk,bot,manager,result=env;sid=manager.selection(22,11,result)
    db.execute('UPDATE playlist_selections SET message_id=50 WHERE id=?',(sid,));db.commit()
    publish(db,result);item=pending(db,{'deviceId':DEVICE})['items'][0]
    acknowledge(db,{'deviceId':DEVICE,'items':[receipt(item,'queued')]})
    assert pending(db,{'deviceId':DEVICE})['items']==[] and state(db,result['md5'])=='queued'
    assert 'Скачано в приложении' not in str(manager.keyboard(sid))
    acknowledge(db,{'deviceId':DEVICE,'items':[receipt(item)]})
    assert state(db,result['md5'])=='ready' and '✓ Скачано в приложении' in str(manager.keyboard(sid))
    assert db.execute('SELECT sid FROM app_keyboard_updates').fetchone()[0]==sid
    acknowledge(db,{'deviceId':DEVICE,'items':[receipt(item,'queued')]})
    assert state(db,result['md5'])=='ready'


def test_download_requests_survive_restarts_are_per_phone_and_reject_stale_receipts(env):
    settings,db,disk,bot,manager,result=env
    publish(db,result);first=pending(db,{'deviceId':DEVICE})['items'][0]
    acknowledge(db,{'deviceId':DEVICE,'items':[receipt(first)]})
    assert len(pending(db,{'deviceId':'phone-000000000002'})['items'])==1
    publish(db,result);second=pending(db,{'deviceId':DEVICE})['items'][0]
    assert first['id']==second['id'] and second['revision']==first['revision']+1
    acknowledge(db,{'deviceId':DEVICE,'items':[receipt(first)]})
    assert len(pending(db,{'deviceId':DEVICE})['items'])==1 and state(db,result['md5'])=='queued'
    acknowledge(db,{'deviceId':DEVICE,'items':[{**receipt(second),'md5':'wrong'}]})
    assert len(pending(db,{'deviceId':DEVICE})['items'])==1


def test_bot_playlist_clicks_share_app_catalog_and_revision_and_allow_immediate_multiple_choices(env):
    settings,db,disk,bot,manager,result=env
    one=manager.create('One');two=manager.create('Two');sid=manager.selection(22,11,result)
    before=library_revision(db)
    for index,p in enumerate((one,two),1):handle_update(db,{},bot,update(data=f'pl:{sid}:{p["id"]}',n=index),settings)
    handle_update(db,{},bot,update(data=f'pd:{sid}',n=3),settings)
    for _ in range(3):assert worker.run(claim_job(db)['id'])==0
    data=catalog(db);assert all(len(p['trackIds'])==1 for p in data['playlists'])
    assert len(data['tracks'])==1 and library_revision(db)!=before and not bot.sent and not bot.edited
    item=pending(db,{'deviceId':DEVICE})['items'][0]
    assert all(item['track']['id'] in p['trackIds'] for p in data['playlists'])


def test_invalid_devices_and_unbounded_receipts_are_rejected(env):
    settings,db,*_=env
    for value in ('',True,'short','x'*65):
        with pytest.raises(ValueError):pending(db,{'deviceId':value})
    with pytest.raises(ValueError):acknowledge(db,{'deviceId':DEVICE,'items':[{}]*101})


def test_inbox_http_is_authenticated_and_exposes_only_public_track_metadata(env):
    settings,db,disk,bot,manager,result=env;publish(db,result)
    server=Server(('127.0.0.1',0),Handler,settings);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=requests.Session();base='http://127.0.0.1:'+str(server.server_port)+'/api/';headers={'X-AIDJ-App':'1'}
    try:
        assert client.post(base+'device-inbox',json={'deviceId':DEVICE},headers=headers).status_code==401
        client.cookies.set('aidj_session',create_session(db))
        response=client.post(base+'device-inbox',json={'deviceId':DEVICE},headers=headers);assert response.status_code==200
        data=response.json();assert len(data['items'])==1
        assert all(value not in response.text for value in ('remote_path','access_token','disk_url','telegram'))
        assert client.get(base+'library-version').json()['revision']==data['revision']
        assert client.post(base+'device-ack',json={'deviceId':DEVICE,'items':[receipt(data['items'][0])]},headers=headers).status_code==200
        assert client.post(base+'device-inbox',json={'deviceId':DEVICE},headers=headers).json()['items']==[]
    finally:client.close();server.shutdown();server.server_close();thread.join()
