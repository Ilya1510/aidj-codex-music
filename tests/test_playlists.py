import hashlib
import json

import pytest

from aidj.bot import handle_update
from aidj.config import Settings, atomic_json
from aidj.disk import DiskError
from aidj.playlists import Playlists, PlaylistError, playlist_name
from aidj.store import connect, meta_set, claim_job, enqueue
from aidj import worker


class MemoryDisk:
    def __init__(self):
        self.files={};self.dirs=set();self.copies=0;self.fail_verify=False
    def metadata(self,path):
        if path in self.dirs:return {'type':'dir'}
        if path not in self.files:return None
        data=self.files[path]
        return {'type':'file','md5':hashlib.md5(data).hexdigest(),'size':len(data)}
    def mkdir(self,path):self.dirs.add(path)
    def verify(self,path,digest,size):
        if self.fail_verify and '/Музыка/Volleyball/' in path:
            self.fail_verify=False
            return False
        item=self.metadata(path)
        return bool(item and item.get('md5')==digest and item.get('size')==size)
    def copy(self,source,target):
        self.copies+=1
        assert target not in self.files
        self.files[target]=self.files[source]
    def upload(self,local,remote,overwrite=False):
        assert overwrite or remote not in self.files
        self.files[remote]=local.read_bytes()


class Bot:
    def __init__(self):self.sent=[];self.edited=[];self.markups=[];self.answered=[]
    def message(self,chat,text,keyboard=None):
        self.sent.append((chat,text,keyboard))
        return {'message_id':100+len(self.sent)}
    def edit(self,*args):self.edited.append(args)
    def markup(self,*args):self.markups.append(args)
    def call(self,method,data):self.answered.append((method,data))


@pytest.fixture
def env(tmp_path,monkeypatch):
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    atomic_json(settings.state/'telegram.json',{'token':'fake'})
    disk=MemoryDisk();bot=Bot()
    monkeypatch.setattr('aidj.playlists.Disk',lambda settings:disk)
    monkeypatch.setattr(worker,'Settings',lambda:settings)
    monkeypatch.setattr(worker,'Telegram',lambda config:bot)
    meta_set(db,'owner_id',11);meta_set(db,'owner_chat_id',22)
    body=b'sample-audio'
    disk.files['/Музыка/aidj_codex_music/song.mp3']=body
    result={'track':{'source':'yandex','id':'123','artist':'Artist','title':'Song','url':'https://music.yandex.ru/track/123'},
            'remote_path':'/Музыка/aidj_codex_music/song.mp3','disk_url':'https://disk.yandex.ru/client/disk/test',
            'md5':hashlib.md5(body).hexdigest(),'bytes':len(body),'duration':123.4}
    return settings,db,disk,bot,Playlists(settings,db,disk),result


def update(text=None,data=None,uid=11,n=1):
    message={'chat':{'id':22,'type':'private'},'from':{'id':uid},'message_id':50}
    if data:
        return {'update_id':n,'callback_query':{'id':str(n),'from':{'id':uid},'message':message,'data':data}}
    message['text']=text
    return {'update_id':n,'message':message}


@pytest.mark.parametrize('name',['','../oops','A/B','A\\B','_archive','.hidden','bad\nname','aidj_codex_music','a'*65])
def test_playlist_name_cannot_escape_music_folder(name):
    with pytest.raises(PlaylistError):playlist_name(name)


def test_create_is_case_insensitive_and_does_not_adopt_unrelated_folder(env):
    settings,db,disk,bot,catalog,result=env
    first=catalog.create(' Volleyball ')
    assert catalog.create('VOLLEYBALL')['id']==first['id']
    disk.dirs.add('/Музыка/Existing')
    with pytest.raises(PlaylistError):catalog.create('Existing')


def test_addition_recovers_after_copy_and_deduplicates_and_keeps_relative_index(env,tmp_path):
    settings,db,disk,bot,catalog,result=env
    playlist=catalog.create('Volleyball')
    disk.fail_verify=True
    with pytest.raises(DiskError):catalog.add(playlist['id'],result,tmp_path)
    assert disk.copies==1
    assert not catalog.contains(playlist['id'],result)
    catalog.add(playlist['id'],result,tmp_path)
    catalog.add(playlist['id'],result,tmp_path)
    assert disk.copies==1 and catalog.contains(playlist['id'],result)
    assert catalog.list()[0]['tracks']==1
    index=disk.files['/Музыка/Volleyball/Volleyball.m3u8'].decode()
    assert '#EXTINF:123,Artist — Song\n001 - Artist — Song.mp3\n' in index
    assert '/Музыка/' not in index


def test_existing_youtube_replacement_matches_original_yandex_track(env):
    settings,db,disk,bot,catalog,result=env
    p=catalog.create('Volleyball')
    db.execute('INSERT INTO playlist_tracks VALUES (?,?,?,?,?,?,?,?,?)',
               (p['id'],'youtube:abcdefghijk',5,'/existing.mp3','md5',10,123,'Artist — Song','complete'))
    db.execute('INSERT INTO playlist_aliases VALUES (?,?,?)',(p['id'],'yandex:123','youtube:abcdefghijk'));db.commit()
    assert catalog.contains(p['id'],result)
    fallback={**result,'origin_key':'yandex:123','track':{**result['track'],'source':'youtube','id':'othervideo1'}}
    assert catalog.contains(p['id'],fallback)
    sid=catalog.selection(22,11,result)
    assert catalog.keyboard(sid)[1][0]['text']=='✓ Volleyball'


def test_each_playlist_click_enqueues_immediately_without_confirmation_or_extra_messages(env):
    settings,db,disk,bot,catalog,result=env
    one=catalog.create('Volleyball');two=catalog.create('Ещё один')
    sid=catalog.selection(22,11,result)
    db.execute('UPDATE playlist_selections SET message_id=50 WHERE id=?',(sid,));db.commit()
    for n,p in enumerate([one,two],1):
        handle_update(db,{},bot,update(data=f'pl:{sid}:{p["id"]}',n=n),settings)
    # Telegram may redeliver an update after the response to a click was lost.
    handle_update(db,{},bot,update(data=f'pl:{sid}:{one["id"]}',n=1),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2
    assert not any(button.get('callback_data','').startswith('pa:') for row in catalog.keyboard(sid) for button in row)
    for _ in range(2):assert worker.run(claim_job(db)['id'])==0
    assert all(r[0]=='completed' for r in db.execute('SELECT status FROM jobs'))
    assert [p['tracks'] for p in catalog.list()]==[1,1]
    assert disk.copies==2
    assert all(catalog.contains(p['id'],result) for p in [one,two])
    assert db.execute('SELECT state,selected FROM playlist_selections').fetchone()[:]==('open','[]')
    assert not bot.sent and not bot.edited


def test_create_from_audio_prompt_selects_the_new_playlist(env):
    settings,db,disk,bot,catalog,result=env
    catalog.create('Volleyball')
    sid=catalog.selection(22,11,result)
    db.execute('UPDATE playlist_selections SET message_id=50 WHERE id=?',(sid,));db.commit()
    handle_update(db,{},bot,update(data=f'pn:{sid}',n=1),settings)
    handle_update(db,{},bot,update(text='Для дороги',n=2),settings)
    job=claim_job(db)
    assert json.loads(job['payload'])['action']=='playlist_create'
    assert worker.run(job['id'])==0
    p=db.execute("SELECT id FROM playlists WHERE name='Для дороги'").fetchone()[0]
    selected=json.loads(db.execute('SELECT selected FROM playlist_selections').fetchone()[0])
    assert selected==[p]
    assert disk.copies==0  # The new playlist name is immediately clickable.


def test_playlist_commands_cancel_and_owner_check(env):
    settings,db,disk,bot,catalog,result=env
    catalog.create('Volleyball')
    handle_update(db,{},bot,update(text='/newplaylist',n=1),settings)
    handle_update(db,{},bot,update(text='/cancel',n=2),settings)
    assert not db.execute("SELECT value FROM meta WHERE key='playlist_name_prompt'").fetchone()[0]
    handle_update(db,{},bot,update(text='/newplaylist Вечер',n=3),settings)
    assert worker.run(claim_job(db)['id'])==0
    handle_update(db,{},bot,update(text='/playlists',n=4),settings)
    assert 'Volleyball' in bot.sent[-1][1] and 'Вечер' in bot.sent[-1][1]
    sid=catalog.selection(22,11,result)
    count=db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    handle_update(db,{},bot,update(data=f'pa:{sid}',uid=999,n=5),settings)
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==count
    assert 'личный' in bot.answered[-1][1]['text']


def test_delivered_audio_has_playlist_buttons_and_persistent_selection(env,monkeypatch):
    settings,db,disk,bot,catalog,result=env
    catalog.create('Volleyball')
    captured={}
    def audio(chat,result,keyboard):
        captured['keyboard']=keyboard
        return {'message_id':777,'audio':{'file_id':'stored'}}
    bot.audio=audio
    class Pipeline:
        def __init__(self,*args):pass
        def run(self,*args):return {**result,'cache_key':'cached-track','local_path':'/temporary/song.mp3'}
    monkeypatch.setattr(worker,'Pipeline',Pipeline)
    job_id=enqueue(db,'audio',22,11,50,'Artist Song')
    claim_job(db)
    assert worker.run(job_id)==0
    selection=db.execute('SELECT * FROM playlist_selections').fetchone()
    assert selection['message_id']==777
    assert 'local_path' not in json.loads(selection['result'])
    assert captured['keyboard'][1][0]['text']=='☐ Volleyball'
    assert captured['keyboard'][-1][0]['callback_data']=='pn:'+selection['id']


def test_partial_multi_playlist_failure_can_be_retried_without_duplicate(env,monkeypatch):
    settings,db,disk,bot,catalog,result=env
    one=catalog.create('Volleyball');two=catalog.create('Второй')
    sid=catalog.selection(22,11,result)
    db.execute('UPDATE playlist_selections SET message_id=50,selected=? WHERE id=?',
               (json.dumps([one['id'],two['id']]),sid));db.commit()
    original=disk.copy
    def copy(source,target):
        if target.startswith('/Музыка/Второй/'):
            raise DiskError('Тестовый сбой копирования')
        original(source,target)
    monkeypatch.setattr(disk,'copy',copy)
    handle_update(db,{},bot,update(data=f'pa:{sid}',n=1),settings)
    assert worker.run(claim_job(db)['id'])==1
    assert catalog.contains(one['id'],result) and not catalog.contains(two['id'],result)
    assert db.execute('SELECT state FROM playlist_selections').fetchone()[0]=='open'
    monkeypatch.setattr(disk,'copy',original)
    handle_update(db,{},bot,update(data=f'pa:{sid}',n=2),settings)
    assert worker.run(claim_job(db)['id'])==0
    assert disk.copies==2
    assert [p['tracks'] for p in catalog.list()]==[1,1]


def test_new_songs_are_prepended_and_retries_preserve_order(env,tmp_path):
    from aidj.web_catalog import catalog as web_catalog
    settings,db,disk,bot,manager,result=env;p=manager.create('Volleyball')
    first=result
    second={**result,'track':{**result['track'],'id':'second','title':'Second'}}
    third={**result,'track':{**result['track'],'id':'third','title':'Third'}}
    for value in (second,third):
        body=value['track']['id'].encode()
        value.update(remote_path='/Музыка/aidj_codex_music/'+value['track']['id']+'.mp3',
                     md5=hashlib.md5(body).hexdigest(),bytes=len(body))
        disk.files[value['remote_path']]=body
    for value in (first,second,third):manager.add(p['id'],value,tmp_path)
    manager.add(p['id'],first,tmp_path)
    rows=db.execute('SELECT label,position FROM playlist_tracks ORDER BY position').fetchall()
    assert [r['label'] for r in rows]==['Artist — Third','Artist — Second','Artist — Song']
    assert [r['position'] for r in rows]==[1,2,3]
    data=web_catalog(db)
    assert data['tracks'][0]['title']=='Third'
    assert data['playlists'][0]['trackIds']==[t['id'] for t in data['tracks']]
    index=disk.files['/Музыка/Volleyball/Volleyball.m3u8'].decode()
    assert index.index('Third')<index.index('Second')<index.index('Artist — Song')


def test_identical_audio_under_another_provider_id_is_one_playlist_entry(env,tmp_path):
    settings,db,disk,bot,catalog,result=env
    p=catalog.create('Предложка');catalog.add(p['id'],result,tmp_path)
    replacement={**result,'origin_key':'yandex:456','track':{**result['track'],'source':'youtube','id':'abcdefghijk','title':'Song (Official Audio)'}}
    assert catalog.contains(p['id'],replacement)
    before=disk.copies
    catalog.add(p['id'],replacement,tmp_path)
    assert disk.copies==before and catalog.list()[0]['tracks']==1
    aliases=dict(db.execute('SELECT track_key,canonical_key FROM playlist_aliases WHERE playlist_id=?',(p['id'],)))
    assert aliases=={'youtube:abcdefghijk':'yandex:123','yandex:456':'yandex:123'}
    assert disk.files['/Музыка/Предложка/Предложка.m3u8'].decode().count('#EXTINF:')==1


def test_same_title_but_different_audio_is_not_silently_merged(env,tmp_path):
    settings,db,disk,bot,catalog,result=env
    p=catalog.create('Предложка');catalog.add(p['id'],result,tmp_path)
    body=b'other recording';remote='/Музыка/aidj_codex_music/other-version.mp3';disk.files[remote]=body
    other={**result,'track':{**result['track'],'id':'456'},'remote_path':remote,'md5':hashlib.md5(body).hexdigest(),'bytes':len(body)}
    assert not catalog.contains(p['id'],other)
    catalog.add(p['id'],other,tmp_path)
    assert catalog.list()[0]['tracks']==2
