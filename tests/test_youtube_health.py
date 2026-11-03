import json
from pathlib import Path
from aidj.youtube_health import probe,record
from aidj.sources import Unavailable
from aidj.store import meta_get
from tests.test_playlists import env


def test_successful_daily_probe_downloads_and_removes_audio_without_messages(env):
    settings,db,disk,bot,manager,result=env
    seen=[]
    class Source:
        def youtube_track(self,video):return video
        def download_youtube(self,track,folder):
            file=folder/'original.m4a';file.write_bytes(b'verified audio');seen.append(file);return file
    assert probe(settings,db,Source())=='ok'
    assert len(seen)==1 and not seen[0].exists()
    assert record(settings,db,'ok',lambda config:bot)==0 and not bot.sent


def test_daily_monitor_alerts_once_per_failure_and_again_only_after_recovery(env):
    settings,db,disk,bot,manager,result=env
    for _ in range(3):assert record(settings,db,'auth',lambda config:bot)==0
    assert len(bot.sent)==1 and 'обновить cookies' in bot.sent[0][1]
    record(settings,db,'ok',lambda config:bot);record(settings,db,'auth',lambda config:bot)
    assert len(bot.sent)==2


def test_network_error_never_claims_cookies_have_expired(env):
    settings,db,disk,bot,manager,result=env
    class Source:
        def youtube_track(self,video):raise Unavailable('Нет соединения с YouTube. Попробуй позже.')
    assert probe(settings,db,Source())=='network'
    record(settings,db,'network',lambda config:bot)
    assert 'не подтверждена' in bot.sent[0][1] and 'сетевая' in bot.sent[0][1]


def test_authentication_failure_stops_additional_video_attempts_and_failed_notice_is_retryable(env):
    settings,db,disk,bot,manager,result=env;calls=[]
    class Source:
        def youtube_track(self,video):calls.append(video);raise Unavailable('Нужно обновить подключение YouTube на сервере.')
    assert probe(settings,db,Source())=='auth' and len(calls)==1
    class Offline:
        def message(self,*args):raise RuntimeError('offline')
    assert record(settings,db,'auth',lambda config:Offline())==75
    assert not json.loads(meta_get(db,'youtube_health'))['notified']
    assert record(settings,db,'auth',lambda config:bot)==0 and len(bot.sent)==1
