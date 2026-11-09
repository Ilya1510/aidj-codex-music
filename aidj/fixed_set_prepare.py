"""Resumable serial preparation of an explicitly curated DJ 1 plan.

Keeps only compact numeric features. One downloaded original and PCM file exist
at a time; all decoder work is bounded. No model output is called a listening
review. Run inside a separately memory/time limited process.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from .config import Settings, atomic_json
from .fixed_sets import signature

FEATURE_VERSION = 2
SR = 11025
HOP = 220


def source_rows(db, playlist_id):
    sql = """SELECT a.*,r.start AS allowed_start,r.end AS allowed_end
        FROM playlist_tracks p JOIN web_assets a ON a.track_key=p.track_key
        LEFT JOIN playback_ranges r ON r.track_id=a.id
        WHERE p.playlist_id=? AND p.state='complete' ORDER BY p.position"""
    seen = set()
    for row in db.execute(sql, (playlist_id,)):
        if row['md5'] in seen:continue
        seen.add(row['md5'])
        song = dict(row)
        song['allowed_start'] = song['allowed_start'] or 0
        if song['allowed_end'] is None:song['allowed_end'] = song['duration']
        yield song


def occupied(words, start, end):
    return sum(max(0, min(end, w[1])-max(start, w[0])) for w in words if w[1]>start and w[0]<end) / max(.001, end-start)


def points(song, graph, evidence, human=(), rejected=(), human_exits=()):
    """A gap between recognised words is evidence, not proof of a phrase end."""
    lo, hi = song['allowed_start'], min(song['duration'], song['allowed_end'])
    words = []
    for row in sorted(evidence.get('words', [])):
        if words and row[0]<=words[-1][1]:words[-1][1]=max(words[-1][1],row[1])
        else:words.append([row[0],row[1]])
    entries = [(lo, 'source-start', .35)]
    exits = [(hi, 'source-end', .35, .6)]
    primary = graph.get('anchors', {})
    entries.extend((p[0], 'word-guarded', .65) for p in primary.get('entries', []) if lo<=p[0]<hi)
    exits.extend((p[0], 'word-guarded', .65, p[1]) for p in primary.get('exits', []) if lo<p[0]<=hi)
    entries.extend((float(t), 'owner', 1.0) for t in human if lo<=t<hi)
    exits.extend((float(t), 'owner', 1.0, .15) for t in human_exits if lo<t<=hi)
    for a, b in zip(words, words[1:]):
        gap = b[0]-a[1]
        if gap < .20:continue
        # Leave breathing room before the next word and finish after the last.
        entry = max(a[1]+.045, b[0]-.14)
        end = min(b[0]-.045, a[1]+min(.55, gap*.7))
        if lo<=entry<hi:entries.append((entry, 'word-gap', min(.8, .4+gap*.25)))
        if lo<end<=hi:exits.append((end, 'word-gap', min(.8, .4+gap*.25), max(.04,end-a[1]-.035)))
    entries=[p for p in entries if not any(abs(p[0]-t)<.3 for t in rejected)]
    exits=[p for p in exits if not any(abs(p[0]-t)<.3 for t in rejected)]
    return entries, exits, words


def audio_features(pcm):
    import numpy as np
    y = np.memmap(pcm, dtype='<f4', mode='r')
    size = 2048
    n = max(0, (len(y)-size)//HOP+1)
    if n<1:raise ValueError('FIXED_AUDIO_SHORT')
    rms = np.zeros(n, dtype=np.float32)
    chroma = np.zeros((n, 12), dtype=np.float32)
    flux = np.zeros(n, dtype=np.float32)
    frequencies = np.fft.rfftfreq(size, 1/SR)
    use = (frequencies >= 65) & (frequencies <= 3000)
    pitch = np.rint(69+12*np.log2(frequencies[use]/440)).astype(int)%12
    window = np.hanning(size).astype(np.float32)
    prior = None
    for first in range(0,n,128):
        count = min(128,n-first)
        frames = np.lib.stride_tricks.sliding_window_view(y[first*HOP:(first+count-1)*HOP+size], size)[::HOP]
        rms[first:first+count] = np.sqrt(np.mean(frames*frames,axis=1))
        spectrum = np.abs(np.fft.rfft(frames*window,axis=1)).astype(np.float32)
        if prior is not None:flux[first] = np.maximum(spectrum[0]-prior,0).mean()
        if count>1:flux[first+1:first+count] = np.maximum(np.diff(spectrum,axis=0),0).mean(axis=1)
        prior = spectrum[-1]
        power = spectrum[:,use]**2
        for pc in range(12):chroma[first:first+count,pc] = power[:,pitch==pc].sum(axis=1)
    # Centre times, rather than pretending a 186 ms FFT window has 20 ms precision.
    times = (np.arange(n)*HOP+size/2)/SR
    return y, times, rms, chroma, flux


def analyse(song, graph, evidence, pcm, human=(), rejected=(), human_exits=()):
    import numpy as np
    y, times, rms, chroma, flux = audio_features(pcm)
    entries, exits, words = points(song,graph,evidence,human,rejected,human_exits)
    reference = max(.00001,float(np.quantile(rms,.75)))
    def local(time, direction):
        # Entry lookahead and exit lookbehind describe the music actually played.
        a,b = (time,time+2) if direction=='in' else (time-2,time)
        select = (times>=a)&(times<=b)
        if not select.any():select[np.argmin(abs(times-time))]=True
        c = chroma[select].mean(axis=0)
        c /= max(float(np.linalg.norm(c)),1e-9)
        near = (times>=time-.12)&(times<=time+.12)
        edge = float(np.min(rms[near])) if near.any() else float(rms[np.argmin(abs(times-time))])
        return {'energy': round(float(rms[select].mean()/reference),4),
                'edge': round(min(3,edge/reference),4),
                'chroma': [round(float(v),4) for v in c],
                'vocal': round(min(1,occupied(words,a,b)),4)}
    in_cache = {};out_cache = {}
    candidates = []
    for start, origin, confidence in entries:
        if origin not in ('owner','source-start') and occupied(words,start-.035,start+.035)>.05:continue
        if origin=='source-start' and start==0:confidence+=.1
        key=round(start,4)
        if key not in in_cache:in_cache[key]=local(start,'in')
        left=in_cache[key]
        for end, end_origin, end_confidence, fade in exits:
            span = end-start
            if not 45<=span<=78:continue
            key=round(end,4)
            if key not in out_cache:out_cache[key]=local(end,'out')
            right=out_cache[key]
            # Reject a fade overlapping known words, even when the old graph
            # labels this physical point safe. A fade budget belongs to its gap.
            fade = min(.8,fade,span/8)
            while fade>.045 and occupied(words,end-fade,end)>.05:fade*=.65
            fade = max(.025,fade)
            if end_origin not in ('source-end','owner') and occupied(words,end-fade,end)>.05:continue
            duration_cost = abs(span-62)*.027
            # Quiet endpoints > amplitude/energy excitement. Explicit owner
            # entries have priority but still need a usable ~minute-long exit.
            score = confidence*.8+end_confidence*.8-duration_cost-left['edge']*.9-right['edge']*1.3
            score -= max(0,start-song['allowed_start']-song['duration']*.55)*.002
            if origin=='owner':score+=1.1
            if end_origin=='owner':score+=1.1
            candidates.append({'start':round(start,4),'end':round(end,4),
                'fadeIn':min(.10,max(.025,.12-left['edge']*.06)), 'fadeOut':round(fade,4),
                'entryOrigin':origin,'exitOrigin':end_origin,'score':round(score,4),
                'in':left,'out':right})
    if not candidates:
        lo,hi=song['allowed_start'],min(song['duration'],song['allowed_end'])
        end=min(hi,lo+62)
        if end-lo<8:raise ValueError('FIXED_RANGE_TOO_SHORT')
        candidates=[{'start':lo,'end':end,'fadeIn':.1,'fadeOut':.8,'entryOrigin':'fallback',
            'exitOrigin':'fallback','score':-10,'in':local(lo,'in'),'out':local(end,'out')}]
    candidates.sort(key=lambda r:r['score'],reverse=True)
    options=[]
    for candidate in candidates:
        if any(abs(candidate['start']-r['start'])<3 and abs(candidate['end']-r['end'])<3 for r in options):continue
        options.append(candidate)
        if len(options)>=8:break
    tempo=evidence.get('bpm',0)
    global_c=chroma.mean(axis=0);global_c/=max(float(np.linalg.norm(global_c)),1e-9)
    del y
    return {'version':FEATURE_VERSION,'trackId':song['id'],'md5':song['md5'],
            'allowedStart':song['allowed_start'],'allowedEnd':song['allowed_end'],
            'title':song['title'],'artist':song['artist'],'duration':song['duration'],
            'bpm':round(tempo,3),'chroma':[round(float(v),4) for v in global_c],
            'options':options,'review':'numeric-analysis; no listening verdict'}


def extract(settings, work, playlist_id, limit=None, decoder=None):
    if decoder is None:
        from .audio import ffmpeg
        decoder=ffmpeg()
    from .disk import Disk
    work.mkdir(parents=True,exist_ok=True)
    folder=work/'features';folder.mkdir(exist_ok=True)
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    disk=Disk(settings);processed=0
    annotations={};rejected={};human_exits={}
    human_file=work/'owner-entries.json'
    if human_file.exists():annotations=json.loads(human_file.read_text())
    bad_file=work/'owner-rejected.json'
    if bad_file.exists():rejected=json.loads(bad_file.read_text())
    exit_file=work/'owner-exits.json'
    if exit_file.exists():human_exits=json.loads(exit_file.read_text())
    # Cursor rows are read serially; no library-sized audio arrays or sources.
    for song in source_rows(db,playlist_id):
        target=folder/(song['md5']+'.json')
        payload=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(song['md5'],)).fetchone()
        ev=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(song['md5'],)).fetchone()
        if not payload or not ev:raise RuntimeError('FIXED_SET_EVIDENCE_MISSING')
        fingerprint=hashlib.sha256(json.dumps([song['id'],song['md5'],song['duration'],song['allowed_start'],song['allowed_end'],payload[0],ev[0],annotations.get(song['id'],[]),rejected.get(song['id'],[])],sort_keys=True).encode()).hexdigest()
        if human_exits.get(song['id']):fingerprint=hashlib.sha256((fingerprint+json.dumps(human_exits[song['id']])).encode()).hexdigest()
        if target.exists():
            old=json.loads(target.read_text())
            if old.get('version')==FEATURE_VERSION and old.get('inputHash')==fingerprint:continue
        if limit is not None and processed>=limit:break
        if shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('FIXED_SET_DISK')
        if song['duration']>1800 or song['bytes']>40*1024**2:raise RuntimeError('FIXED_SET_SOURCE_LIMIT')
        with tempfile.TemporaryDirectory(prefix='one-song-',dir=work) as scratch:
            src=Path(scratch)/('source'+Path(song['remote_path']).suffix);pcm=Path(scratch)/'audio.f32'
            disk.download(song['remote_path'],src,expected_md5=song['md5'])
            args=[decoder,'-nostdin','-v','error','-threads','1','-filter_threads','1','-i',str(src),'-t','1800','-vn','-sn','-dn','-ac','1','-ar',str(SR),'-f','f32le','-y',str(pcm)]
            result=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
            if result.returncode:raise RuntimeError('FIXED_SET_DECODE')
            value=analyse(song,json.loads(payload[0]),json.loads(ev[0]),pcm,annotations.get(song['id'],[]),rejected.get(song['id'],[]),human_exits.get(song['id'],[]))
            value['inputHash']=fingerprint
            atomic_json(target,value)
        processed+=1
        print('FEATURE',processed,song['id'],song['artist'],song['title'],'options',len(value['options']),flush=True)
    db.close()


def distance(a,b):
    left,right=a['options'][0],b['options'][0]
    bpm_a,bpm_b=a['bpm'],b['bpm']
    # Half/double-time equivalence is only a grouping cue, never a sync claim.
    tempo=min(abs(math.log2(bpm_a*scale/bpm_b)) for scale in (.5,1,2)) if bpm_a and bpm_b else .3
    quiet=left['out']['energy']<.08 or right['in']['energy']<.08
    lc=a['chroma'] if quiet else left['out']['chroma']
    rc=b['chroma'] if quiet else right['in']['chroma']
    harmony=1-sum(x*y for x,y in zip(lc,rc))
    energy=0 if quiet else abs(math.log(max(.05,left['out']['energy'])/max(.05,right['in']['energy'])))
    return tempo*4+harmony*.9+energy*.45


def quiet_tail_end(start,end,levels,hop,words,threshold):
    """Remove sustained near-silence, retaining known words and a short tail."""
    active=[i for i,value in enumerate(levels) if value>threshold]
    last=start+(active[-1]+1)*hop if active else start
    word_end=max((min(end,w[1]) for w in words if start<w[1] and w[0]<end),default=start)
    return min(end,max(last+.12,word_end+.12))


def refine_tails(settings,work,decoder):
    """A second bounded pass only for selected excerpts with almost-silent tails."""
    import numpy as np
    from .disk import Disk
    curated=json.loads((work/'curator-map.json').read_text());disk=Disk(settings)
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    changed=0
    for song in source_rows(db,curated['playlistId']):
        path=work/'features'/(song['md5']+'.json');value=json.loads(path.read_text())
        index=curated.get('optionOverrides',{}).get(song['id'],0);option=value['options'][index]
        if option.get('tailChecked') or option['out']['energy']>=.08 or option['exitOrigin']=='owner':continue
        if shutil.disk_usage(work).free<512*1024**2:raise RuntimeError('FIXED_SET_DISK')
        ev=json.loads(db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(song['md5'],)).fetchone()[0])
        end=option['end'];start=max(option['start']+45,end-12)
        if end-start<.5:continue
        with tempfile.TemporaryDirectory(prefix='tail-',dir=work) as directory:
            source=Path(directory)/('source'+Path(song['remote_path']).suffix);pcm=Path(directory)/'tail.f32'
            disk.download(song['remote_path'],source,expected_md5=song['md5'])
            process=subprocess.run([decoder,'-nostdin','-v','error','-threads','1','-filter_threads','1','-ss',str(start),'-i',str(source),'-t',str(end-start),'-ac','1','-ar',str(SR),'-f','f32le','-y',str(pcm)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
            if process.returncode:raise RuntimeError('FIXED_SET_TAIL_DECODE')
            samples=np.fromfile(pcm,dtype='<f4');width=110;n=len(samples)//width
            rms=np.sqrt(np.mean(samples[:n*width].reshape(n,width)**2,axis=1))
            threshold=max(.0008,float(np.quantile(rms,.95))*.04)
            revised=quiet_tail_end(start,end,rms,width/SR,ev.get('words',[]),threshold)
            option['tailChecked']=True
            if end-revised>=.4:
                # Never move onto an explicitly rejected physical boundary.
                bad=json.loads((work/'owner-rejected.json').read_text()).get(song['id'],[])
                if any(abs(revised-t)<.3 for t in bad):revised=min(end,revised+.65)
                last_word=max((min(revised,w[1]) for w in ev.get('words',[]) if revised-2<w[1] and w[0]<revised),default=revised-.3)
                option['originalEnd']=end;option['end']=round(revised,4)
                option['fadeOut']=round(max(.025,min(.25,revised-last_word-.025)),4)
                option['exitOrigin']='quiet-tail'
                # The old tail's noise spectrum is unsuitable for harmonic cost.
                option['out']['chroma']=value['chroma'];option['out']['chromaSource']='global-after-tail-trim'
                changed+=1
                print('TAIL',song['id'],round(end-revised,3),'seconds removed',flush=True)
            atomic_json(path,value)
    db.close();print('TAIL_REVIEW_COMPLETE',changed,flush=True)


def compile_plan(work, playlist_id):
    curated=json.loads((work/'curator-map.json').read_text())
    wanted=set(curated['tracks'])
    features={}
    for file in (work/'features').glob('*.json'):
        song=json.loads(file.read_text())
        if song['trackId'] in wanted and song.get('version')==FEATURE_VERSION:
            index=curated.get('optionOverrides',{}).get(song['trackId'],0)
            if type(index) is not int or not 0<=index<len(song['options']):raise ValueError('FIXED_SET_OPTION_OVERRIDE')
            song['selectedOption']=index
            song['options']=[song['options'][index]]+[o for i,o in enumerate(song['options']) if i!=index]
            features[song['trackId']]=song
    if set(features)!=wanted:raise ValueError('FIXED_SET_FEATURES_INCOMPLETE')
    groups={aid:row['group'] for aid,row in curated['tracks'].items()};order=[]
    # Curator chooses musical blocks and opening sequence. Numeric comparisons
    # choose close neighbours within those editorial blocks, not random tracks.
    remaining=set(features)
    for aid in curated.get('openingIds',[]):
        if aid in remaining:order.append(features[aid]);remaining.remove(aid)
    for group in curated['groupOrder']:
        pool=[features[aid] for aid in sorted(remaining) if groups.get(aid,'other')==group]
        while pool:
            current=order[-1] if order else None
            selected=min(pool,key=lambda song:(distance(current,song) if current else abs(song['bpm']-120),song['trackId']))
            pool.remove(selected);remaining.remove(selected['trackId']);order.append(selected)
    if remaining:raise ValueError('FIXED_CURATOR_UNASSIGNED')
    # Explicit listening/editorial review can replace the proposed adjacency.
    # These edits are applied once at authoring time, never at playback time.
    for move in curated.get('moveAfter',[]):
        aid,anchor=move['trackId'],move['afterTrackId']
        if aid==anchor or aid not in features or anchor not in features:raise ValueError('FIXED_SET_EDITORIAL_MOVE')
        item=next(song for song in order if song['trackId']==aid)
        order.remove(item);at=next(i for i,song in enumerate(order) if song['trackId']==anchor)
        order.insert(at+1,item)
    tracks=[];audit=[]
    for i,song in enumerate(order):
        option=song['options'][0]
        row={'trackId':song['trackId'],'md5':song['md5'],'start':option['start'],'end':option['end'],
            'fadeIn':round(option['fadeIn'],4),'fadeOut':option['fadeOut'],'rate':1,
            'transitionKind':'clean-cut','transitionSeconds':0}
        tracks.append(row)
        audit.append({'position':i+1,'trackId':song['trackId'],'artist':song['artist'],'title':song['title'],
            'group':groups.get(song['trackId'],'other'),'bpm':song['bpm'],'optionIndex':song['selectedOption'],'option':option,
            'nextDistance':round(distance(song,order[i+1]),4) if i+1<len(order) else None})
    result={'id':'dj1','name':'DJ 1','playlistId':playlist_id,'revision':signature(tracks),'tracks':tracks}
    atomic_json(work/'plan.json',result);atomic_json(work/'audit.json',audit)
    print('COMPILED',len(tracks),'segments',round(sum(r['end']-r['start'] for r in tracks)/3600,2),'hours',flush=True)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['extract','refine','compile','publish'])
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--playlist',type=int,default=1);parser.add_argument('--limit',type=int)
    parser.add_argument('--ffmpeg')
    args=parser.parse_args();settings=Settings()
    if args.action=='extract':extract(settings,args.work,args.playlist,args.limit,args.ffmpeg)
    elif args.action=='refine':
        if not args.ffmpeg:raise RuntimeError('FIXED_SET_FFMPEG_REQUIRED')
        refine_tails(settings,args.work,args.ffmpeg)
    elif args.action=='compile':compile_plan(args.work,args.playlist)
    else:
        from .store import connect
        from .fixed_sets import publish
        db=connect(settings,dj_enrol=False)
        try:result=publish(db,json.loads((args.work/'plan.json').read_text()));print('PUBLISHED',len(result['tracks']),result['revision'])
        finally:db.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Transport exceptions can contain signed Disk URLs. Never log them.
        print('FIXED_PREPARATION_FAILED',type(exc).__name__,str(exc) if isinstance(exc,(ValueError,RuntimeError)) and str(exc).startswith('FIXED_') else '',flush=True)
        raise SystemExit(1)
