import json
from urllib.parse import urlsplit,parse_qs

from aidj.android_release import connection_url,version
from aidj.config import Settings,atomic_json
from aidj.store import connect
from aidj.web_auth import redeem_code,session_valid,AuthError
from aidj.webapp import Server,Handler
import requests
import threading
import pytest


def test_android_connection_is_single_use_and_never_puts_code_in_http_path(tmp_path):
    s=Settings(state=tmp_path/'state');db=connect(s)
    url=connection_url(db,'https://music.example.com')
    parsed=urlsplit(url);values=parse_qs(parsed.fragment)
    assert parsed.path=='/android-connect' and not parsed.query
    assert values['server']==['https://music.example.com']
    token=redeem_code(db,values['code'][0]);assert session_valid(db,token)
    with pytest.raises(AuthError):redeem_code(db,values['code'][0])


def test_apk_release_download_is_immutable_and_supports_system_download_resumption(tmp_path):
    s=Settings(state=tmp_path/'state');s.prepare()
    root=s.state/'android-release';(root/'releases/1').mkdir(parents=True)
    (root/'releases/1/AI-DJ.apk').write_bytes(b'SIGNED_TEST_APK')
    atomic_json(root/'version.json',{'versionCode':1,'versionName':'0.2.0','sha256':'digest','bytes':15,'signerSha256':'fingerprint'})
    atomic_json(s.state/'miniapp-url.json',{'url':'https://test.trycloudflare.com'})
    assert version(s,'https://music.example.com')['url']=='https://music.example.com/android/releases/1/AI-DJ.apk'
    server=Server(('127.0.0.1',0),Handler,s);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_port)
    try:
        r=requests.get(base+'/api/android-version');assert r.status_code==200 and r.json()['versionCode']==1
        r=requests.get(base+'/android/releases/1/AI-DJ.apk',headers={'Range':'bytes=7-10'})
        assert r.status_code==206 and r.content==b'TEST'
        assert r.headers['Content-Type']=='application/vnd.android.package-archive'
        assert 'attachment' in r.headers['Content-Disposition']
        assert requests.get(base+'/android/releases/../identity.json').status_code in (401,404)
        assert requests.get(base+'/api/catalog').status_code==401
    finally:server.shutdown();server.server_close();thread.join()


def test_android_shell_allows_every_local_frontend_module_import():
    # New JS files can pass browser QA but fail on the APK's asset allowlist.
    from pathlib import Path
    import re
    root=Path(__file__).resolve().parents[1]
    native=(root/'android/src/com/aidj/music/MainActivity.java').read_text()
    block=native.split('Set<String> allowed=',1)[1].split('if(!allowed.contains',1)[0]
    allowed=set(re.findall(r'"(/[^" ]+)"',block))
    for file in (root/'miniapp').glob('*.mjs'):
        for module in re.findall(r"from ['\"]\./([^'\"]+)['\"]",file.read_text()):
            assert '/'+module in allowed,(file.name,module)
            assert (root/'miniapp'/module).is_file()


def test_blended_fixed_set_requires_native39_but_clean_cuts_remain_compatible(monkeypatch):
    from aidj.webapp import client_catalog
    import copy
    data={'tracks':[], 'playlists':[], 'djSets':[
        {'id':'clean','tracks':[{'transitionSeconds':0}]},
        {'id':'blend','tracks':[{'transitionSeconds':3}]}]}
    monkeypatch.setattr('aidj.webapp.catalog',lambda db:copy.deepcopy(data))
    for version in ('37','38','garbage'):
        assert [s['id'] for s in client_catalog(None,version)['djSets']]==['clean']
    for version in (None,'39','40'):
        assert [s['id'] for s in client_catalog(None,version)['djSets']]==['clean','blend']


def test_catalog_only_sends_the_cue_schema_supported_by_the_native_player(tmp_path,monkeypatch):
    from aidj.web_auth import create_session
    import copy
    s=Settings(state=tmp_path/'state');db=connect(s);token=create_session(db);db.close()
    value={'tracks':[{'id':'a'*24,'djV4':{'cues':[{'fade':.25}],'anchors':{'version':1},'acousticAnchors':{'quality':'acoustic'}},'djV3':{'cues':[{'fade':.5}]},'djV2':{},'dj':{}}],'playlists':[]}
    monkeypatch.setattr('aidj.webapp.catalog',lambda db:copy.deepcopy(value))
    server=Server(('127.0.0.1',0),Handler,s);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)+'/api/catalog'
    try:
        for version,modern in [(None,False),('19',False),('20',True),('21',True),('garbage',False)]:
            headers={'Cookie':'aidj_session='+token}
            if version:headers['X-AIDJ-Version']=version
            response=requests.get(url,headers=headers);assert response.status_code==200
            track=response.json()['tracks'][0]
            assert ('djV4' in track)==modern
            assert ('djV3' in track)!=modern
            if modern:
                assert ('anchors' in track['djV4'])==(version=='21')
                assert ('acousticAnchors' in track['djV4'])==(version=='21')
    finally:server.shutdown();server.server_close();thread.join()
