"""Render the exact first minutes and selected seams, without altering originals."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from .config import Settings, atomic_json


def render(settings, work, decoder, count=6):
    from .disk import Disk
    plan=json.loads((work/'plan.json').read_text());items=plan['tracks'][:count]
    if not 2<=len(items)<=10 or shutil.disk_usage(work).free<512*1024**2:
        raise RuntimeError('FIXED_PREVIEW_BUDGET')
    if any(r['rate']!=1 or r['transitionSeconds']!=0 for r in items):
        raise RuntimeError('FIXED_PREVIEW_UNSUPPORTED_TRANSITION')
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(settings);timeline=[];clock=0
    def run(args):
        child=subprocess.run([decoder,'-nostdin','-v','error','-threads','1','-filter_threads','1',*map(str,args)],
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        if child.returncode:raise RuntimeError('FIXED_PREVIEW_FFMPEG')
    with tempfile.TemporaryDirectory(prefix='preview-',dir=work) as directory:
        scratch=Path(directory);paths=[]
        for i,item in enumerate(items):
            song=db.execute('SELECT a.*,l.integrated_lufs,l.true_peak_dbtp FROM web_assets a LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?',(item['trackId'],)).fetchone()
            if not song or song['md5']!=item['md5']:raise RuntimeError('FIXED_PREVIEW_SOURCE')
            source=scratch/('source'+Path(song['remote_path']).suffix)
            disk.download(song['remote_path'],source,expected_md5=song['md5'])
            gain=min(12,-16-song['integrated_lufs'],-1-song['true_peak_dbtp']) if song['integrated_lufs'] is not None else 0
            duration=item['end']-item['start'];fade=item['fadeOut']
            filters=f'volume={gain:.5f}dB,volume=0.7,afade=t=in:st=0:d={max(.001,item["fadeIn"]):.5f},afade=t=out:st={duration-fade:.5f}:d={max(.001,fade):.5f}'
            output=scratch/f'{i:02}.wav'
            run(['-ss',item['start'],'-i',source,'-t',duration,'-vn','-af',filters,'-ar','22050','-ac','2','-c:a','pcm_s16le','-y',output]);source.unlink()
            paths.append(output)
            timeline.append({'position':i+1,'artist':song['artist'],'title':song['title'],
                             'start':round(clock,4),'end':round(clock+duration,4),'sourceStart':item['start'],'sourceEnd':item['end']})
            clock+=duration
        concat=scratch/'concat.txt';concat.write_text(''.join("file '"+str(p)+"'\n" for p in paths))
        run(['-f','concat','-safe','0','-i',concat,'-c:a','libmp3lame','-b:a','128k','-y',work/'DJ-1-first-minutes.mp3'])
        # Only actual seams: 6s before and after each boundary. Every cut inside
        # a seam is exactly the one authored for playback, with identical gain.
        seams=[]
        for i in range(len(paths)-1):
            left=scratch/f'tail{i}.wav';right=scratch/f'head{i}.wav'
            run(['-sseof','-6','-i',paths[i],'-c:a','pcm_s16le','-y',left])
            run(['-i',paths[i+1],'-t','6','-c:a','pcm_s16le','-y',right]);seams.extend([left,right])
            if i<3:
                one=scratch/'one-seam.txt';one.write_text("file '"+str(left)+"'\nfile '"+str(right)+"'\n")
                filters='[0:a]asplit=2[a][b];[a]showwavespic=s=1200x180:colors=0xcfff62,format=rgb24[w];[b]showspectrumpic=s=1200x300:legend=0:scale=log:fscale=log,format=rgb24[s];[w][s]vstack=inputs=2,drawbox=x=600:y=0:w=2:h=480:color=red:t=fill[v]'
                run(['-f','concat','-safe','0','-i',one,'-filter_complex_threads','1','-filter_complex',filters,'-map','[v]','-frames:v','1','-y',work/f'seam-{i+1}.png'])
        concat.write_text(''.join("file '"+str(p)+"'\n" for p in seams))
        run(['-f','concat','-safe','0','-i',concat,'-c:a','libmp3lame','-b:a','128k','-y',work/'DJ-1-transitions.mp3'])
    db.close();atomic_json(work/'preview-timeline.json',{'revision':plan['revision'],'tracks':timeline})
    print('FIXED_PREVIEW_READY',len(items),round(clock,1),'seconds',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True);parser.add_argument('--ffmpeg',required=True)
    args=parser.parse_args()
    try:render(Settings(),args.work,args.ffmpeg)
    except Exception as exc:
        print('FIXED_PREVIEW_FAILED',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('FIXED_') else '',flush=True)
        raise SystemExit(1)
