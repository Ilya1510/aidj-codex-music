"""Offline DJ listening experiment. Originals, playlists and app cache stay intact.

Run prepare with the normal environment; analysis/render use requirements-dj.txt
in a separate environment. All audio work is sequential and bounded per song.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

from .config import Settings, atomic_json


def read(path):return json.loads(Path(path).read_text())


def checksum(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'md5').hexdigest()


def run(ffmpeg,args,timeout=240):
    process=subprocess.run([ffmpeg,'-nostdin','-hide_banner','-threads','1','-filter_threads','1','-filter_complex_threads','1',*map(str,args)],
                           stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=timeout)
    if process.returncode:raise RuntimeError('FFmpeg failed: '+process.stderr[-1200:])
    return process.stderr


def prepare(work):
    from .disk import Disk
    settings=Settings();work.mkdir(parents=True,exist_ok=True);(work/'sources').mkdir(exist_ok=True)
    candidates=read(work/'candidates.json')
    if not isinstance(candidates,list) or not 2<=len(candidates)<=16:raise RuntimeError('Select between 2 and 16 candidates')
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    pid=db.execute("SELECT id FROM playlists WHERE name_key='volleyball'").fetchone()[0]
    rows=db.execute("SELECT * FROM playlist_tracks WHERE playlist_id=? AND state='complete' ORDER BY position",(pid,)).fetchall()
    owner=db.execute("SELECT value FROM meta WHERE key='owner_id'").fetchone()[0]
    songs=[];seen=set()
    for artist,title in candidates:
        row=next((r for r in rows if artist.casefold() in r['label'].casefold() and title.casefold() in r['label'].casefold()),None)
        if row is None or row['md5'] in seen:continue
        vote=db.execute('''SELECT v.value FROM review_votes v JOIN review_items i ON i.id=v.item_id
            WHERE v.user_id=? AND v.playlist_id=? AND json_extract(i.result,'$.md5')=? ORDER BY v.updated DESC LIMIT 1''',(owner,pid,row['md5'])).fetchone()
        if vote and vote[0]==-1:continue
        aid=hashlib.sha256(row['track_key'].encode()).hexdigest()[:24]
        part=db.execute('SELECT start,end FROM playback_ranges WHERE track_id=?',(aid,)).fetchone()
        suffix=Path(row['remote_path']).suffix
        if suffix not in ('.mp3','.m4a'):continue
        seen.add(row['md5']);songs.append({'key':row['track_key'],'label':row['label'],'md5':row['md5'],
            'bytes':row['bytes'],'duration':row['duration'],'remote':row['remote_path'],
            'file':str(Path('sources')/(row['md5']+suffix)),
            'allowedStart':part['start'] if part else 0,'allowedEnd':part['end'] if part else row['duration']})
    db.close()
    size=sum(s['bytes'] for s in songs)
    if size>256*1024**2 or shutil.disk_usage(work).free<size+1024**3:raise RuntimeError('Insufficient scratch space')
    atomic_json(work/'sources.json',songs);disk=Disk(settings)
    print('PREPARE',len(songs),'tracks',size,'bytes',flush=True)
    for i,song in enumerate(songs,1):
        local=work/song['file']
        if not local.exists() or local.stat().st_size!=song['bytes'] or checksum(local)!=song['md5']:
            disk.download(song['remote'],local,expected_md5=song['md5'])
        print('SOURCE',i,song['label'],flush=True)


def analyse_song(work,song,ffmpeg):
    import numpy as np
    import librosa
    # Decode only this song, never the full playlist, into a bounded scratch file.
    pcm=work/'analysis.f32'
    run(ffmpeg,['-loglevel','error','-i',work/song['file'],'-vn','-ac','1','-ar','22050','-f','f32le','-y',pcm])
    y=np.fromfile(pcm,dtype=np.float32);pcm.unlink();sr=22050;hop=512
    if len(y)>sr*600:raise RuntimeError('Pilot songs must be shorter than ten minutes')
    onset=librosa.onset.onset_strength(y=y,sr=sr,hop_length=hop)
    _,frames=librosa.beat.beat_track(onset_envelope=onset,sr=sr,hop_length=hop,trim=False)
    if len(frames)<64:raise RuntimeError('Not enough stable beats')
    beats=librosa.frames_to_time(frames,sr=sr,hop_length=hop)
    # A median of one-hop beat intervals quantises e.g. 138 BPM to 136 BPM.
    # Average over sixteen beats before estimating a steady track tempo.
    bpm=60*16/float(np.median(beats[16:]-beats[:-16]))
    # Preserve the detected grid; report any possible half/double tempo openly.
    chroma=librosa.feature.chroma_stft(y=y,sr=sr,hop_length=hop,tuning=0)
    rms=librosa.feature.rms(y=y,hop_length=hop)[0]
    beat_chroma=librosa.util.sync(chroma,frames,aggregate=np.mean,pad=False)
    beat_energy=librosa.util.sync(rms[None,:],frames,aggregate=np.mean,pad=False)[0]
    # This pilot estimates bar phase from accents; it is not a learned downbeat
    # detector. Keep the underlying beat/section map for listening corrections.
    accents=onset[np.minimum(frames,len(onset)-1)]
    phase=max(range(4),key=lambda p:float(np.mean(accents[p::4])))
    bar_ids=np.arange(phase,len(beats)-4,4);bars=beats[bar_ids]
    features=[];energy=[]
    for b in bar_ids:
        features.append(np.mean(beat_chroma[:,b:b+4],axis=1));energy.append(float(np.mean(beat_energy[b:b+4])))
    f=np.array(features);energy=np.asarray(energy);f/=np.maximum(np.linalg.norm(f,axis=1,keepdims=True),1e-6)
    novelty=np.zeros(len(bars));repeat=np.zeros(len(bars))
    for i in range(4,len(bars)-4):
        left=np.mean(f[i-4:i],axis=0);right=np.mean(f[i:i+4],axis=0)
        novelty[i]=1-float(left@right)/(max(float(np.linalg.norm(left)*np.linalg.norm(right)),1e-6))
    # Repeated 8-bar passages are candidates for a recognisable refrain, not
    # semantic chorus labels. Compare only non-overlapping distant passages.
    windows=np.array([f[i:i+8].reshape(-1) for i in range(len(f)-8)])
    windows/=np.maximum(np.linalg.norm(windows,axis=1,keepdims=True),1e-6)
    similarity=windows@windows.T
    for i in range(len(windows)):
        mask=np.abs(np.arange(len(windows))-i)>=12
        if np.any(mask):repeat[i]=float(np.max(similarity[i,mask]))
    e=energy/max(float(np.quantile(energy,.9)),1e-6)
    n=novelty/max(float(np.quantile(novelty,.9)),1e-6)
    choices=[];lo=song['allowedStart'];hi=min(song['allowedEnd'],len(y)/sr)
    for a,start in enumerate(bars):
        if not lo<=start<=min(hi-80,lo+70):continue
        for b in range(a+16,len(bars)):
            end=float(bars[b]);length=end-float(start)
            if end>hi or length<88 or length>132:continue
            # Keep whole 8-bar phrases, with a soft target rather than a timer cut.
            if (b-a)%8:continue
            score=(float(np.mean(e[a:b]))*.7+float(np.max(repeat[a:b]))*.3+
                   min(float(n[a]+n[b]),4)*.12-abs(length-108)*.012-float(start)*.001)
            choices.append((score,a,b))
    if not choices:raise RuntimeError('No segment within the user playback range')
    choices.sort(reverse=True);options=[]
    for score,a,b in choices:
        if any(abs(float(bars[a])-o['start'])<6 and abs(float(bars[b])-o['end'])<6 for o in options):continue
        options.append({'start':round(float(bars[a]),5),'end':round(float(bars[b]),5),'score':round(score,4),'bars':b-a})
        if len(options)==5:break
    return {'bpm':round(bpm,3),'beats':[round(float(t),5) for t in beats],
            'bars':[round(float(t),5) for t in bars],'barPhaseMethod':'onset-accent heuristic',
            'segments':options,'duration':len(y)/sr}


def analyse(work,ffmpeg):
    songs=read(work/'sources.json');out=work/'analysis';out.mkdir(exist_ok=True)
    for i,song in enumerate(songs,1):
        dest=out/(song['md5']+'.json')
        if not dest.exists():atomic_json(dest,analyse_song(work,song,ffmpeg))
        result=read(dest)
        print('ANALYSED',i,song['label'],'BPM',result['bpm'],'CLIP',result['segments'][0],flush=True)


def transition(left,right):
    if abs(left['targetBpm']-right['targetBpm'])<.1:
        beats=right.get('transitionBeats',8)
        if not isinstance(beats,int) or not 1<=beats<=16:raise RuntimeError('Invalid overlap length')
        seconds=beats*60/left['targetBpm']
        return {'kind':'beat-overlap','seconds':seconds,'beats':beats}
    return {'kind':'short-change','seconds':.8,'beats':None}


def render(work,ffmpeg):
    import mutagen
    plan=read(work/'plan.json');songs={s['md5']:s for s in read(work/'sources.json')}
    clips=work/'clips';clips.mkdir(exist_ok=True);items=plan['tracks'];inputs=[];timeline=[];clock=0.
    if not 2<=len(items)<=12 or shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('Preview exceeds resource budget')
    for i,item in enumerate(items):
        song=songs[item['md5']];analysis=read(work/'analysis'/(item['md5']+'.json'))
        option=dict(analysis['segments'][item.get('segment',0)])
        source_bpm=option['bars']*4*60/(option['end']-option['start'])
        corrected='sourceStart' in item or 'sourceEnd' in item
        if corrected:
            # Listening corrections override the beat-grid suggestion, while
            # retaining the original song tempo and the user's allowed range.
            option.update(start=item.get('sourceStart',option['start']),end=item.get('sourceEnd',option['end']))
            source_bpm=item.get('sourceBpm',analysis['bpm'])
        rate=item['targetBpm']/source_bpm
        if not .97<=rate<=1.03:raise RuntimeError('Tempo adjustment exceeds three percent')
        if not song['allowedStart']<=option['start']<option['end']<=song['allowedEnd']+.05:raise RuntimeError('Outside the user playback range')
        args=['-ss',f"{option['start']:.5f}",'-t',f"{option['end']-option['start']:.5f}",'-i',work/song['file'],'-vn']
        exact=(option['end']-option['start'])/rate if corrected else option['bars']*4*60/item['targetBpm']
        tempo=f'atempo={rate:.8f},apad=pad_dur=0.1,atrim=duration={exact:.8f}'
        measured=run(ffmpeg,[*args,'-af',tempo+',loudnorm=I=-16:TP=-2:LRA=11:print_format=json','-f','null','-'])
        stats=json.loads(measured[measured.rfind('{'):measured.rfind('}')+1])
        gain=min(-16-float(stats['input_i']),-2-float(stats['input_tp']))
        file=clips/f'{i:02d}.wav'
        run(ffmpeg,['-loglevel','error',*args,'-af',tempo+f',volume={gain:.4f}dB','-ac','2','-ar','44100','-c:a','pcm_s16le','-y',file])
        length=mutagen.File(file).info.length
        cross=transition(items[i-1],item) if i else None
        if cross:clock-=cross['seconds']
        timeline.append({'label':song['label'],'start':round(clock,5),'end':round(clock+length,5),'sourceStart':option['start'],
                         'sourceEnd':option['end'],'sourceBpm':source_bpm,'targetBpm':item['targetBpm'],'rate':rate,'transition':cross})
        clock+=length;inputs.extend(['-i',file]);print('CLIP',i+1,song['label'],round(length,1),'seconds',flush=True)
    filters=[];previous='0:a'
    for i in range(1,len(items)):
        cross=timeline[i]['transition'];name=f'm{i}'
        length=cross['seconds']
        if cross['kind']=='beat-overlap':
            # Move the bass from one record to the other in the middle of the
            # phrase, instead of layering two full-volume kick/bass parts.
            prior_end=timeline[i-1]['end'];old_length=prior_end
            filters.extend([
                f'[{previous}]acrossover=split=180:order=4th[ol{i}][oh{i}]',
                f'[{i}:a]acrossover=split=180:order=4th[nl{i}][nh{i}]',
                f'[ol{i}]afade=t=out:st={old_length-length/2-.06:.8f}:d=0.12:curve=qsin[olc{i}]',
                f'[nl{i}]afade=t=in:st={length/2-.06:.8f}:d=0.12:curve=qsin[nlc{i}]',
                f'[olc{i}][nlc{i}]acrossfade=d={length:.8f}:c1=nofade:c2=nofade[low{i}]',
                f'[oh{i}][nh{i}]acrossfade=d={length:.8f}:c1=qsin:c2=qsin[high{i}]',
                f'[low{i}][high{i}]amix=inputs=2:normalize=0[{name}]',
            ])
        else:filters.append(f'[{previous}][{i}:a]acrossfade=d={length:.8f}:c1=qsin:c2=qsin[{name}]')
        previous=name
    outro=min(max(float(plan.get('outroFadeSeconds',.6)),.04),min(10,clock/2))
    filters.append(f'[{previous}]afade=t=in:d=0.04,afade=t=out:st={clock-outro:.6f}:d={outro:.6f},alimiter=limit=0.84:level=false[master]')
    output=work/'Volleyball-DJ-preview-01.mp3'
    run(ffmpeg,['-loglevel','error',*inputs,'-filter_complex',';'.join(filters),'-map','[master]','-c:a','libmp3lame','-b:a','192k',
                '-metadata','title='+plan.get('title','Волейбол DJ · проба 01'),'-metadata','artist=AI DJ','-y',output],timeout=300)
    actual=mutagen.File(output).info.length
    if abs(actual-clock)>.25:raise RuntimeError('Mix duration verification failed')
    atomic_json(work/'timeline.json',{'duration':actual,'tracks':timeline})
    lines=['Волейбол DJ · проба 01','',f'Длительность: {timestamp(actual)}','']
    for item in timeline:
        kind='начало' if not item['transition'] else 'сведение в ритм' if item['transition']['kind']=='beat-overlap' else 'короткая смена темпа'
        lines.append(f'{timestamp(item["start"])} — {item["label"]} · {kind}')
    (work/'tracklist.txt').write_text('\n'.join(lines)+'\n')
    # A short separate listening reel makes the transitions easy to evaluate.
    bits=[]
    for i,item in enumerate(timeline[1:]):
        file=work/f'seam-{i:02d}.wav';start=max(0,item['start']-5)
        run(ffmpeg,['-loglevel','error','-ss',f'{start:.5f}','-i',output,'-t','13','-c:a','pcm_s16le','-y',file]);bits.append(file)
    listing=work/'seams.ffconcat';listing.write_text('ffconcat version 1.0\n'+''.join(f"file '{p.name}'\n" for p in bits))
    run(ffmpeg,['-loglevel','error','-f','concat','-safe','1','-i',listing,'-c:a','libmp3lame','-b:a','192k',
                '-metadata','title=Волейбол DJ · только переходы','-metadata','artist=AI DJ','-y',work/'Volleyball-DJ-transitions-01.mp3'])
    for file in bits:file.unlink()
    listing.unlink();print('MIX_READY',timestamp(actual),output.name,flush=True)


def timestamp(value):
    seconds=max(0,int(value));return f'{seconds//60}:{seconds%60:02d}'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('stage',choices=['prepare','analyse','render'])
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--ffmpeg',default='ffmpeg');args=parser.parse_args()
    try:
        if args.stage=='prepare':prepare(args.work)
        elif args.stage=='analyse':analyse(args.work,args.ffmpeg)
        else:render(args.work,args.ffmpeg)
    except Exception as error:
        # Unexpected network exceptions can contain signed Disk URLs; don't log them.
        print('DJ_PREVIEW_FAILED',type(error).__name__,str(error)[:1200] if isinstance(error,RuntimeError) else '',flush=True);raise SystemExit(1)


if __name__=='__main__':main()
