import hashlib
import hmac
import json
import threading
import time
from urllib.parse import urlencode
from types import SimpleNamespace

import pytest
import requests

from aidj.config import Settings
from aidj.store import connect
from aidj.web_auth import AuthError,verify_telegram,browser_code,redeem_code,session_valid
from aidj.web_catalog import catalog,mutate,revision,Conflict
from aidj.webapp import Server,Handler,byte_range
from tests.test_playlists import env


def init_data(user=11,now=None,token='fake',**extra):
    values={'auth_date':str(int(time.time() if now is None else now)),'user':json.dumps({'id':user}),**extra}
    key=hmac.digest(b'WebAppData',token.encode(),'sha256')
    values['hash']=hmac.new(key,'\n'.join(k+'='+v for k,v in sorted(values.items())).encode(),'sha256').hexdigest()
    return urlencode(values)


def test_telegram_signature_owner_and_age_are_verified():
    valid=init_data(now=1000)
    assert verify_telegram(valid,'fake',11,now=1001)==11
    for raw,token,owner,now in [(valid,'other',11,1001),(valid,'fake',12,1001),
                               (valid,'fake',11,90000),(valid,'fake',11,800),
                               (valid+'&user=other','fake',11,1001)]:
        with pytest.raises((AuthError,ValueError)):verify_telegram(raw,token,owner,now=now)


def test_browser_pairing_is_single_use_and_expiring(tmp_path):
    db=connect(Settings(state=tmp_path/'state'))
    code=browser_code(db);token=redeem_code(db,code)
    assert session_valid(db,token)
    with pytest.raises(AuthError):redeem_code(db,code)
    code=browser_code(db)
    db.execute('UPDATE web_login_codes SET expires=0');db.commit()
    with pytest.raises(AuthError):redeem_code(db,code)
    assert not session_valid(db,'wrong')


@pytest.mark.parametrize('value,size,expected',[(None,10,(0,9,False)),('bytes=0-3',10,(0,3,True)),
    ('bytes=5-',10,(5,9,True)),('bytes=-4',10,(6,9,True)),('bytes=2-50',10,(2,9,True))])
def test_audio_seek_ranges(value,size,expected):assert byte_range(value,size)==expected


@pytest.mark.parametrize('value',['bytes=10-','bytes=7-3','bytes=-0','bytes=0-2,4-6','nonsense','bytes=-'])
def test_invalid_ranges_are_rejected(value):
    with pytest.raises(ValueError):byte_range(value,10)


@pytest.fixture
def prepared(env,tmp_path,monkeypatch):
    settings,db,disk,bot,manager,result=env
    p=manager.create('Volleyball');manager.add(p['id'],result,tmp_path)
    values=catalog(db);track=values['tracks'][0]
    def move(source,target):
        if source in disk.files:disk.files[target]=disk.files.pop(source)
        elif source in disk.dirs:
            for path in list(disk.files):
                if path.startswith(source+'/'):disk.files[target+path[len(source):]]=disk.files.pop(path)
            disk.dirs={target+path[len(source):] if path==source or path.startswith(source+'/') else path for path in disk.dirs}
        else:raise ValueError('source absent')
    disk.move=move
    return settings,db,disk,manager,result,p,track


def test_remove_preserves_library_and_can_add_back_without_download(prepared,tmp_path):
    settings,db,disk,manager,result,p,track=prepared
    before=revision(db,p['id'])
    body={'opId':'remove-song-001','action':'remove','playlistId':p['id'],'trackId':track['id'],'revision':before}
    mutate(settings,db,disk,body,tmp_path)
    after=catalog(db)
    assert after['playlists'][0]['trackIds']==[] and len(after['tracks'])==1
    assert db.execute('SELECT remote_path FROM web_assets').fetchone()[0].startswith('/Музыка/_aidj_removed/')
    mutate(settings,db,disk,body,tmp_path)  # Lost HTTP response can be retried.
    mutate(settings,db,disk,{'opId':'add-song-again','action':'add','playlistId':p['id'],'trackId':track['id'],
                           'revision':revision(db,p['id'])},tmp_path)
    assert catalog(db)['playlists'][0]['trackIds']==[track['id']]
    assert len([x for x in disk.files if x.startswith('/Музыка/Volleyball/') and x.endswith('.mp3')])==1


def test_rename_moves_folder_updates_paths_and_index(prepared,tmp_path):
    settings,db,disk,manager,result,p,track=prepared
    body={'opId':'rename-playlist','action':'rename','playlistId':p['id'],'name':'Волейбол',
          'revision':revision(db,p['id'])}
    mutate(settings,db,disk,body,tmp_path)
    after=catalog(db)
    assert after['playlists'][0]['name']=='Волейбол'
    assert '/Музыка/Волейбол' in disk.dirs
    assert '/Музыка/Волейбол/Волейбол.m3u8' in disk.files
    assert all(not path.startswith('/Музыка/Volleyball/') for path in disk.files)
    assert db.execute('SELECT remote_path FROM web_assets').fetchone()[0].startswith('/Музыка/Волейбол/')
    mutate(settings,db,disk,body,tmp_path)


def test_reorder_and_stale_revision_protect_against_lost_edits(prepared,tmp_path):
    settings,db,disk,manager,result,p,track=prepared
    old=revision(db,p['id'])
    second={**result,'track':{**result['track'],'id':'456','title':'Another Song'}}
    body=b'Another audio file'
    second.update(remote_path='/Музыка/aidj_codex_music/another.mp3',md5=hashlib.md5(body).hexdigest(),bytes=len(body))
    disk.files[second['remote_path']]=body
    manager.add(p['id'],second,tmp_path)
    values=catalog(db);ids=values['playlists'][0]['trackIds']
    with pytest.raises(Conflict):mutate(settings,db,disk,{'opId':'stale-reorder','action':'reorder','playlistId':p['id'],
        'trackIds':list(reversed(ids)),'revision':old},tmp_path)
    mutate(settings,db,disk,{'opId':'valid-reorder','action':'reorder','playlistId':p['id'],
        'trackIds':list(reversed(ids)),'revision':revision(db,p['id'])},tmp_path)
    assert catalog(db)['playlists'][0]['trackIds']==list(reversed(ids))
    assert disk.files['/Музыка/Volleyball/Volleyball.m3u8'].decode().index('Artist — Song') < disk.files['/Музыка/Volleyball/Volleyball.m3u8'].decode().index('Another Song')


def test_authenticated_http_catalog_and_range_stream_do_not_expose_disk_credentials(prepared,monkeypatch):
    settings,db,disk,manager,result,p,track=prepared
    monkeypatch.setattr('aidj.webapp.Disk',lambda settings:disk)
    disk.request=lambda *args,**kwargs:SimpleNamespace(status_code=200,json=lambda:{'href':'https://example.invalid/PRIVATE_SIGNED_URL'})
    class Upstream:
        status_code=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,size):yield b'sample-audio'
    monkeypatch.setattr('aidj.webapp.requests.get',lambda *args,**kwargs:Upstream())
    server=Server(('127.0.0.1',0),Handler,settings)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=requests.Session();base='http://127.0.0.1:'+str(server.server_port)
    try:
        assert client.request('GET',base+'/api/catalog').status_code==401
        assert client.request('GET',base+'/').status_code==200
        assert client.request('POST',base+'/api/auth',json={'initData':init_data()}).status_code==403
        r=client.request('POST',base+'/api/auth',json={'initData':init_data()},headers={'X-AIDJ-App':'1'})
        assert r.status_code==200
        assert 'HttpOnly' in r.headers['Set-Cookie'] and 'Secure' in r.headers['Set-Cookie']
        cookie=r.headers['Set-Cookie'].split(';')[0]
        headers={'Cookie':cookie}
        r=client.request('GET',base+'/api/catalog',headers=headers)
        assert r.status_code==200
        assert 'remote_path' not in r.text and 'PRIVATE_SIGNED_URL' not in r.text
        r=client.request('GET',base+'/api/audio/'+track['id'],headers={**headers,'Range':'bytes=3-7'})
        assert r.status_code==206 and r.content==b'ple-a'
        assert r.headers['Content-Range']=='bytes 3-7/12'
        r=client.request('POST',base+'/api/edit',json={'action':'create'},headers={**headers,'X-AIDJ-App':'1','Origin':'https://attacker.invalid'})
        assert r.status_code==403
        assert client.request('GET',base+'/../../.local/share/telegram.json').status_code in (401,404)
    finally:
        client.close();server.shutdown();server.server_close();thread.join()
