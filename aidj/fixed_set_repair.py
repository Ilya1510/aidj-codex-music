"""Serial, resumable acoustic review of every boundary of a frozen DJ set.

Song order never enters an optimiser here. Unknown or merely ASR-spaced exits
remain explicit exceptions; they cannot be published as quiet boundaries.
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
from .quiet_boundaries import boundary_points, detect_quiet_regions, digital_silence_regions
from .fixed_sets import signature, validate

VERSION=2
SR=16000
HOP=.01


def merge(rows, padding=0):
    out=[]
    for a,b in sorted((row[0],row[1]) for row in rows):
        a-=padding;b+=padding
        if out and a<=out[-1][1]:out[-1][1]=max(out[-1][1],b)
        else:out.append([a,b])
    return out


def covered(coverage,a,b):
    return any(x<=a and b<=y for x,y in merge(coverage))


def word_clear(words,a,b,padding=.3):
    return not any(x-padding<b and y+padding>a for x,y,*_ in words)


def probe_supports(probe,point):
    return probe['confident']>=3 and probe['start']<=point['fadeStart']-.15 and probe['end']>=point['time']+.1


def selection_error(item):
    """Revalidate checkpoint evidence before it can enter a public set."""
    if item.get('status')!='ready':return 'unresolved'
    row=item.get('row',{});point=item.get('exit',{});method=point.get('method')
    required=('time','fade','fadeStart','regionStart','regionEnd','maxRms','relativeMax')
    if any(type(point.get(k)) not in (int,float) or not math.isfinite(point[k]) for k in required):return 'invalid-proof'
    if abs(row.get('end',-1)-point['time'])>.001 or abs(row.get('fadeOut',-1)-point['fade'])>.001:return 'different-cut'
    if point['regionStart']>point['fadeStart']-.149 or point['regionEnd']<point['time']+.099:return 'fade-outside-proof'
    if method=='reobserved-instrumental-fade':
        if not any(probe_supports(p,point) for p in item.get('repairProbes',[])):return 'missing-local-reprobe'
        if not word_clear(item.get('observedWords',[]),point['fadeStart']-.15,point['time']+.1,.4):return 'vocal-over-fade'
        return None
    limits={'strict-quiet':(.08,.8),'owner-listened-quiet':(.081,.7),
            'soft-quiet':(.18,1.2),'instrumental-fade':(.35,1.6)}
    if method=='digital-silence':
        return None if point['maxRms']<=.0001 and point['regionEnd']-point['regionStart']>=1.2-1e-6 else 'not-digital-silence'
    if method not in limits:return 'unknown-exit-method'
    threshold,minimum=limits[method]
    if point['regionEnd']-point['regionStart']<minimum-1e-6:return 'brief-valley'
    if point['relativeMax']>threshold+1e-6 and point['maxRms']>.0005:return 'music-not-quiet'
    mixed=point.get('proof')=='observed-or-physical-quiet' and point.get('validated') is True
    if not point.get('coverageComplete') and not point.get('absoluteSilence') and not mixed and method!='owner-listened-quiet':return 'unknown-vocals'
    if not word_clear(item.get('observedWords',[]),point['fadeStart']-.149,point['time']+.099,point.get('wordPadding',.3)):
        return 'vocal-over-fade'
    return None


def entries_for(old,lo,hi,words,coverage,regions,owner=()):
    choices={}
    def add(t,kind,penalty,fade=.1):
        if lo<=t<hi and (t not in choices or penalty<choices[t]['penalty']):
            choices[t]={'time':t,'kind':kind,'penalty':penalty,'fade':fade}
    add(lo,'source-start',1.1,0 if not word_clear(words,lo,lo+.1,0) else .08)
    for t in owner:add(t,'owner',0)
    if abs(old['start']-lo)<.001:add(old['start'],'kept-source-start',.2,0 if not word_clear(words,lo,lo+.1,0) else .08)
    elif word_clear(words,old['start']-.1,old['start']+.1,.2) and covered(coverage,old['start']-.2,old['start']+.2):add(old['start'],'kept-guarded',.2)
    for point in boundary_points(regions)['entries']:add(point['time'],'quiet-entry',.55,point['fade'])
    occupied=merge(words)
    for left,right in zip(occupied,occupied[1:]):
        a,b=left[1],right[0]
        if b-a>=.9 and covered(coverage,a,b):
            t=b-.35
            if word_clear(words,t-.04,t+.1,.25):add(t,'observed-word-gap',1)
    return list(choices.values())


def choose(old,song,evidence,rms,owner=(),rejected=(),gold=None):
    import numpy as np
    words=evidence.get('words',[]);coverage=evidence.get('coverage',[])
    lo=song['lo'];hi=min(song['hi'],len(rms)*HOP)
    first=math.ceil(lo/HOP);last=math.floor(hi/HOP)
    levels=rms[first:last];reference=max(.00001,float(np.quantile(levels,.75)))
    ends=[];all_regions=[]
    for tier,(threshold,minimum,padding,method) in enumerate([
        (.08,.8,.3,'strict-quiet'),(.18,1.2,.4,'soft-quiet'),(.35,1.6,.5,'instrumental-fade')]):
        regions=detect_quiet_regions(levels,HOP,first*HOP,words=words,coverage=coverage,
            reference=reference,max_relative=threshold,min_seconds=minimum,absolute_silence=.0005,word_padding=padding)
        for point in boundary_points(regions,max_fade=.8)['exits']:
            region=next(r for r in regions if r['start']==point['regionStart'] and r['end']==point['regionEnd'])
            ends.append({**point,'tier':tier,'method':method,'maxRms':region['maxRms'],'wordPadding':padding})
        if tier==0:all_regions=[r for r in regions if r['validated']]
    digital=digital_silence_regions(levels,HOP,first*HOP)
    for point in boundary_points(digital)['exits']:
        region=next(r for r in digital if r['start']==point['regionStart'] and r['end']==point['regionEnd'])
        ends.append({**point,'tier':0,'method':'digital-silence','maxRms':region['maxRms'],'wordPadding':0})
    all_regions+=digital
    entries=entries_for(old,lo,hi,words,coverage,all_regions,owner)
    # Strong observed vocal-free gaps can be reviewed as engineered musical
    # fades, but remain exceptions requiring a second ASR observation.
    spans=merge(words)
    gaps=[(a[1]+.5,b[0]-.5) for a,b in zip(spans,spans[1:]) if b[0]-a[1]>=2.8]
    if spans and hi-spans[-1][1]>=2.8:gaps.append((spans[-1][1]+.5,hi-.15))
    for a,b in gaps:
        a=max(lo,a);b=min(hi,b)
        if b-a<1.5 or not covered(coverage,a,b):continue
        # Prefer an actually quieter part of the instrumental fill.
        possibles=[]
        for t in np.arange(a,b-1.4,.2):
            block=rms[math.ceil(t/HOP):math.floor((t+1.4)/HOP)]
            if len(block):possibles.append((float(np.quantile(block,.9)),float(t)))
        if not possibles:continue
        peak,t=min(possibles)
        ends.append({'time':t+1.4,'fade':1.2,'fadeStart':t+.2,'tier':3,'method':'observed-instrumental-needs-probe',
            'regionStart':t,'regionEnd':t+1.5,'relativeMax':peak/reference,'maxRms':peak,
            'validated':False,'coverageComplete':True,'absoluteSilence':False,'wordPadding':.5,'proof':'long-observed-gap'})
    candidates=[]
    for end in ends:
        if any(abs(end['time']-t)<.35 for t in rejected):continue
        for entry in entries:
            span=end['time']-entry['time']
            if not min(45,hi-lo-.1)<=span<=100:continue
            if any(abs(entry['time']-t)<.35 for t in rejected):continue
            score=end['tier']*10+entry['penalty']+abs(span-62)/45+abs(entry['time']-old['start'])/200
            candidates.append((score,entry,end))
    if gold:
        # Preserve the user-approved Awoo example, rechecking it against the
        # actual physical region instead of trusting the earlier ASR marker.
        begin=gold['quietWindow']['start'];finish=gold['quietWindow']['end']
        measured=rms[math.ceil(begin/HOP):math.floor(finish/HOP)]
        peak=float(np.max(measured)) if len(measured) else math.inf
        entry=next((e for e in entries if abs(e['time']-old['start'])<.025),None)
        contained=begin<=gold['end']-gold['fadeOut']-.15 and gold['end']<=finish
        in_range=lo<=old['start']<gold['end']<=hi+.001
        vetoed=any(min(abs(old['start']-t),abs(gold['end']-t))<.35 for t in rejected)
        if entry and contained and in_range and not vetoed and peak<=reference*.081 and word_clear(words,begin,finish,.3):
            selected={'time':gold['end'],'fade':gold['fadeOut'],'fadeStart':gold['end']-gold['fadeOut'],
                'regionStart':begin,'regionEnd':finish,'method':'owner-listened-quiet','tier':0,
                'validated':True,'proof':'owner-listened-continuous-quiet','maxRms':peak,'relativeMax':peak/reference,
                'coverageComplete':covered(coverage,begin,finish),'absoluteSilence':peak<=.0005,'wordPadding':.3}
            candidates.append((-10,entry,selected))
    if not candidates:
        return {'status':'needs-review','trackId':old['trackId'],'title':song['title'],'reason':'no-long-vocal-free-quiet-region',
                'quietRegions':len(all_regions),'sourceEnd':hi,'lastWordEnd':max((w[1] for w in words),default=None)}
    candidates.sort(key=lambda v:v[0]);score,entry,end=candidates[0]
    row={**old,'start':round(entry['time'],4),'end':round(end['time'],4),'fadeIn':entry['fade'],'fadeOut':round(end['fade'],4),
         'transitionSeconds':0,'transitionKind':'clean-cut','rate':1}
    gain=min(12,-16-song['lufs'],-1-song['peak']) if song.get('lufs') is not None else 0
    end['maxNormalizedDbfs']=round(20*math.log10(max(1e-10,end['maxRms']))+gain+20*math.log10(.7),2)
    return {'status':'ready' if end['validated'] else 'needs-probe','trackId':old['trackId'],'md5':old['md5'],
            'artist':song['artist'],'title':song['title'],'row':row,'entry':entry,'exit':end,
            'referenceRms':reference,'score':score,'candidateCount':len(candidates),
            'old':old,'alternatives':[{'score':s,'entry':en,'exit':ex} for s,en,ex in candidates[1:5]]}


def run(settings,work,decoder,limit=None):
    import numpy as np
    from .disk import Disk
    work.mkdir(exist_ok=True,parents=True);folder=work/'tracks';folder.mkdir(exist_ok=True)
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    baseline=work/'baseline.json'
    if not baseline.exists():atomic_json(baseline,json.loads(db.execute("SELECT payload FROM dj_fixed_sets WHERE id='dj1'").fetchone()[0]))
    plan=json.loads(baseline.read_text());curation=settings.state/'research/dj1-curation'
    owner=json.loads((curation/'owner-entries.json').read_text());bad=json.loads((curation/'owner-rejected.json').read_text())
    gold=json.loads((curation/'curator-map.json').read_text()).get('reviewedBoundaries',[])
    gold={g['trackId']:g for g in gold};disk=Disk(settings);count=0;model=None
    def observe(evidence,y,lo,hi):
        nonlocal model
        from .dj_worker import numeric_words,replace_observed_words
        if model is None:
            from faster_whisper import WhisperModel
            model=WhisperModel('base',device='cpu',compute_type='int8',cpu_threads=3,num_workers=1,
                download_root=str(settings.state/'dj-models'),local_files_only=True)
        sample=np.array(y[int(lo*SR):int(hi*SR)],dtype=np.float32,copy=True)
        segments,_=model.transcribe(sample,beam_size=3,temperature=0.0,word_timestamps=True,
            condition_on_previous_text=False,vad_filter=False)
        fresh=[]
        for part in segments:fresh.extend(numeric_words(part,lo))
        # An empty new recognition must never erase previously detected vocals.
        if sum(bool(w[2]) for w in fresh)>=3:evidence['words']=replace_observed_words(evidence.get('words',[]),fresh,lo,hi)
        else:evidence['words']=[*evidence.get('words',[]),*fresh]
        evidence['coverage']=[*evidence.get('coverage',[]),[lo,hi]]
        evidence.setdefault('repairProbes',[]).append({'start':lo,'end':hi,'words':len(fresh),'confident':sum(bool(w[2]) for w in fresh)})
    for at,old in enumerate(plan['tracks'],1):
        asset=db.execute('SELECT a.*,r.start AS lo,r.end AS hi,l.integrated_lufs AS lufs,l.true_peak_dbtp AS peak FROM web_assets a LEFT JOIN playback_ranges r ON r.track_id=a.id LEFT JOIN audio_loudness l ON l.md5=a.md5 WHERE a.id=?',(old['trackId'],)).fetchone()
        if not asset or asset['md5']!=old['md5']:raise RuntimeError('REPAIR_SOURCE_CHANGED')
        song=dict(asset);song['lo']=song['lo'] or 0;song['hi']=song['hi'] if song['hi'] is not None else song['duration']
        raw=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(old['md5'],)).fetchone()[0]
        fingerprint=hashlib.sha256(json.dumps([VERSION,old,song['lo'],song['hi'],raw,owner.get(old['trackId'],[]),bad.get(old['trackId'],[]),gold.get(old['trackId'])],sort_keys=True).encode()).hexdigest()
        target=folder/(old['trackId']+'.json')
        if target.exists() and json.loads(target.read_text()).get('inputHash')==fingerprint:continue
        if limit is not None and count>=limit:break
        if shutil.disk_usage(work).free<768*1024**2:raise RuntimeError('REPAIR_DISK')
        if song['bytes']>40*1024**2 or song['duration']>1800:raise RuntimeError('REPAIR_SOURCE_LIMIT')
        with tempfile.TemporaryDirectory(prefix='source-',dir=work) as directory:
            src=Path(directory)/('source'+Path(song['remote_path']).suffix);pcm=Path(directory)/'audio.f32'
            disk.download(song['remote_path'],src,expected_md5=old['md5'])
            child=subprocess.run([decoder,'-nostdin','-v','error','-threads','1','-filter_threads','1','-i',str(src),'-t','1800','-ac','1','-ar',str(SR),'-f','f32le','-y',str(pcm)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
            if child.returncode:raise RuntimeError('REPAIR_DECODE')
            y=np.memmap(pcm,dtype='<f4',mode='r');n=len(y)//160
            rms=np.empty(n,dtype=np.float32)
            for first in range(0,n,1000):
                end=min(n,first+1000);block=np.asarray(y[first*160:end*160]).reshape(-1,160)
                rms[first:end]=np.sqrt(np.mean(block*block,axis=1))
            evidence=json.loads(raw)
            levels=rms[math.ceil(song['lo']/HOP):math.floor(min(song['hi'],len(rms)*HOP)/HOP)]
            ref=max(.00001,float(np.quantile(levels,.75)))
            unknown=detect_quiet_regions(levels,HOP,math.ceil(song['lo']/HOP)*HOP,
                words=evidence.get('words',[]),coverage=evidence.get('coverage',[]),reference=ref,
                max_relative=.18,min_seconds=.9,absolute_silence=.0005,word_padding=.3)
            unknown=[r for r in unknown if not r['validated'] and r['end']-song['lo']>=45]
            if unknown and old['trackId'] not in gold:
                region=min(unknown,key=lambda r:abs(r['start']-old['end']))
                end=min(song['duration'],region['end']+3);begin=max(0,end-28)
                observe(evidence,y,begin,end)
            result=choose(old,song,evidence,rms,owner.get(old['trackId'],[]),bad.get(old['trackId'],[]),gold.get(old['trackId']))
            for attempt in range(3):
                if result['status']!='needs-probe':break
                end=result['exit']['time'];begin=max(0,end-14);finish=min(song['duration'],begin+28)
                observe(evidence,y,begin,finish)
                result=choose(old,song,evidence,rms,owner.get(old['trackId'],[]),bad.get(old['trackId'],[]),gold.get(old['trackId']))
                if result['status']=='needs-probe':
                    point=result['exit'];a=point['fadeStart']-.15;b=point['time']+.1
                    fresh=evidence['repairProbes'][-1]
                    if probe_supports(fresh,point) and word_clear(evidence['words'],a,b,.4):
                        result['status']='ready';point.update(validated=True,method='reobserved-instrumental-fade',proof='long-gap-reobserved-and-guarded')
            result['repairProbes']=evidence.get('repairProbes',[])
            result['observedWords']=evidence.get('words',[])
            result['inputHash']=fingerprint;result['position']=at;atomic_json(target,result)
            y._mmap.close();del y,rms
        count+=1;print('REPAIRED',at,result['status'],old['trackId'],result.get('exit',{}).get('method','none'),flush=True)
    db.close()


def assemble(settings,work):
    baseline=json.loads((work/'baseline.json').read_text());tracks=[];audits=[]
    for old in baseline['tracks']:
        path=work/'tracks'/(old['trackId']+'.json')
        if not path.exists():raise RuntimeError('REPAIR_INCOMPLETE')
        item=json.loads(path.read_text())
        error=selection_error(item)
        if error:raise RuntimeError('REPAIR_UNRESOLVED_'+error)
        tracks.append(item['row']);audits.append(item)
    assert [r['trackId'] for r in tracks]==[r['trackId'] for r in baseline['tracks']]
    plan={**baseline,'tracks':tracks,'revision':signature(tracks)}
    db=sqlite3.connect(f'file:{settings.state / "bot.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    assets={r['id']:dict(r) for r in db.execute('SELECT id,md5,duration FROM web_assets')}
    ranges={r['track_id']:(r['start'],r['end']) for r in db.execute('SELECT * FROM playback_ranges')}
    plan=validate(plan,assets,ranges);db.close()
    atomic_json(work/'plan.json',plan);atomic_json(work/'audit.json',audits)
    print('REPAIR_ASSEMBLED',len(tracks),plan['revision'],plan['duration'])


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['run','assemble']);p.add_argument('--work',type=Path,required=True);p.add_argument('--ffmpeg');p.add_argument('--limit',type=int)
    args=p.parse_args();s=Settings()
    if args.action=='run':
        if not args.ffmpeg:raise RuntimeError('REPAIR_DECODER')
        run(s,args.work,args.ffmpeg,args.limit)
    else:assemble(s,args.work)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('QUIET_REPAIR_FAILED',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('REPAIR_') else '',flush=True)
        raise SystemExit(1)
