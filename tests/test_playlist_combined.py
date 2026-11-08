"""Dynamic references share tracks and never mutate source folders or votes."""
import json
import sqlite3

import pytest

from aidj.backup import snapshot
from aidj.config import Settings
from aidj.device_inbox import library_revision
from aidj.playlist_combined import BASE,records
from aidj.playlists import PlaylistError,Playlists
from aidj.store import connect
from aidj.web_catalog import asset_id,catalog,mutate,Conflict,revision


class NoDisk:
    def __getattr__(self,key):raise AssertionError('Combined operation accessed Disk: '+key)


@pytest.fixture
def prepared(tmp_path):
    settings=Settings(state=tmp_path/'state');db=connect(settings,dj_enrol=False)
    for i,name in enumerate(('Volleyball','Предложка','Other'),1):
        db.execute('INSERT INTO playlists VALUES (?,?,?,?,0)',(i,name,name.casefold(),'/Музыка/'+name))
    def track(pid,key,md5,position,state='complete'):
        db.execute('INSERT INTO playlist_tracks VALUES (?,?,?,?,?,?,?,?,?)',
            (pid,key,position,'/Музыка/source/'+key+'.mp3',md5,123,60,'Artist — '+key,state))
    track(1,'youtube:a','a'*32,1);track(1,'youtube:b','b'*32,2)
    track(2,'youtube:alias-a','a'*32,1);track(2,'youtube:c','c'*32,2)
    track(2,'youtube:pending','d'*32,3,'pending');db.commit()
    return settings,db,tmp_path,track


def edit(env,action='combine',op='combine-001',**values):
    settings,db,work,_=env
    mutate(settings,db,NoDisk(),{'action':action,'opId':op,**values},work)


def create(env,name='Together',sources=None,op='combine-001'):
    edit(env,name=name,sourcePlaylistIds=[1,2] if sources is None else sources,op=op)
    return records(env[1])[-1]


def test_combined_http_needs_owner_session_but_not_disk_credentials_and_hides_from_old_apks(prepared,monkeypatch):
    import threading
    import requests
    from aidj.web_auth import create_session
    from aidj.webapp import Server,Handler
    settings,db,work,_=prepared;token=create_session(db)
    def forbidden(*args,**kwargs):raise AssertionError('DB-only aggregation constructed a Disk client')
    monkeypatch.setattr('aidj.webapp.Disk',forbidden)
    server=Server(('127.0.0.1',0),Handler,settings);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    body={'action':'combine','opId':'http-combine-27','name':'Listening union','sourcePlaylistIds':[1,2]}
    auth={'Cookie':'aidj_session='+token,'X-AIDJ-App':'1','X-AIDJ-Version':'27'}
    try:
        assert requests.post(url+'/api/edit',json=body,headers={'X-AIDJ-App':'1'}).status_code==401
        assert requests.post(url+'/api/edit',json=body,headers={'Cookie':'aidj_session='+token}).status_code==403
        response=requests.post(url+'/api/edit',headers=auth,json=body);assert response.status_code==200,response.text
        combined=[p for p in response.json()['playlists'] if p.get('combined')]
        assert len(combined)==1 and combined[0]['trackIds']==[asset_id('youtube:'+key) for key in ('a','b','c')]
        assert response.json()['combinedSupported'] is True
        for version in ('26','19','garbage'):
            old=requests.get(url+'/api/catalog',headers={**auth,'X-AIDJ-Version':version}).json()
            assert not old['combinedSupported'] and not any(p.get('combined') for p in old['playlists'])
        browser=requests.get(url+'/api/catalog',headers={'Cookie':'aidj_session='+token}).json()
        assert browser['combinedSupported'] and any(p.get('combined') for p in browser['playlists'])
        assert requests.post(url+'/api/edit',headers=auth,json=body).status_code==200
        assert db.execute('SELECT COUNT(*) FROM playlist_combined').fetchone()[0]==1
    finally:server.shutdown();server.server_close();thread.join()


def test_combined_catalog_is_ordered_md5_union_with_global_track_ids_and_no_source_changes(prepared):
    settings,db,work,track=prepared
    before=[tuple(r) for r in db.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]
    ordinary=[tuple(r) for r in db.execute('SELECT * FROM playlists ORDER BY id')]
    initial=catalog(db);one=create(prepared);now=catalog(db)
    assert initial['combinedSupported'] is True and now['combinedSupported'] is True
    assert one['id']==BASE+1 and one['combined'] and one['sourcePlaylistIds']==[1,2]
    assert one['trackIds']==[asset_id('youtube:'+key) for key in ('a','b','c')]
    assert now['playlists'][-1]==one and now['playlists'][:3]==initial['playlists']
    assert now['tracks']==initial['tracks']
    assert [tuple(r) for r in db.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]==before
    assert [tuple(r) for r in db.execute('SELECT * FROM playlists ORDER BY id')]==ordinary
    assert db.execute('SELECT COUNT(*) FROM review_votes').fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM app_deliveries').fetchone()[0]==0


def test_source_add_remove_reorder_are_reflected_without_copying_or_stale_memberships(prepared):
    settings,db,work,track=prepared;one=create(prepared)
    track(2,'youtube:e','e'*32,4);db.commit();two=records(db)[0]
    assert two['revision']!=one['revision'] and two['trackIds'][-1]==asset_id('youtube:e')
    db.execute("DELETE FROM playlist_tracks WHERE playlist_id=1 AND track_key='youtube:a'");db.commit()
    three=records(db)[0]
    assert three['trackIds'][:2]==[asset_id('youtube:b'),asset_id('youtube:alias-a')]
    assert three['revision']!=two['revision']
    # The same MD5 from a later source remains a single track using its first
    # currently present occurrence, so source deletion does not lose music.
    assert len(three['trackIds'])==4
    db.execute('UPDATE playlist_tracks SET position=-position WHERE playlist_id=2')
    for position,key in enumerate(('youtube:e','youtube:alias-a','youtube:c','youtube:pending'),1):
        db.execute('UPDATE playlist_tracks SET position=? WHERE playlist_id=2 AND track_key=?',(position,key))
    db.commit();four=records(db)[0]
    assert four['revision']!=three['revision']
    assert four['trackIds']==[asset_id(k) for k in ('youtube:b','youtube:e','youtube:alias-a','youtube:c')]


def test_source_order_is_explicit_and_changes_which_duplicate_id_is_canonical(prepared):
    one=create(prepared,sources=[2,1])
    assert one['trackIds']==[asset_id(k) for k in ('youtube:alias-a','youtube:c','youtube:b')]


def test_create_rename_delete_are_atomic_idempotent_and_changed_replay_is_rejected(prepared):
    _,db,_,_=prepared;one=create(prepared)
    assert create(prepared)==one
    with pytest.raises(PlaylistError):create(prepared,name='Different')
    assert db.execute('SELECT COUNT(*) FROM playlist_combined').fetchone()[0]==1
    body=dict(playlistId=one['id'],name='Renamed',revision=one['revision'])
    edit(prepared,'rename-combined','rename-001',**body)
    edit(prepared,'rename-combined','rename-001',**body)
    renamed=records(db)[0];assert renamed['name']=='Renamed' and renamed['revision']!=one['revision']
    body=dict(playlistId=one['id'],revision=renamed['revision'])
    edit(prepared,'delete-combined','delete-001',**body)
    edit(prepared,'delete-combined','delete-001',**body)
    assert not records(db)
    assert db.execute("SELECT COUNT(*) FROM web_operations WHERE state='complete'").fetchone()[0]==3
    assert create(prepared,op='combine-002')['id']>one['id']


def test_stale_revision_after_source_update_protects_rename_and_delete(prepared):
    _,db,_,track=prepared;one=create(prepared)
    track(2,'youtube:e','e'*32,4);db.commit()
    for action in ('rename-combined','delete-combined'):
        with pytest.raises(Conflict):edit(prepared,action,'stale-'+action,playlistId=one['id'],name='Oops',revision=one['revision'])
    assert records(db)[0]['name']=='Together'


def test_failed_operation_receipt_rolls_back_definition_and_can_be_retried(prepared):
    _,db,_,_=prepared
    db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON web_operations BEGIN SELECT RAISE(ABORT,'test rejection'); END")
    with pytest.raises(sqlite3.IntegrityError):create(prepared)
    assert not records(db)
    assert db.execute('SELECT COUNT(*) FROM web_operations').fetchone()[0]==0
    db.execute('DROP TRIGGER reject_receipt');db.commit()
    assert create(prepared)['id']==BASE+1


@pytest.mark.parametrize('value',[1,True,1.1,str(BASE+1),BASE,2**53])
def test_combined_edit_rejects_real_or_invalid_id_namespaces(prepared,value):
    with pytest.raises(PlaylistError):edit(prepared,'delete-combined','bad-namespace',playlistId=value,revision='a'*20)


@pytest.mark.parametrize('value',[None,True,123,'tiny','x'*101,'path/slash'])
def test_operation_ids_are_validated_before_any_write(prepared,value):
    with pytest.raises(PlaylistError):create(prepared,op=value)
    assert not records(prepared[1])
    assert prepared[1].execute('SELECT COUNT(*) FROM web_operations').fetchone()[0]==0


@pytest.mark.parametrize('sources',[None,[],[1],[1,1],[1,2,2],[True,2],[1.0,2],['1',2],[1,-2],[1,999],[1,900000021],[1,BASE+1],list(range(1,32))])
def test_invalid_virtual_unknown_duplicate_and_unbounded_sources_are_rejected(prepared,sources):
    with pytest.raises(PlaylistError):edit(prepared,name='Bad',sourcePlaylistIds=sources)
    assert not records(prepared[1])
    assert prepared[1].execute('SELECT COUNT(*) FROM web_operations').fetchone()[0]==0


@pytest.mark.parametrize('name',[None,True,3,'','../escape','_hidden','x'*65,'Volleyball','ПРЕДЛОЖКА'])
def test_names_are_validated_against_real_and_combined_playlists(prepared,name):
    with pytest.raises(PlaylistError):create(prepared,name=name)
    assert not records(prepared[1])


def test_names_collide_both_directions_in_bot_create_and_web_rename(prepared):
    settings,db,work,_=prepared;one=create(prepared)
    with pytest.raises(PlaylistError):create(prepared,name='TOGETHER',op='combine-002')
    with pytest.raises(PlaylistError):Playlists(settings,db,NoDisk()).create('TOGETHER')
    with pytest.raises(PlaylistError):edit(prepared,'rename','ordinary-rename',playlistId=3,name='Together',revision=revision(db,3))
    with pytest.raises(PlaylistError):edit(prepared,'rename-combined','combined-rename',playlistId=one['id'],name='OTHER',revision=one['revision'])


def test_combined_count_is_bounded_and_ordinary_mutations_cannot_edit_aggregate(prepared):
    _,db,_,_=prepared
    for i in range(30):create(prepared,name='Together '+str(i),op='combine-'+str(i).zfill(3))
    with pytest.raises(PlaylistError):create(prepared,name='Too much',op='combine-031')
    one=records(db)[0]
    for action in ('add','remove','reorder','rename'):
        with pytest.raises(PlaylistError):edit(prepared,action,'wrong-action-'+action,playlistId=one['id'],name='Oops',revision=one['revision'],trackId=one['trackIds'][0],trackIds=[])
    assert len(records(db))==30


def test_combined_changes_reach_other_devices_via_existing_library_revision(prepared):
    _,db,_,_=prepared;before=library_revision(db);one=create(prepared);after=library_revision(db)
    assert after!=before
    edit(prepared,'rename-combined','rename-001',playlistId=one['id'],name='Changed',revision=one['revision'])
    renamed=library_revision(db);assert renamed!=after
    current=records(db)[0];edit(prepared,'delete-combined','delete-001',playlistId=current['id'],revision=current['revision'])
    assert library_revision(db)!=renamed


def test_backup_preserves_definitions_and_deleted_id_sequence_and_accepts_old_schema(prepared):
    settings,db,work,_=prepared;one=create(prepared)
    edit(prepared,'delete-combined','delete-001',playlistId=one['id'],revision=one['revision'])
    two=create(prepared,name='Keep',op='combine-002')
    edit(prepared,'delete-combined','delete-002',playlistId=two['id'],revision=two['revision'])
    output=work/'snapshot';output.mkdir();file,counts=snapshot(settings,output)
    restored=sqlite3.connect(file);restored.row_factory=sqlite3.Row
    assert counts['playlist_combined']==0
    assert restored.execute("SELECT seq FROM sqlite_sequence WHERE name='playlist_combined'").fetchone()[0]==2
    from aidj.playlist_combined import mutate as direct_mutate
    direct_mutate(restored,{'action':'combine','opId':'after-restore','name':'Restored','sourcePlaylistIds':[1,2]})
    assert records(restored)[0]['id']==BASE+3
    restored.close()
    create(prepared,name='Definition',op='combine-003')
    output=work/'with-definition';output.mkdir();file,counts=snapshot(settings,output)
    restored=sqlite3.connect(file);restored.row_factory=sqlite3.Row
    assert records(restored)[0]['name']=='Definition';restored.close()
    db.execute('DROP TABLE playlist_combined');db.commit()
    output=work/'old-schema';output.mkdir();_,counts=snapshot(settings,output)
    assert counts['playlist_combined']==0
