from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import os
import shutil
import unicodedata
from urllib.parse import parse_qs, quote, urlparse

import mutagen
from mutagen.id3 import ID3, TALB, TIT2, TPE1
from mutagen.mp4 import MP4
import requests
from yandex_music import Client
import yt_dlp


class SourceError(RuntimeError):
    pass


class Unavailable(SourceError):
    pass


def youtube_failure(error,action='получить запись'):
    """Classify provider errors without exposing URLs, cookies or raw exceptions."""
    message=str(error).casefold()
    if 'not a bot' in message or 'confirm you’re not' in message or 'confirm you\'re not' in message:
        return Unavailable('YouTube требует подтвердить вход для загрузчика. Нужно обновить подключение YouTube на сервере.')
    if 'private video' in message or 'members-only' in message or 'join this channel' in message:
        return Unavailable('Эта запись закрыта. Подключённый аккаунт YouTube должен иметь к ней доступ.')
    if 'age-restricted' in message or 'confirm your age' in message:
        return Unavailable('YouTube требует подтверждение возраста для этой записи. Нужен аккаунт с доступом к ней.')
    if 'not available in your country' in message or 'geo restricted' in message or 'geo-restricted' in message:
        return Unavailable('Эта запись YouTube недоступна в регионе сервера.')
    if 'video unavailable' in message or 'video has been removed' in message or 'video is unavailable' in message:
        return Unavailable('YouTube сообщает, что запись недоступна. Проверь, открывается ли эта ссылка в браузере.')
    if any(value in message for value in ('timed out','timeout','connection','network is unreachable','unable to download webpage')):
        return Unavailable('Нет соединения с YouTube. Попробуй позже.')
    if 'sign in' in message or 'login required' in message:
        return Unavailable('Для этой записи YouTube требует вход. Нужно обновить подключение YouTube на сервере.')
    return Unavailable('Не удалось '+action+' из YouTube. Причина не подтверждена; попробуй позже или другую запись.')


@dataclass
class Track:
    source: str
    id: str
    title: str
    artist: str
    url: str
    duration: float | None = None
    available: bool = True
    version: str = ''
    channel: str = ''

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**value)

    @property
    def label(self):
        return self.artist + ' — ' + self.title + (f' ({self.version})' if self.version else '')


def normalize(text):
    text = unicodedata.normalize('NFKD', text.casefold().replace('ё','е'))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-zа-я0-9]+', text))


def parse_input(text):
    text = text.strip()
    if not text or len(text) > 512:
        raise SourceError('Пришли название песни с исполнителем или ссылку на один трек.')
    if not re.search(r'https?://', text):
        return 'text', text
    matches = re.findall(r'https?://[^\s<>]+', text)
    if len(matches) != 1:
        raise SourceError('Пришли одну ссылку на песню.')
    try:
        url = urlparse(matches[0].rstrip(').,;'))
        port = url.port
    except ValueError:
        raise SourceError('Некорректная ссылка. Пришли обычную ссылку на песню.') from None
    if url.username or url.password or port:
        raise SourceError('Необычная ссылка: пришли обычную ссылку на трек.')
    host = (url.hostname or '').casefold()
    if host in {'music.yandex.ru','music.yandex.com','music.yandex.by','music.yandex.kz','music.yandex.uz'}:
        match = re.fullmatch(r'/(?:album/\d+/)?tracks?/(\d+)/?',url.path)
        if not match:
            raise SourceError('Сейчас принимаю ссылки на отдельные песни Яндекс Музыки.')
        return 'yandex', match.group(1)
    if host in {'youtube.com','www.youtube.com','music.youtube.com','m.youtube.com','youtu.be'}:
        video = url.path.strip('/') if host == 'youtu.be' else (parse_qs(url.query).get('v') or [''])[0]
        if not re.fullmatch(r'[a-zA-Z0-9_-]{11}', video):
            raise SourceError('Нужна ссылка на одно видео или песню YouTube Music.')
        return 'youtube', video
    raise SourceError('Поддерживаются текст, Яндекс Музыка и YouTube Music.')


def from_yandex(track):
    album = track.albums[0].id if track.albums else None
    url = f'https://music.yandex.ru/album/{album}/track/{track.id}' if album else f'https://music.yandex.ru/track/{track.id}'
    return Track('yandex',str(track.id),track.title or '',', '.join(a.name for a in track.artists),url,
                 track.duration_ms/1000 if track.duration_ms else None, bool(track.available),track.version or '')


class Quiet:
    def debug(self, message):
        pass
    info = debug
    warning = debug
    error = debug


class Sources:
    def __init__(self, settings):
        self.settings = settings
        self.client = Client(settings.secret('credentials.json')['access_token'])

    def youtube_options(self):
        local=Path.home()/'.local/bin/node'
        node=os.environ.get('AIDJ_NODE') or (str(local) if local.is_file() else shutil.which('node') or 'node')
        return {'cookiefile':str(self.settings.credentials/'youtube-cookies.txt'),
                'quiet':True,'no_warnings':True,'logger':Quiet(),'noplaylist':True,
                'socket_timeout':20,'retries':1,'extractor_retries':1,
                'js_runtimes':{'node':{'path':node}}}

    def yandex_track(self, track_id):
        tracks = self.client.tracks([track_id],timeout=25)
        if not tracks:
            raise Unavailable('Яндекс Музыка не вернула этот трек.')
        result = from_yandex(tracks[0])
        if not result.title or not result.artist:
            raise Unavailable('Яндекс не раскрывает название этой записи; пришли название текстом.')
        return result

    def search_yandex(self, text):
        result = self.client.search(text,type_='track',nocorrect=True,timeout=25)
        return [from_yandex(t) for t in result.tracks.results[:8]] if result and result.tracks else []

    def search_youtube(self, query):
        options = {**self.youtube_options(),'extract_flat':'in_playlist','skip_download':True,'playlistend':6}
        result = None
        try:
            with yt_dlp.YoutubeDL(options) as y:
                result = y.extract_info('https://music.youtube.com/search?q='+quote(query)+'#songs',download=False)
        except Exception:
            pass
        entries = list(result.get('entries',[]))[:6] if result else []
        # Music search can silently substitute unrelated catalog songs when a
        # recording is absent. Include ordinary music videos and rank literally.
        try:
            with yt_dlp.YoutubeDL(options) as y:
                result = y.extract_info('ytsearch6:'+query,download=False)
            entries.extend(list(result.get('entries',[]))[:6])
        except Exception as exc:
            if not entries:
                raise youtube_failure(exc,'выполнить поиск') from None
        tracks = []
        seen=set()
        for item in entries:
            if not item:
                continue
            video_id = item.get('id')
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}',str(video_id)):
                continue
            if video_id in seen:continue
            seen.add(video_id)
            if item.get('duration') and item['duration'] > self.settings.max_duration:
                continue
            artists = item.get('artists') or []
            if isinstance(artists,str):artists=[artists]
            if artists and isinstance(artists[0],dict):
                artists = [a.get('name','') for a in artists]
            channel = item.get('channel') or item.get('uploader') or ''
            artist = ', '.join(artists) or channel.removesuffix(' - Topic')
            tracks.append(Track('youtube',video_id,item.get('track') or item.get('title') or query,artist,
                                'https://music.youtube.com/watch?v='+video_id,item.get('duration'),True,'',channel))
        return tracks

    def search(self, text, youtube_only=False):
        if not youtube_only:
            tracks = self.search_yandex(text)
            if tracks and max(coverage(text,t) for t in tracks)>=.85:
                return tracks
        return self.search_youtube(text)

    def youtube_track(self, video_id):
        try:
            with yt_dlp.YoutubeDL({**self.youtube_options(),'skip_download':True}) as y:
                item = y.extract_info('https://music.youtube.com/watch?v='+video_id,download=False)
        except Exception as exc:
            raise youtube_failure(exc) from None
        if (item.get('duration') or 0) > self.settings.max_duration:
            raise SourceError('Запись длиннее 30 минут.')
        return Track('youtube',video_id,item.get('track') or item['title'],
                     ', '.join(item.get('artists') or []) or item.get('channel') or '',
                     'https://music.youtube.com/watch?v='+video_id,item.get('duration'),True,'',item.get('channel') or '')

    def download_yandex(self, track, destination):
        try:
            real = self.client.tracks([track.id],timeout=25)[0]
            variants = sorted((v for v in real.get_download_info(timeout=25) if v.codec=='mp3' and not v.preview),
                              key=lambda v:v.bitrate_in_kbps, reverse=True)
        except Exception:
            raise Unavailable('Яндекс Музыка не отдала полный файл.') from None
        for variant in variants:
            target = destination/'original.mp3'
            try:
                size = 0
                with requests.get(variant.get_direct_link(timeout=20),stream=True,timeout=(15,45)) as r:
                    if r.status_code != 200:
                        continue
                    with target.open('wb') as f:
                        for chunk in r.iter_content(131072):
                            size += len(chunk)
                            if size > self.settings.max_bytes:
                                raise SourceError('Файл превышает лимит размера.')
                            f.write(chunk)
                audio = mutagen.File(target)
                if audio is None or not audio.info.length:
                    continue
                if track.duration and audio.info.length < track.duration * .9:
                    continue
                tags = ID3(target) if audio.tags else ID3()
                tags.add(TIT2(encoding=3,text=track.title))
                tags.add(TPE1(encoding=3,text=track.artist))
                tags.add(TALB(encoding=3,text='AI DJ'))
                tags.save(target,v2_version=3)
                return target
            except SourceError:
                raise
            except Exception:
                continue
        raise Unavailable('Полного файла Яндекс Музыки нет.')

    def download_youtube(self, track, destination):
        target = destination/'original.m4a'
        def limit(progress):
            if progress.get('downloaded_bytes',0) > self.settings.max_bytes:
                raise SourceError('Файл превышает лимит размера.')
        options = {**self.youtube_options(),'format':'bestaudio[ext=m4a]',
                   'outtmpl':str(destination/'original.%(ext)s'),'fixup':'never',
                   'max_filesize':self.settings.max_bytes,'fragment_retries':1,
                   'concurrent_fragment_downloads':1,'progress_hooks':[limit]}
        try:
            with yt_dlp.YoutubeDL(options) as y:
                info = y.extract_info(track.url,download=True)
            audio = MP4(target)
            if info.get('id') != track.id or not audio.info.length:
                raise Unavailable('YouTube вернул неожиданный файл.')
            if info.get('duration') and abs(audio.info.length-info['duration']) > 3:
                raise Unavailable('Файл YouTube не прошёл проверку длительности.')
            audio['\xa9nam']=[track.title]
            audio['\xa9ART']=[track.artist]
            audio['\xa9cmt']=[track.url]
            audio.save()
            return target
        except SourceError:
            raise
        except Exception as exc:
            raise youtube_failure(exc,'скачать аудио') from None


def ranked(query, tracks):
    query_tokens = set(normalize(query).split())
    def score(t):
        label = normalize(t.label)
        tokens = set(label.split())
        return (len(query_tokens & tokens)/max(1,len(query_tokens)),
                len(query_tokens & tokens)/max(1,len(tokens)),int(t.available))
    return sorted(tracks,key=score,reverse=True)


def coverage(query,track):
    query_tokens=set(normalize(query).split())
    return len(query_tokens & set(normalize(track.label).split()))/max(1,len(query_tokens))


def unambiguous(query, tracks):
    if not tracks:
        return False
    q = normalize(query)
    first = tracks[0]
    title, artist = normalize(first.title), normalize(first.artist)
    if not title or not artist or title not in q or artist not in q:
        return False
    if first.version and normalize(first.version) not in q:
        return False
    # Multiple album releases of the same recording do not require another click.
    for other in tracks[1:]:
        if normalize(other.label)==normalize(first.label):
            continue
        if other.version and normalize(other.version) not in q:
            continue
        if normalize(other.title) in q and normalize(other.artist) in q:
            return False
    return True
