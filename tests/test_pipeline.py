from pathlib import Path
import shutil

from aidj.config import Settings
from aidj.disk import md5
from aidj.pipeline import Pipeline
from aidj.sources import Track,Unavailable
from aidj.store import connect
from tests.test_audio import audio_file


class Sources:
    def __init__(self):self.yandex_downloads=0;self.youtube_downloads=0
    def yandex_track(self,value):return Track('yandex',value,'Song','Artist','https://music.yandex.ru/track/'+value,available=False)
    def search_youtube(self,query):return [Track('youtube','abcdefghijk','Song','Artist','https://music.youtube.com/watch?v=abcdefghijk')]
    def download_yandex(self,track,work):self.yandex_downloads+=1;raise Unavailable('missing')
    def download_youtube(self,track,work):
        self.youtube_downloads+=1
        return audio_file(work/'original.wav',[(2,False),(3,True),(2,False)])


class Disk:
    def __init__(self,root):self.root=root;self.items={}
    def mkdir(self,path):pass
    def verify(self,path,digest,size):return self.items.get(path)==(digest,size)
    def upload(self,local,remote):
        digest=md5(local);self.items[remote]=(digest,local.stat().st_size)
        shutil.copyfile(local,self.root/'remote.wav')
        return digest
    def download(self,remote,local,digest):shutil.copyfile(self.root/'remote.wav',local)


def test_unavailable_yandex_falls_back_trims_uploads_and_then_uses_cache(tmp_path):
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    sources=Sources();disk=Disk(tmp_path)
    pipeline=Pipeline(settings,db,sources,disk)
    work=tmp_path/'first';work.mkdir()
    result=pipeline.run('https://music.yandex.ru/track/123',{},work)
    assert result['track']['source']=='youtube'
    assert sources.yandex_downloads==0 and sources.youtube_downloads==1
    assert result['trim']['removed_start']>1.5 and result['trim']['removed_end']>1.5
    assert 3.2<result['duration']<3.5
    second=tmp_path/'second';second.mkdir()
    cached=pipeline.run('https://music.yandex.ru/track/123',{},second)
    assert cached['cached'] and sources.youtube_downloads==1
    assert Path(cached['local_path']).is_file()
    def no_local_download(*args):raise AssertionError('App cache hits must not download the file to VM again')
    disk.download=no_local_download
    third=tmp_path/'third';third.mkdir()
    cached=pipeline.run('https://music.yandex.ru/track/123',{},third,need_local=False)
    assert cached['cached'] and 'local_path' not in cached and sources.youtube_downloads==1


def test_fallback_cache_is_used_if_yandex_metadata_claims_available(tmp_path):
    class MissingFile(Sources):
        def yandex_track(self,value):return Track('yandex',value,'Song','Artist','https://music.yandex.ru/track/'+value,available=True)
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    sources=MissingFile();pipeline=Pipeline(settings,db,sources,Disk(tmp_path))
    for label in ['first','second']:
        work=tmp_path/label;work.mkdir()
        result=pipeline.run('https://music.yandex.ru/track/123',{},work)
    assert result['cached'] and sources.youtube_downloads==1
    assert sources.yandex_downloads==2


def test_youtube_fallback_does_not_offer_unrelated_catalog_songs(tmp_path):
    class Irrelevant(Sources):
        def search_youtube(self,query):
            return [Track('youtube','abcdefghijk','Other Song','Other Artist','url')]
    import pytest
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    work=tmp_path/'work';work.mkdir()
    with pytest.raises(Unavailable):
        Pipeline(settings,db,Irrelevant(),Disk(tmp_path)).run('https://music.yandex.ru/track/123',{},work)


def test_oliver_is_not_mistaken_for_live_version(tmp_path):
    class Oliver(Sources):
        def search_youtube(self,query):
            return [Track('youtube','abcdefghijk','Oliver Heldens - Never Look Back','Label','url')]
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    result=Pipeline(settings,db,Oliver(),Disk(tmp_path)).fallback(
        Track('yandex','1','Never Look Back','Oliver Heldens','url',available=False))
    assert result.artist=='Oliver Heldens' and result.title=='Never Look Back'
