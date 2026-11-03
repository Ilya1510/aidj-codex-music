import json
from http.server import HTTPServer
import threading
import requests
import pytest

from aidj import reviews as R
from aidj.review_web import request,snapshot
from aidj.store import meta_get
from aidj.web_catalog import catalog,asset_id
from aidj.webapp import Handler,Server
from aidj.web_auth import create_session
from tests.test_playlists import env
from tests.test_reviews import setup, start, process


def test_bot_votes_arrive_in_catalog_and_owner_sees_names_without_private_metadata(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    start(setup);process(setup);card=db.execute('SELECT * FROM review_cards').fetchone()
    R.remember_profile(db,{'id':101,'username':'friend_one','first_name':'Friend'})
    R.vote(db,card['id'],101,101,-1)
    value=catalog(db);track=value['tracks'][0]
    assert track['rating']=={'likes':0,'dislikes':1,'neutral':0,'total':1,'mine':None,'proposals':0}
    assert value['review']['participants']==1 and value['review']['poolTrackIds']==[track['id']]
    details=request(db,{'action':'details','trackId':track['id']})
    assert details['people']==[{'label':'@friend_one','username':'friend_one','isMine':False,'vote':-1,'proposed':False}]
    encoded=json.dumps(details)
    assert not any(value in encoded for value in ('chat_id','remote_path','token','local_path','disk.yandex'))


def test_owner_vote_updates_shared_rating_is_idempotent_replaceable_and_can_be_reset(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    aid=catalog(db)['tracks'][0]['id'];body={'action':'vote','trackId':aid,'value':1,'opId':'vote-first-1'}
    first=request(db,body);second=request(db,body)
    assert first['rating']==second['rating'] and first['rating']['likes']==1
    assert db.execute('SELECT COUNT(*) FROM review_votes').fetchone()[0]==1
    with pytest.raises(R.ReviewError):request(db,{**body,'value':-1})
    changed=request(db,{**body,'value':-1,'opId':'vote-change-2'})
    assert changed['rating']['likes']==0 and changed['rating']['dislikes']==1 and changed['rating']['mine']==-1
    reset=request(db,{**body,'value':None,'opId':'vote-reset-3'})
    assert reset['rating']['total']==0 and reset['rating']['mine'] is None


def test_same_audio_aliases_do_not_double_count_one_person(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    a=R.remember(db,result);other=R.remember(db,{**result,'track':{**result['track'],'source':'youtube','id':'abcdefghijk'}})
    db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],a,101,1,1))
    db.execute('INSERT INTO review_votes VALUES (?,?,?,?,?)',(p['id'],other,101,-1,2));db.commit()
    track=catalog(db)['tracks'][0]
    assert track['rating']['total']==1 and track['rating']['dislikes']==1


def test_app_propose_and_archive_keep_saved_music_and_do_not_trigger_download(setup):
    settings,db,disk,bot,manager,result,p,*_=setup
    new={**result,'track':{**result['track'],'id':'456','title':'Suggestion'},'md5':'another-digest'}
    item=R.remember(db,new)
    # An owner search already provides the app asset; suggestions remain a separate membership.
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(item,'yandex:456','Suggestion','Artist',100,new['md5'],12,'/existing/suggestion.mp3'));db.commit()
    answer=request(db,{'action':'propose','trackId':item,'opId':'propose-from-app-1'})
    assert answer['rating']['proposals']==1 and answer['people'][0]['isMine']
    assert db.execute("SELECT status FROM review_proposals WHERE item_id=?",(item,)).fetchone()[0]=='confirmed'
    assert not db.execute('SELECT 1 FROM app_deliveries').fetchone()
    archived=request(db,{'action':'archive','trackId':item,'opId':'archive-from-app-1'})
    assert archived['rating']['proposals']==0
    assert db.execute('SELECT 1 FROM web_assets WHERE id=?',(item,)).fetchone()
    assert db.execute('SELECT 1 FROM review_saves WHERE item_id=?',(item,)).fetchone()


@pytest.mark.parametrize('value',[True,False,2,-2,'1',{},[]])
def test_invalid_vote_values_are_rejected(setup,value):
    settings,db,*_=setup;aid=catalog(db)['tracks'][0]['id']
    with pytest.raises(R.ReviewError):request(db,{'action':'vote','trackId':aid,'value':value,'opId':'invalid-vote-123'})


def test_review_http_requires_owner_session_and_csrf_header(setup):
    settings,db,*_=setup;aid=catalog(db)['tracks'][0]['id']
    server=Server(('127.0.0.1',0),Handler,settings);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}';body={'action':'vote','trackId':aid,'value':1,'opId':'http-vote-123'}
    try:
        assert requests.post(base+'/api/reviews',json=body,headers={'X-AIDJ-App':'1'}).status_code==401
        token=create_session(db);client=requests.Session();client.cookies.set('aidj_session',token)
        assert client.post(base+'/api/reviews',json=body).status_code==403
        response=client.post(base+'/api/reviews',json=body,headers={'X-AIDJ-App':'1'})
        assert response.status_code==200 and response.json()['rating']['mine']==1
    finally:server.shutdown();server.server_close();thread.join()
