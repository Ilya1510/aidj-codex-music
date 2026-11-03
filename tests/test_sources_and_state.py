import time

import pytest
import requests

from aidj.bot import bind_owner,owner_matches
from aidj.config import Settings
from aidj.sources import SourceError,Track,parse_input,ranked,unambiguous
from aidj.store import connect,enqueue,claim_job
from aidj.telegram import Telegram,TelegramError


@pytest.mark.parametrize('url,expected',[
 ('https://music.yandex.ru/album/4868533/track/38189882?utm_source=test',('yandex','38189882')),
 ('https://music.yandex.ru/track/38189882',('yandex','38189882')),
 ('https://music.youtube.com/watch?v=zh1JQf_5DCI&list=anything',('youtube','zh1JQf_5DCI')),
 ('https://youtu.be/zh1JQf_5DCI?t=20',('youtube','zh1JQf_5DCI')),
 ('LOBODA Парень',('text','LOBODA Парень')),
])
def test_supported_single_track_inputs(url,expected):
    assert parse_input(url)==expected


@pytest.mark.parametrize('url',[
 'http://127.0.0.1:8000/private','https://music.yandex.ru.attacker.com/track/123',
 'https://user:password@music.yandex.ru/track/123',
 'https://music.yandex.ru/playlists/uuid','https://www.youtube.com/playlist?list=whatever',
])
def test_non_song_and_arbitrary_urls_are_rejected(url):
    with pytest.raises(SourceError):parse_input(url)


def test_album_duplicates_do_not_require_manual_selection_but_remixes_do():
    one=Track('yandex','1','Парень','LOBODA','url')
    two=Track('yandex','2','Парень','LOBODA','url')
    assert unambiguous('LOBODA Парень',[one,two])
    remix=Track('yandex','3','Парень','LOBODA','url',version='Remix')
    assert not unambiguous('LOBODA Парень',[remix])
    assert not unambiguous('Парень',[one,two])


def test_owner_pairing_is_one_time_and_unicode_does_not_crash(tmp_path):
    settings=Settings(state=tmp_path/'state');db=connect(settings)
    config={'pair_code':'test-secret'}
    assert not bind_owner(db,config,10,10,'/start чужой')
    assert not bind_owner(db,config,10,10,'/start wrong')
    assert bind_owner(db,config,10,10,'/start test-secret')
    assert owner_matches(db,10,10)
    assert not owner_matches(db,20,10)
    assert not bind_owner(db,config,20,20,'/start test-secret')


def test_duplicate_telegram_update_cannot_enqueue_twice(tmp_path):
    db=connect(Settings(state=tmp_path/'state'))
    assert enqueue(db,123,10,10,5,'song')
    assert enqueue(db,123,10,10,5,'song') is None
    assert claim_job(db)['query']=='song'
    assert claim_job(db) is None


def test_telegram_failure_does_not_expose_token(monkeypatch):
    secret='12345:FAKE_SECRET_NEVER_PRINT'
    bot=Telegram({'token':secret})
    def fail(*args,**kwargs):
        raise requests.ConnectionError('request failed at '+bot.base)
    monkeypatch.setattr(bot.session,'post',fail)
    with pytest.raises(TelegramError) as error:bot.call('getMe')
    assert secret not in str(error.value)
