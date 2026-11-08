import json
import sqlite3
import threading
from urllib.parse import parse_qs,urlsplit

import pytest
import requests

from aidj.config import Settings
from aidj.store import connect,meta_set
from aidj.web_auth import create_session,session_access,browser_code,redeem_code,digest
from aidj.webapp import Server,Handler
from aidj.app_install import AppInstallUI
from tests.test_web import init_data


@pytest.fixture
def gateway(tmp_path,monkeypatch):
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    meta_set(db,'owner_id',11);meta_set(db,'owner_chat_id',11)
    (settings.state/'telegram.json').write_text(json.dumps({'token':'fake'}))
    (settings.state/'miniapp-url.json').write_text(json.dumps({'url':'https://music.example.com'}))
    asset=('a'*24,'test:a','Test song','Artist',180,'b'*32,100,'/private/track.mp3')
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',asset);db.commit()
    data={'tracks':[{'id':asset[0],'rating':{'mine':1}}],'playlists':[{'id':1,'name':'Volleyball','trackIds':[asset[0]],'diskUrl':'https://private.example'}]}
    monkeypatch.setattr('aidj.webapp.catalog',lambda db:json.loads(json.dumps(data)))
    server=Server(('127.0.0.1',0),Handler,settings);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield db,settings,'http://127.0.0.1:'+str(server.server_port)
    finally:server.shutdown();server.server_close();thread.join();db.close()


def headers(token):return {'Cookie':'aidj_session='+token,'X-AIDJ-App':'1'}


def test_listener_can_list_seek_download_and_cannot_modify_any_shared_state(gateway):
    db,settings,base=gateway;token=create_session(db,role='listener',user_id=22);h=headers(token)
    value=requests.get(base+'/api/catalog',headers=h).json()
    assert value['access']['role']=='listener' and not value['access']['editLibrary']
    assert 'diskUrl' not in value['playlists'][0] and value['tracks'][0]['rating']['mine'] is None
    assert requests.head(base+'/api/audio/'+'a'*24,headers=h).status_code==200
    assert requests.head(base+'/api/audio/'+'a'*24,headers={**h,'Range':'bytes=5-9'}).status_code==206
    for path,body in [('/api/edit',{'action':action,'playlistId':1,'name':'Hacked','trackIds':[],'role':'admin'}) for action in ('create','rename','add','remove','reorder','combine','delete-combined')]+[
        ('/api/reviews',{'action':'vote','value':1}),('/api/range',{'trackId':'a'*24,'start':0,'end':10}),
        ('/api/dj',{}),('/api/discover',{'action':'search','query':'new song'}),('/api/import-register',{})]:
        assert requests.post(base+path,headers={**h,'X-AIDJ-Role':'admin'},json=body).status_code==403,path
    for path in ('/api/discover','/api/discover/1','/api/import-current'):
        assert requests.get(base+path,headers=h).status_code==403
    assert db.execute('SELECT COUNT(*) FROM web_assets').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM playlists').fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM review_votes').fetchone()[0]==0
    assert requests.post(base+'/api/device-inbox',headers=h,json={'deviceId':'fake','djPriority':['a'*24]}).json()['items']==[]


def test_listener_pairing_links_never_escalate_even_when_role_is_spoofed(gateway):
    db,settings,base=gateway;token=create_session(db,role='listener',user_id=22);h=headers(token)
    for path,parameter in [('/api/browser-link','login'),('/api/android-connect','code')]:
        result=requests.post(base+path,headers=h,json={'role':'admin','userId':11}).json()
        code=parse_qs(urlsplit(result['url']).fragment)[parameter][0]
        response=requests.post(base+'/api/auth',headers={'X-AIDJ-App':'1'},json={'loginCode':code,'role':'admin'})
        assert response.status_code==200 and response.json()['access']['role']=='listener'
        assert response.json()['access']['userId']==22
        assert requests.post(base+'/api/auth',headers={'X-AIDJ-App':'1'},json={'loginCode':code}).status_code==403
    for user,role in [(22,'listener'),(11,'admin')]:
        response=requests.post(base+'/api/auth',headers={'X-AIDJ-App':'1'},json={'initData':init_data(user=user),'role':'admin'})
        assert response.status_code==200 and response.json()['access']['role']==role
    assert requests.post(base+'/api/auth',headers={'X-AIDJ-App':'1'},json={'initData':init_data(user=11,token='wrong')}).status_code==403


def test_existing_owner_sessions_and_codes_survive_role_migration(tmp_path):
    settings=Settings(state=tmp_path/'state');settings.prepare();old=sqlite3.connect(settings.state/'bot.sqlite')
    old.execute('CREATE TABLE web_sessions(digest TEXT PRIMARY KEY,expires REAL NOT NULL)')
    old.execute('CREATE TABLE web_login_codes(digest TEXT PRIMARY KEY,expires REAL NOT NULL)')
    old.execute('INSERT INTO web_sessions VALUES (?,?)',(digest('legacy'),9999999999))
    old.execute('INSERT INTO web_login_codes VALUES (?,?)',(digest('old-code'),9999999999));old.commit();old.close()
    db=connect(settings)
    assert session_access(db,'legacy')['editLibrary']
    assert session_access(db,redeem_code(db,'old-code'))['role']=='admin'
    listener=create_session(db,role='listener',user_id=22)
    db.execute('UPDATE web_sessions SET expires=0 WHERE digest=?',(digest(listener),));db.commit()
    assert session_access(db,listener) is None
    orphan=create_session(db,role='listener',user_id=22)
    db.execute("DELETE FROM web_access WHERE kind='session' AND digest=?",(digest(orphan),));db.commit()
    assert session_access(db,orphan) is None
    db.close();db=connect(settings);assert session_access(db,orphan) is None;db.close()


class Bot:
    def __init__(self):self.messages=[];self.documents=[]
    def message(self,*args):self.messages.append(args)
    def document(self,*args):self.documents.append(args)
    def call(self,*args):pass


def test_public_apk_command_sends_latest_file_and_listener_only_identity(gateway):
    from aidj.bot import handle_update
    db,settings,base=gateway;bot=Bot();ui=AppInstallUI(db,bot,settings)
    release=settings.state/'android-release';(release/'releases/99').mkdir(parents=True)
    (release/'version.json').write_text(json.dumps({'versionCode':99,'versionName':'test'}))
    (release/'releases/99/AI-DJ.apk').write_bytes(b'test apk')
    message=lambda text:{'message':{'text':text,'chat':{'type':'private','id':22},'from':{'id':22}}}
    handle_update(db,{},bot,{'update_id':1,**message('/apk')},settings)
    assert bot.messages[-1][2][0][0]['callback_data']=='appinstall:android'
    handle_update(db,{},bot,{'update_id':2,'callback_query':{'id':'qa-callback','data':'appinstall:android','message':{'chat':{'type':'private','id':22}},'from':{'id':22}}},settings)
    file=bot.documents[-1];assert file[1].read_bytes()==b'test apk'
    code=parse_qs(urlsplit(file[3][0][0]['url']).fragment)['code'][0]
    assert session_access(db,redeem_code(db,code))=={'role':'listener','userId':22,'editLibrary':False,'rateSongs':False,'discoverSongs':False,'personalPlaylists':True}
    assert ui.message(message('/ios'),False)
    code=parse_qs(urlsplit(bot.messages[-1][2][0][0]['url']).fragment)['login'][0]
    assert session_access(db,redeem_code(db,code))['role']=='listener'
    assert not ui.message(message('/android'),True)
