"""Create a separate, reproducible clip from a file already held on Disk."""
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from mutagen.mp4 import MP4
from .audio import AudioError,duration,ffmpeg
from .pipeline import safe_filename
from .disk import web_url


def timestamp(text):
    fields=text.strip().replace(',','.').split(':')
    if not 1<=len(fields)<=3 or any(not re.fullmatch(r'\d+(?:\.\d+)?',p) for p in fields):raise AudioError('Время: минуты:секунды, например 1:30.')
    if len(fields)>1 and any(float(p)>=60 for p in fields[1:]):raise AudioError('Секунды и минуты внутри времени должны быть меньше 60.')
    return sum(float(value)*60**i for i,value in enumerate(reversed(fields)))


def bounds(start,end,total):
    if type(start) not in (int,float) or type(end) not in (int,float) or not all(math.isfinite(v) for v in (start,end,total)):
        raise AudioError('Укажи начало и конец фрагмента.')
    if start<0 or end-start<.5 or end>total+.05:raise AudioError('Фрагмент должен быть внутри песни и длиться не меньше половины секунды.')
    return round(start,3),round(min(end,total),3)


def span(text,total):
    match=re.fullmatch(r'\s*(?:от\s*)?([\d:.,]+)\s*(?:[-–—]|до)\s*([\d:.,]+)\s*',text,re.I)
    if not match:raise AudioError('Напиши интервал: 0:00–1:30. /cancel — отменить.')
    return bounds(timestamp(match[1]),timestamp(match[2]),total)


def clock(value):
    minutes,seconds=divmod(round(value,1),60)
    return f'{int(minutes)}:{seconds:04.1f}'.removesuffix('.0')


def create(settings,db,original,start,end,work,disk):
    start,end=bounds(start,end,original['duration']);work=Path(work)
    identity=hashlib.sha256(f"{original['md5']}:{start:.3f}:{end:.3f}:aac-v1".encode()).hexdigest()[:24]
    key='clip:'+identity
    previous=db.execute('SELECT result FROM audio_clips WHERE track_key=?',(key,)).fetchone()
    if previous:
        result=json.loads(previous[0]);asset=db.execute('SELECT remote_path FROM web_assets WHERE track_key=?',(key,)).fetchone()
        if asset:result.update(remote_path=asset['remote_path'],disk_url=web_url(asset['remote_path']))
        if disk.verify(result['remote_path'],result['md5'],result['bytes']):
            target=work/'fragment.m4a';disk.download(result['remote_path'],target,result['md5']);result['local_path']=str(target)
            if asset is None:
                from .web_catalog import register_asset
                register_asset(db,result);db.commit()
            return result
    if shutil.disk_usage(settings.state).free<2*1024**3:raise AudioError('На сервере недостаточно свободного места для подготовки фрагмента.')
    source=work/('source'+Path(original['remote_path']).suffix);disk.download(original['remote_path'],source,original['md5'])
    target=work/'fragment.m4a'
    result=subprocess.run([ffmpeg(),'-nostdin','-hide_banner','-loglevel','error','-threads','1','-ss',str(start),'-i',str(source),'-t',str(end-start),'-map','0:a:0','-vn','-c:a','aac','-b:a','192k','-threads','1','-movflags','+faststart','-y',str(target)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=180)
    if result.returncode:raise AudioError('Не удалось подготовить фрагмент.')
    actual=duration(target)
    if abs(actual-(end-start))>.2:raise AudioError('Фрагмент не прошёл проверку длительности.')
    track={**original['track'],'source':'clip','id':identity,'version':'','title':original['track']['title']+' ['+clock(start)+'–'+clock(end)+']'}
    tags=MP4(target);tags['\xa9nam']=[track['title']];tags['\xa9ART']=[track['artist']];tags['\xa9cmt']=[track['url']];tags.save()
    remote=settings.disk_folder+'/'+safe_filename(track['artist']+' — '+track['title'])+' [clip-'+identity+'].m4a'
    disk.mkdir(settings.disk_folder);checksum=disk.upload(target,remote)
    answer={'track':track,'remote_path':remote,'disk_url':web_url(remote),'md5':checksum,'bytes':target.stat().st_size,'duration':duration(target),'clip':{'start':start,'end':end,'original_md5':original['md5']},'telegram_file_id':None}
    db.execute('INSERT INTO audio_clips VALUES (?,?,?) ON CONFLICT(track_key) DO UPDATE SET result=excluded.result',(key,json.dumps(answer,ensure_ascii=False),time.time()))
    from .web_catalog import register_asset
    register_asset(db,answer)
    db.commit()
    answer['local_path']=str(target);return answer


def save_range(db,body):
    row=db.execute('SELECT duration FROM web_assets WHERE id=?',(body.get('trackId'),)).fetchone()
    if not row:raise AudioError('Песня не найдена.')
    start,end=bounds(body.get('start'),body.get('end'),row[0])
    db.execute('INSERT INTO playback_ranges VALUES (?,?,?,?) ON CONFLICT(track_id) DO UPDATE SET start=excluded.start,end=excluded.end,updated=excluded.updated',(body['trackId'],start,end,time.time()));db.commit()
    return {'ok':True,'start':start,'end':end}
