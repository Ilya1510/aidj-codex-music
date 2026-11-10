"""Render the exact first minutes and selected seams, without altering originals."""
import argparse
import json
import math
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import wave

from .config import Settings, atomic_json

SAMPLE_RATE=22050


def clip_arguments(item,song,source,output):
    """Construct a source-pinned fixed clean-cut render matching runtime bounds."""
    finite=lambda v:type(v) in (int,float) and math.isfinite(v)
    numbers=[item.get(k) for k in ('start','end','fadeIn','fadeOut','rate','transitionSeconds')]
    total=song.get('duration');lo=song.get('range_start') or 0.
    hi=song.get('range_end');hi=total if hi is None else hi
    if (item.get('md5')!=song.get('md5') or not finite(total) or not 0<total<=1800 or
            not all(finite(v) for v in numbers) or not finite(lo) or not finite(hi) or
            not 0<=lo<=item['start']<item['end']<=min(total,hi)+.001 or item['end']-item['start']<.2 or
            item['rate']!=1 or item['transitionSeconds']!=0 or item.get('transitionKind')!='clean-cut' or
            any(not 0<=item[k]<=min(8,(item['end']-item['start'])/2) for k in ('fadeIn','fadeOut'))):
        raise RuntimeError('FIXED_PREVIEW_SOURCE_OR_BOUNDS')
    completion=hi>=total and abs(item['end']-total)<=.001
    duration=item['end']-item['start']
    lufs,peak=song.get('integrated_lufs'),song.get('true_peak_dbtp')
    measured=(finite(lufs) and -70<=lufs<=0 and finite(peak) and -60<=peak<=6 and
              song.get('loudness_method')=='ebu-r128-v1' and song.get('loudness_status')=='complete')
    gain=min(12.,-16-lufs,-1-peak) if measured else 0.
    filters=[f'volume={gain:.5f}dB','volume=0.7']
    if item['fadeIn']>0:filters.append(f'afade=t=in:st=0:d={item["fadeIn"]:.5f}')
    # Full-file positive endings need their actual decoded duration first. A
    # second streaming pass applies only that tail envelope, never gain twice.
    if item['fadeOut']>0 and not completion:filters.append(f'afade=t=out:st={duration-item["fadeOut"]:.5f}:d={item["fadeOut"]:.5f}')
    args=['-ss',item['start'],'-i',source]
    if not completion:args.extend(['-t',duration])
    args.extend(['-vn','-af',','.join(filters),'-ar',str(SAMPLE_RATE),'-ac','2','-c:a','pcm_s16le','-y',output])
    return args,completion


def rendered_frames(path):
    """Read only the WAV header; clocks use decoded samples, not metadata spans."""
    with wave.open(str(path),'rb') as audio:
        if (audio.getnchannels()!=2 or audio.getsampwidth()!=2 or audio.getframerate()!=SAMPLE_RATE or
                audio.getcomptype()!='NONE' or audio.getnframes()<=0):
            raise RuntimeError('FIXED_PREVIEW_PCM_FORMAT')
        return audio.getnframes()


def ending_fade_arguments(source,output,frames,fade):
    if type(frames) is not int or frames<=0 or type(fade) not in (int,float) or not math.isfinite(fade) or not 0<fade<frames/SAMPLE_RATE:
        raise RuntimeError('FIXED_PREVIEW_END_FADE')
    return ['-i',source,'-af',f'afade=t=out:st={frames/SAMPLE_RATE-fade:.8f}:d={fade:.8f}',
            '-c:a','pcm_s16le','-y',output]


def timeline_row(item,song,position,clock_frames,frames,completion):
    if type(frames) is not int or frames<=0 or type(clock_frames) is not int or clock_frames<0:
        raise RuntimeError('FIXED_PREVIEW_PCM_TIMELINE')
    return {'position':position,'artist':song['artist'],'title':song['title'],
            'start':clock_frames/SAMPLE_RATE,'end':(clock_frames+frames)/SAMPLE_RATE,
            'sourceStart':item['start'],'sourceEnd':item['end'],'sourceCompletion':completion,
            'renderedFrames':frames,'sampleRate':SAMPLE_RATE}


def render(settings, work, decoder, count=6):
    from .disk import Disk
    plan=json.loads((work/'plan.json').read_text());items=plan['tracks'][:count]
    if not 2<=len(items)<=10 or shutil.disk_usage(work).free<512*1024**2:
        raise RuntimeError('FIXED_PREVIEW_BUDGET')
    if any(r['rate']!=1 or r['transitionSeconds']!=0 for r in items):
        raise RuntimeError('FIXED_PREVIEW_UNSUPPORTED_TRANSITION')
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(settings);timeline=[];clock_frames=0
    def run(args):
        child=subprocess.run([decoder,'-nostdin','-v','error','-threads','1','-filter_threads','1',*map(str,args)],
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        if child.returncode:raise RuntimeError('FIXED_PREVIEW_FFMPEG')
    with tempfile.TemporaryDirectory(prefix='preview-',dir=work) as directory:
        scratch=Path(directory);paths=[]
        for i,item in enumerate(items):
            song=db.execute('''SELECT a.*,r.start AS range_start,r.end AS range_end,
                l.integrated_lufs,l.true_peak_dbtp,l.method AS loudness_method,l.status AS loudness_status
                FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id
                LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?''',(item['trackId'],)).fetchone()
            if not song or song['md5']!=item['md5']:raise RuntimeError('FIXED_PREVIEW_SOURCE')
            song=dict(song)
            source=scratch/('source'+Path(song['remote_path']).suffix)
            output=scratch/f'{i:02}.wav'
            arguments,completion=clip_arguments(item,song,source,output)
            disk.download(song['remote_path'],source,expected_md5=song['md5'])
            run(arguments);source.unlink()
            frames=rendered_frames(output)
            if completion and item['fadeOut']>0:
                ramped=scratch/f'{i:02}-ending.wav'
                run(ending_fade_arguments(output,ramped,frames,item['fadeOut']))
                if rendered_frames(ramped)!=frames:raise RuntimeError('FIXED_PREVIEW_END_FADE_LENGTH')
                ramped.replace(output)
            paths.append(output)
            timeline.append(timeline_row(item,song,i+1,clock_frames,frames,completion))
            clock_frames+=frames
        concat=scratch/'concat.txt';concat.write_text(''.join("file '"+str(p)+"'\n" for p in paths))
        run(['-f','concat','-safe','0','-i',concat,'-c:a','libmp3lame','-b:a','128k','-y',work/'DJ-1-first-minutes.mp3'])
        # Only actual seams: 6s before and after each boundary. Every cut inside
        # a seam is exactly the one authored for playback, with identical gain.
        seams=[];seam_timeline=[];seam_clock=0
        for i in range(len(paths)-1):
            left=scratch/f'tail{i}.wav';right=scratch/f'head{i}.wav'
            run(['-sseof','-6','-i',paths[i],'-c:a','pcm_s16le','-y',left])
            run(['-i',paths[i+1],'-t','6','-c:a','pcm_s16le','-y',right]);seams.extend([left,right])
            left_frames,right_frames=rendered_frames(left),rendered_frames(right)
            seam_timeline.append({'position':i+1,'fromTrackId':items[i]['trackId'],
                'toTrackId':items[i+1]['trackId'],'start':seam_clock/SAMPLE_RATE,
                'boundary':(seam_clock+left_frames)/SAMPLE_RATE,
                'end':(seam_clock+left_frames+right_frames)/SAMPLE_RATE,
                'leftFrames':left_frames,'rightFrames':right_frames,'sampleRate':SAMPLE_RATE})
            seam_clock+=left_frames+right_frames
            if i<3:
                one=scratch/'one-seam.txt';one.write_text("file '"+str(left)+"'\nfile '"+str(right)+"'\n")
                marker=round(1200*left_frames/(left_frames+right_frames))
                filters=f'[0:a]asplit=2[a][b];[a]showwavespic=s=1200x180:colors=0xcfff62,format=rgb24[w];[b]showspectrumpic=s=1200x300:legend=0:scale=log:fscale=log,format=rgb24[s];[w][s]vstack=inputs=2,drawbox=x={marker}:y=0:w=2:h=480:color=red:t=fill[v]'
                run(['-f','concat','-safe','0','-i',one,'-filter_complex_threads','1','-filter_complex',filters,'-map','[v]','-frames:v','1','-y',work/f'seam-{i+1}.png'])
        concat.write_text(''.join("file '"+str(p)+"'\n" for p in seams))
        run(['-f','concat','-safe','0','-i',concat,'-c:a','libmp3lame','-b:a','128k','-y',work/'DJ-1-transitions.mp3'])
    db.close();atomic_json(work/'preview-timeline.json',{'revision':plan['revision'],'tracks':timeline,'seams':seam_timeline})
    print('FIXED_PREVIEW_READY',len(items),round(clock_frames/SAMPLE_RATE,1),'seconds',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True);parser.add_argument('--ffmpeg',required=True)
    args=parser.parse_args()
    try:render(Settings(),args.work,args.ffmpeg)
    except Exception as exc:
        print('FIXED_PREVIEW_FAILED',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('FIXED_') else '',flush=True)
        raise SystemExit(1)
