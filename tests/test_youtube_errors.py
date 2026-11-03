import pytest
from aidj.sources import Sources,youtube_failure,Unavailable
from aidj.config import Settings


@pytest.mark.parametrize('provider,expected',[
    ('Sign in to confirm you’re not a bot.','подтвердить вход'),
    ('This video is private video','запись закрыта'),
    ('Sign in to confirm your age','подтверждение возраста'),
    ('Video not available in your country','регионе'),
    ('Video unavailable','запись недоступна'),
    ('Connection timed out','соединения'),
    ('Sign in to access this recording','требует вход'),
    ('Extractor changed unexpectedly','Причина не подтверждена'),
])
def test_youtube_errors_report_only_confirmed_reason_and_hide_diagnostics(provider,expected):
    error=youtube_failure(RuntimeError(provider+' https://example.invalid/PRIVATE_URL token=PRIVATE_COOKIE'))
    assert expected in str(error)
    assert 'PRIVATE' not in str(error) and 'истекла' not in str(error)


def test_metadata_failure_preserves_youtube_auth_reason(monkeypatch):
    class Downloader:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):raise RuntimeError('Sign in to confirm you’re not a bot. PRIVATE_COOKIE')
    monkeypatch.setattr('aidj.sources.yt_dlp.YoutubeDL',Downloader)
    source=object.__new__(Sources);source.settings=Settings()
    with pytest.raises(Unavailable,match='подтвердить вход') as error:source.youtube_track('KBkqRCn4INY')
    assert 'PRIVATE_COOKIE' not in str(error.value)
