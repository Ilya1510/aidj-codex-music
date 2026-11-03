import hashlib
import json
from pathlib import Path
import pytest
from aidj.audio import AudioError,duration
from aidj.clips import span,create,save_range
from aidj.config import Settings
from aidj.store import connect,meta_set,claim_job
from aidj.web_catalog import catalog,result_for
from aidj.bot import handle_update
from aidj import worker
from tests.test_audio import audio_file
from tests.test_playlists import env,update


@pytest.mark.parametrize('text,expected',[('0:00–1:30',(0,90)),('от 0:10 до 1:30',(10,90)),('1:00:00 - 1:00:05',(3600,3605)),('0:00.5-0:01.5',(.5,1.5))])
def test_clip_intervals(text,expected):assert span(text,4000)==expected


@pytest.mark.parametrize('text',['-1:00-1:20','1:60-2:10','0:30-0:10','0:00-5:00','0:00-0:00','nonsense'])
def test_invalid_clip_intervals(text):
    with pytest.raises(AudioError):span(text,240)


def test_clip_is_a_new_verified_file_and_original_remains_unchanged(tmp_path):
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    original=audio_file(tmp_path/'song.wav',[(4,True)]);body=original.read_bytes();digest=hashlib.md5(body).hexdigest()
    class Disk:
        def __init__(self):self.files={'/source.wav':body};self.uploads=0
        def mkdir(self,path):pass
        def download(self,remote,target,expected):target.write_bytes(self.files[remote]);assert hashlib.md5(target.read_bytes()).hexdigest()==expected
        def upload(self,local,remote):self.files[remote]=local.read_bytes();self.uploads+=1;return hashlib.md5(self.files[remote]).hexdigest()
        def verify(self,remote,checksum,size):return remote in self.files and len(self.files[remote])==size and hashlib.md5(self.files[remote]).hexdigest()==checksum
    disk=Disk();original_result={'track':{'source':'youtube','id':'abcdefghijk','title':'Song','artist':'Artist','url':'https://www.youtube.com/watch?v=abcdefghijk'},'remote_path':'/source.wav','md5':digest,'duration':4,'bytes':len(body)}
    work=tmp_path/'work';work.mkdir();answer=create(settings,db,original_result,1,2.5,work,disk)
    assert answer['track']['source']=='clip' and answer['duration']==pytest.approx(1.5,abs=.15)
    assert disk.files['/source.wav']==body and disk.uploads==1
    again=create(settings,db,original_result,1,2.5,work,disk);assert again['md5']==answer['md5'] and disk.uploads==1
    values=catalog(db);assert len(values['tracks'])==1 and values['tracks'][0]['duration']==answer['duration']
    asset=db.execute('SELECT * FROM web_assets').fetchone();assert result_for(asset,db)['track']['url']==original_result['track']['url']


def test_bot_range_prompt_queues_clip_without_touching_playlists(env,monkeypatch):
    settings,db,disk,bot,manager,result=env;manager.create('Volleyball');sid=manager.selection(22,11,result)
    handle_update(db,{},bot,update(data='pc:'+sid,n=41),settings)
    handle_update(db,{},bot,update(text='0:00–1:30',n=42),settings)
    row=claim_job(db);payload=json.loads(row['payload']);assert payload['action']=='clip' and payload['end']==90
    assert manager.list()[0]['tracks']==0


def test_playback_range_is_validated_and_returned_in_shared_catalog(env,tmp_path):
    settings,db,disk,bot,manager,result=env;p=manager.create('Volleyball');manager.add(p['id'],result,tmp_path)
    track=catalog(db)['tracks'][0];save_range(db,{'trackId':track['id'],'start':10,'end':30})
    track=catalog(db)['tracks'][0];assert track['rangeStart']==10 and track['rangeEnd']==30
    with pytest.raises(AudioError):save_range(db,{'trackId':track['id'],'start':30,'end':10})


def test_bot_clip_worker_returns_audio_with_its_own_playlist_and_app_buttons(env,tmp_path,monkeypatch):
    settings,db,disk,bot,manager,result=env
    wav=audio_file(tmp_path/'original.wav',[(4,True)]).read_bytes();disk.files[result['remote_path']]=wav
    result.update(md5=hashlib.md5(wav).hexdigest(),bytes=len(wav),duration=4)
    disk.download=lambda remote,target,expected:target.write_bytes(disk.files[remote])
    def upload(local,remote,overwrite=False):disk.files[remote]=local.read_bytes();return hashlib.md5(disk.files[remote]).hexdigest()
    disk.upload=upload;monkeypatch.setattr('aidj.disk.Disk',lambda settings:disk)
    captured={}
    def audio(chat,answer,keyboard):
        captured.update(answer=answer,keyboard=keyboard)
        assert duration(Path(answer['local_path']))==pytest.approx(2,abs=.15)
        return {'message_id':999,'audio':{'file_id':'clip-file'}}
    bot.audio=audio;sid=manager.selection(22,11,result)
    handle_update(db,{},bot,update(data='pc:'+sid,n=71),settings);handle_update(db,{},bot,update(text='0:00–0:02',n=72),settings)
    assert worker.run(claim_job(db)['id'])==0
    assert captured['answer']['track']['source']=='clip' and disk.files[result['remote_path']]==wav
    assert any(b.get('callback_data','').startswith('pd:') for row in captured['keyboard'] for b in row)
