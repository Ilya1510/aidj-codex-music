import json
from dataclasses import replace
from pathlib import Path
import re
import shutil
import time

from .audio import duration, trim
from .disk import Disk, web_url
from .sources import Sources, SourceError, Track, Unavailable, parse_input, ranked, unambiguous,normalize,coverage

ALGORITHM='edges-v1:-55dB:0.6:0.15:0.8'


class SelectionNeeded(Exception):
    def __init__(self, query, tracks, youtube=False):
        self.query,self.tracks,self.youtube=query,tracks,youtube


def safe_filename(value):
    value=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',value).strip(' .')
    return value.encode()[:200].decode(errors='ignore').rstrip(' .') or 'Песня'


class Pipeline:
    def __init__(self,settings,db,sources=None,disk=None,namespace=''):
        self.settings,self.db=settings,db
        self.namespace=namespace
        self.sources=sources or Sources(settings)
        self.disk=disk or Disk(settings)

    def select(self,text,payload):
        if payload.get('track'):
            return Track.from_dict(payload['track'])
        kind,value=parse_input(text)
        if kind=='yandex':
            return self.sources.yandex_track(value)
        if kind=='youtube':
            return self.sources.youtube_track(value)
        candidates=ranked(value,self.sources.search(value,youtube_only=payload.get('youtube_only',False)))
        if not candidates:
            raise Unavailable('Не нашёл подходящую песню. Попробуй указать исполнителя или прислать ссылку.')
        if not unambiguous(value,candidates):
            raise SelectionNeeded(value,candidates[:5],payload.get('youtube_only',False))
        return candidates[0]

    def fallback(self,track):
        query=' '.join(p for p in [track.artist,track.title,track.version] if p)
        candidates=ranked(query,self.sources.search_youtube(query))
        if not candidates:
            raise Unavailable('В Яндекс Музыке полного файла нет, в YouTube Music подходящую запись не нашёл.')
        anchored=[]
        related=[]
        for candidate in candidates:
            label=normalize(candidate.label)
            requested=normalize(query)
            wanted_title,wanted_artist=normalize(track.title),normalize(track.artist)
            found_title,found_artist=normalize(candidate.title),normalize(candidate.artist)
            artist_prefix=bool(wanted_artist and found_title.startswith(wanted_artist+' '))
            remainder=found_title[len(wanted_artist)+1:] if artist_prefix else found_title
            title_match=remainder==wanted_title or remainder.startswith(wanted_title+' ')
            identity_match=title_match and (found_artist==wanted_artist or artist_prefix)
            different_version=any((' '+marker+' ') in (' '+label+' ') and
                                  (' '+marker+' ') not in (' '+requested+' ') for marker in
                                  ['remix','cover','кавер','live','концерт','караоке','slowed','sped up','acoustic'])
            if identity_match and not different_version:
                anchored.append(replace(candidate,title=track.title,artist=track.artist,version=track.version))
            if (found_artist==wanted_artist or found_title==wanted_title or artist_prefix or
                (len(wanted_title.split())>=2 and (' '+wanted_title+' ') in (' '+found_title+' '))):
                related.append(candidate)
        if anchored:
            return anchored[0]
        candidates=related
        if not candidates:
            raise Unavailable('Нашёл только другие песни. Пришли ссылку на подходящую версию YouTube.')
        if not unambiguous(query,candidates):
            raise SelectionNeeded(query,candidates[:5],True)
        return candidates[0]

    def cached(self,track,work,need_local=True):
        key=self.namespace+ALGORITHM+':'+track.source+':'+track.id
        cache=self.db.execute('SELECT * FROM cache WHERE key=?',(key,)).fetchone()
        if not cache:
            return None
        result=json.loads(cache['result'])
        if not self.disk.verify(result['remote_path'],result['md5'],result['bytes']):
            return None
        result.update(cached=True,telegram_file_id=cache['telegram_file_id'],cache_key=key)
        if need_local and not cache['telegram_file_id']:
            local=work/('cached'+Path(result['remote_path']).suffix)
            self.disk.download(result['remote_path'],local,result['md5'])
            result['local_path']=str(local)
        return result

    def run(self,text,payload,work,notify=lambda text:None,need_local=True):
        if shutil.disk_usage(self.settings.state).free < 2*1024**3:
            raise SourceError('На VM осталось мало свободного места. Загрузку остановил.')
        notify('Ищу песню…')
        track=self.select(text,payload)
        origin_key=track.source+':'+track.id
        if track.duration and track.duration>self.settings.max_duration:
            raise SourceError('Пока поддерживаю записи до 30 минут.')
        if track.source=='yandex' and not track.available:
            notify('В Яндекс Музыке запись недоступна. Ищу в YouTube Music…')
            track=self.fallback(track)
        key=self.namespace+ALGORITHM+':'+track.source+':'+track.id
        cached=self.cached(track,work,need_local)
        if cached:
            cached['origin_key']=origin_key
            return cached
        notify('Скачиваю: '+track.label)
        if track.source=='yandex':
            try:
                original=self.sources.download_yandex(track,work)
            except Unavailable:
                notify('Полного файла в Яндекс Музыке нет. Ищу в YouTube Music…')
                track=self.fallback(track)
                key=self.namespace+ALGORITHM+':'+track.source+':'+track.id
                cached=self.cached(track,work,need_local)
                if cached:
                    cached['origin_key']=origin_key
                    return cached
                original=self.sources.download_youtube(track,work)
        else:
            original=self.sources.download_youtube(track,work)
        final,edges=trim(original,work/('clean'+original.suffix))
        name=safe_filename(track.label)+f' [{track.source}-{track.id}]'+original.suffix
        named=work/name
        final.replace(named)
        self.disk.mkdir(self.settings.disk_folder)
        remote=self.settings.disk_folder+'/'+name
        notify('Сохраняю на Диск…')
        digest=self.disk.upload(named,remote)
        result={'track':track.to_dict(),'remote_path':remote,'disk_url':web_url(remote),
                'origin_key':origin_key,
                'md5':digest,'bytes':named.stat().st_size,'duration':duration(named),
                'trim':edges.to_dict(),'cached':False,'cache_key':key,'telegram_file_id':None}
        self.db.execute('INSERT INTO cache(key,result,created) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET result=excluded.result,telegram_file_id=NULL,created=excluded.created',
                        (key,json.dumps(result,ensure_ascii=False),time.time()))
        self.db.commit()
        result['local_path']=str(named)
        return result
