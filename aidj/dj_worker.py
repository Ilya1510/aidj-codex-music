"""Sequential CPU preparation. No LLM API, audio uploads or playlist mutations.

Optional environment: requirements-dj-runtime.txt. A bounded service owns one
scratch file at a time. Only numeric cues survive; transcripts are not stored.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from .config import Settings
from .store import connect
from .dj import complete,next_job,heartbeat,upgrade_queue,MAX_FULL_DURATION


def vocal_cues(words,beats,bpm,duration):
    """Require a bounded gap between recognised phrases, not missing ASR output.

    Fades stay after the preceding word and before the following word. This is
    conservative evidence, not a guarantee: singing can confuse ASR too.
    """
    if len(words)<8:return []
    words=sorted((w[0],w[1],w[2] if len(w)>2 else True) for w in words)
    gaps=[];end=words[0][1];confident=words[0][2]
    for start,stop,next_confident in words[1:]:
        # Incoming audio can fade in early. The outgoing deck stays at full gain
        # until after the final word, so the available gap needn't cover the whole mix.
        if confident and next_confident and .9<=start-end<=5:gaps.append((end+.2,start-.2))
        if stop>=end:end=stop;confident=next_confident
    cues=[]
    for target in (80,110,140):
        choices=[]
        for lo,hi in gaps:
            if hi>duration or not 45<=hi<=190 or abs(hi-target)>28:continue
            # A few extra seconds are preferable to an unnecessarily abrupt
            # exit: use more of the confirmed vocal gap before chasing the grid.
            candidates=[t for t in beats if lo+.5<=t<=hi]
            if hi-lo>=.5:candidates.append(hi)
            if not candidates:continue
            stop=min(candidates,key=lambda t:abs(t-target)+8*max(0,1.2-min(2,t-lo)))
            fade=min(2,stop-lo)
            if fade<.5:continue
            cue={'start':0,'end':round(stop,4),'bpm':round(bpm,4),'fade':round(fade,4),'mix':3.2}
            if bpm and beats:cue['grid']=round(beats[0]%(60/bpm),6)
            choices.append((abs(stop-target)+8*max(0,1.2-fade),cue))
        if choices:
            cue=min(choices,key=lambda c:c[0])[1]
            if cue not in cues:cues.append(cue)
    return cues


def merged_coverage(intervals):
    """Union observed intervals; unobserved holes never become vocal pauses."""
    merged=[]
    for lo,hi in sorted((float(lo),float(hi)) for lo,hi in intervals if hi>lo):
        if merged and lo<=merged[-1][1]+.001:merged[-1][1]=max(merged[-1][1],hi)
        else:merged.append([lo,hi])
    return merged


def coverage(evidence):
    if 'coverage' in evidence:return evidence['coverage']
    # v2 transcribed three contiguous 45-second windows. Older hand-authored
    # numeric fixtures covered the full file.
    return [[45,min(180,evidence['duration'])]] if evidence.get('version')==2 else [[0,evidence['duration']]]


def vocal_gaps(evidence,minimum=.9):
    words=sorted((float(w[0]),float(w[1]),bool(w[2]) if len(w)>2 else True) for w in evidence['words'])
    if len(words)<8:return []
    result=[];end=words[0][1];confident=words[0][2]
    for start,stop,good in words[1:]:
        # A missing transcription window must never look like an instrumental
        # gap. Both neighbours and the complete gap need observed coverage.
        observed=any(lo+.4<=end and start<=hi-.4 for lo,hi in coverage(evidence))
        if confident and good and observed and minimum-1e-7<=start-end<=5:result.append((end+.2,start-.2))
        if stop>=end:end=stop;confident=good
    return result


def section_energy(evidence,start,end):
    values=evidence.get('energy',[])
    if not values:return .5
    part=values[max(0,int(start)):max(int(start)+1,int(end))]
    return sum(part)/len(part) if part else .5


def thin_points(rows,limit=256):
    """Bound payload size without removing an entire part of a long song."""
    if len(rows)<=limit:return rows
    return [rows[round(i*(len(rows)-1)/(limit-1))] for i in range(limit)]


def anchor_graph(evidence):
    """Reusable whole-word boundaries, not a predetermined excerpt list.

    A runtime player may pair these points at any requested duration. Entry
    zero is the intact original start; every other point is observed between
    two confident words, with 0.2 seconds reserved on both sides.
    """
    duration=evidence['duration'];bpm=evidence['bpm'];beats=evidence['beats']
    starts={0.:round(section_energy(evidence,0,min(duration,8)),4)}
    for lo,hi in vocal_gaps(evidence,.5):
        if 0<lo<duration:starts[round(lo,4)]=round(section_energy(evidence,lo,min(duration,lo+8)),4)
    stops={}
    for lo,hi in vocal_gaps(evidence,.55):
        if 0<hi<=duration and hi-lo>=.15-1e-7:
            stops[round(hi,4)]=[round(min(2,hi-lo),4),round(section_energy(evidence,max(0,hi-8),hi),4)]
    # MD5-bound previously validated exits stay useful when the latest local
    # ASR view is uncertain. Do not manufacture entries from such an exit.
    for lo,hi in evidence.get('safeExits',[]):
        if 0<=lo<hi<=duration and .5<=hi-lo<=8:
            stop=round(hi,4);fade=round(min(2,hi-lo),4)
            if stop not in stops or fade>stops[stop][0]:stops[stop]=[fade,round(section_energy(evidence,max(0,hi-8),hi),4)]
    graph={'version':1,'wordGuarded':True,'bpm':round(bpm,4),
           'entries':thin_points([[t,energy] for t,energy in sorted(starts.items())]),
           'exits':thin_points([[t,*values] for t,values in sorted(stops.items())])}
    if bpm and beats:graph['grid']=round(beats[0]%(60/bpm),6)
    return graph


def usable_anchors(graph,minimum=10,maximum=120):
    """Count actual choices without storing an O(n²) Cartesian product."""
    entries=[row[0] for row in graph['entries']];count=0
    for end,*_ in graph['exits']:
        count+=sum(minimum-1e-7<=end-start<=maximum+1e-7 for start in entries)
    return count


def coarse_regions(energy):
    """Legacy one-second RMS can provide provisional, review-only valleys."""
    if not energy or max(energy)-min(energy)<.001:return []
    threshold=min(sorted(energy)[int((len(energy)-1)*.25)],(min(energy)+max(energy))/2)
    regions=[];start=None
    for i,value in enumerate(energy+[float('inf')]):
        if value<=threshold:
            if start is None:start=i
        elif start is not None:
            regions.append([float(start),float(i),sum(energy[start:i])/(i-start)]);start=None
    return regions


def acoustic_graph(evidence):
    """Provisional DSP boundaries, explicitly distinct from verified word gaps."""
    duration=evidence['duration'];bpm=evidence['bpm'];beats=evidence['beats']
    regions=evidence.get('acousticFeatures',{}).get('regions')
    if regions is None:regions=coarse_regions(evidence.get('energy',[]))
    # A low-confidence word is still detected vocal activity, not permission
    # to start inside it. Keep the original 0.2s uncertainty padding on every
    # detected span, including uncertain/long singing alignments.
    occupied=merged_coverage([[max(0,w[0]-.2),min(duration,w[1]+.2)] for w in evidence.get('words',[])])
    def clear(lo,hi):return not any(start<hi-1e-7 and end>lo+1e-7 for start,end in occupied)
    def entry_clear(t):
        # Combined with the occupied padding, this leaves >=0.4s after the
        # preceding detected word and >=0.35s before the next detected word.
        # Also require actual measured quiet after the point: a bare beat grid
        # cannot certify a breath or an unobserved vocal onset.
        quiet=any(lo+.05-1e-7<=t and t+.35<=hi+1e-7 for lo,hi,_ in regions)
        return quiet and clear(t-.2,t+.15)
    candidates=[]
    for lo,hi,energy in regions:
        for t in (lo+.05,(lo+hi)/2,hi-.35,hi-.05):
            if lo<=t<=hi and 0<t<duration:candidates.append((t,min(1,energy),min(1.2,max(.5,hi-lo-.1))))
    # A measured stable beat grid may align an entry inside an existing RMS
    # valley, never replace the valley/word checks. Bar exits remain provisional.
    if bpm and beats:
        bar=4*60/bpm;origin=beats[0]%bar;t=origin
        while t<duration:
            if t>0:candidates.append((t,section_energy(evidence,max(0,t-2),min(duration,t+2)),.5))
            t+=bar*2
    candidates.append((max(0,duration-.02),section_energy(evidence,max(0,duration-8),duration),.5))
    entries={0.:round(section_energy(evidence,0,min(duration,8)),4)};exits={}
    for t,energy,fade in candidates:
        time=round(t,4);fade=round(min(fade,t),4)
        if entry_clear(time):entries[time]=round(energy,4)
        if fade>=.5 and clear(time-fade,time):exits[time]=[fade,round(energy,4)]
    graph={'version':1,'quality':'acoustic','wordGuarded':False,'needsReview':True,'bpm':round(bpm,4),
           'entries':thin_points([[t,e] for t,e in sorted(entries.items())]),
           'exits':thin_points([[t,*v] for t,v in sorted(exits.items())])}
    if bpm and beats:graph['grid']=round(beats[0]%(60/bpm),6)
    return graph


def needs_rescue(evidence):
    """One bounded quality pass, never an endless retry of an uncuttable song."""
    if evidence.get('boundaryPass',0)>=1:return False
    graph=anchor_graph(evidence)
    return len(graph['entries'])<8 or len(graph['exits'])<8 or usable_anchors(graph)<8


def rescue_windows(evidence,limit=6):
    if not needs_rescue(evidence):return []
    duration=evidence['duration']
    if duration<20:return []
    # Shift the framing relative to the first 28-second pass. This gives words
    # smeared at its edges another complete local context without relaxing their
    # ASR confidence or treating missing words as a pause.
    ranked=[]
    for zone in range(12):
        center=duration*(zone+.5)/12;lo=round(max(0,min(duration-20,center-10)),3);hi=lo+20
        if any(abs(a-lo)<.25 and abs(b-hi)<.25 for a,b in evidence.get('probes',[])):continue
        local=[w for w in evidence['words'] if w[0]<hi and w[1]>lo]
        confidence=sum(bool(w[2]) if len(w)>2 else True for w in local)/max(1,len(local))
        score=section_energy(evidence,lo,hi)+.7*(1-confidence)
        ranked.append((score,lo,hi))
    chosen=[]
    for _,lo,hi in sorted(ranked,reverse=True):
        if all(abs(lo-old[0])>=8 for old in chosen):chosen.append([lo,hi])
        if len(chosen)>=limit:break
    return chosen


def replace_observed_words(previous,fresh,lo,hi):
    """Refresh a local observation without retaining an uncertain blanket span.

    Confident words crossing an observation edge remain whole. Uncertain spans
    are kept outside the freshly observed interior, so a long old low-confidence
    segment cannot mask all new, individually confident word boundaries.
    """
    begin,end=lo+1,hi-1;kept=[]
    for word in previous:
        start,stop=word[:2];confident=word[2] if len(word)>2 else True
        if stop<=begin or start>=end or confident and (start<begin or stop>end):kept.append(word)
        else:
            if start<begin:kept.append([start,begin,False])
            if stop>end:kept.append([end,stop,False])
    return kept+fresh


def selection(evidence):
    duration=evidence['duration'];bpm=evidence['bpm'];beats=evidence['beats']
    def cue(start,lo,end):
        result={'start':round(start,4),'end':round(end,4),'bpm':round(bpm,4),'fade':round(min(2,end-lo),4),
                'mix':3.2,'entryVerified':True,'wordGuarded':True,'energy':round(section_energy(evidence,start,end),4)}
        if bpm and beats:result['grid']=round(beats[0]%(60/bpm),6)
        return result
    def exits(minimum,min_fade):
        result=[(lo,hi) for lo,hi in vocal_gaps(evidence,minimum) if hi-lo>=min_fade-1e-7 and hi<=duration]
        # Prior endpoints use the same MD5 and were already validated. Legacy
        # evidence only stores broad fades, so a short new fade still requires
        # the two confident words and observed coverage above.
        for lo,hi in evidence.get('safeExits',[]):
            if 0<=lo<hi<=duration and .5<=hi-lo<=8 and (lo,hi) not in result:result.append((lo,hi))
        return result
    strict_exits=exits(.9,.5)
    regular_exits=exits(.65,.25)
    remix_exits=exits(.55,.15)
    entry_gaps=vocal_gaps(evidence,.5)
    entry_limit=duration-35 if evidence.get('analysisScope')=='full' else min(130,duration-35)
    entries=[0]+[lo for lo,hi in entry_gaps if 12<=lo<=entry_limit]
    cues=[]
    for target in (80,110,140):
        # Keep the wider, gentler exit when one fits. A shorter outgoing fade
        # is a fallback, while the incoming deck still enters over ~3 seconds.
        for available in (strict_exits,regular_exits):
            choices=[]
            for start in entries:
                for lo,end in available:
                    length=end-start
                    if not max(35,target-28)<=length<=target+28:continue
                    energy=section_energy(evidence,start,end)
                    cost=abs(length-target)/target+.6*(1-energy)+(.22 if start==0 else 0)+.12*max(0,1.2-min(2,end-lo))
                    choices.append((cost,cue(start,lo,end)))
            if choices:
                chosen=min(choices,key=lambda item:item[0])[1]
                if chosen not in cues:cues.append(chosen)
                break
    candidates={}
    for tier,available in enumerate((strict_exits,remix_exits)):
        for start in [0]+[lo for lo,hi in entry_gaps]:
            for lo,end in available:
                if 8<=end-start<=24:
                    c=cue(start,lo,end)
                    cost=abs(end-start-14)/20+.8*(1-c['energy'])+(.15 if start==0 else 0)+.12*max(0,1.2-c['fade'])
                    key=(c['start'],c['end'])
                    rank=(tier,cost)
                    if key not in candidates or rank<candidates[key][0]:candidates[key]=(rank,c)
    # Keep a reusable pool across the whole song, not just three chosen cuts.
    # Each boundary is an actual guarded word gap. At each start, different
    # endings/lengths are useful choices for a later transition.
    zones={}
    for rank,c in sorted(candidates.values(),key=lambda item:item[0]):
        time_zone=min(7,int(8*c['start']/max(1,duration)))
        length=c['end']-c['start'];length_band=0 if length<12 else 1 if length<18 else 2
        zones.setdefault((time_zone,length_band),[]).append((rank,c))
    remix=[]
    while zones and len(remix)<32:
        # One candidate per time/length zone per pass prevents a loud chorus or
        # one fixed duration from occupying every slot, while the best-ranked
        # available section leads.
        for zone in sorted(list(zones),key=lambda z:zones[z][0][0]):
            pool=zones[zone]
            while pool:
                _,candidate=pool.pop(0)
                if all(abs(candidate['start']-old['start'])>=2 or abs(candidate['end']-old['end'])>=2 for old in remix):
                    remix.append(candidate);break
            if not pool:del zones[zone]
            if len(remix)>=32:break
    graph=anchor_graph(evidence)
    reason=None
    if not cues and not remix and not usable_anchors(graph):
        reason='few-recognised-words' if len(evidence['words'])<8 else 'no-safe-gap' if not graph['exits'] else 'no-safe-duration'
    fallback=acoustic_graph(evidence)
    review=not usable_anchors(graph)
    return {'version':3,'cueSchema':4,'anchors':graph,'acousticAnchors':fallback,'needsReview':review,
            'reviewRecommended':review or bool(evidence.get('rescuePlan')),'fullFallback':review and not usable_anchors(fallback),
            'cues':cues,'remix':remix,'method':'analysed','reason':reason}


def rhythm(y,sr):
    """Beat tracking with a 16-beat estimate, avoiding single-hop BPM quantisation."""
    import numpy as np
    import librosa
    hop=256
    onset=librosa.onset.onset_strength(y=y,sr=sr,hop_length=hop)
    _,frames=librosa.beat.beat_track(onset_envelope=onset,sr=sr,hop_length=hop,trim=False)
    beats=librosa.frames_to_time(frames,sr=sr,hop_length=hop)
    if len(beats)<64:return [],0
    spans=beats[16:]-beats[:-16];span=float(np.median(spans));bpm=60*16/span
    if not 60<=bpm<=210 or float(np.median(np.abs(spans-span)))>.20:return [],0
    return [float(t) for t in beats],float(bpm)


def numeric_words(part,offset):
    # faster-whisper alignment exposes NumPy probabilities. Convert every value
    # before saving evidence; numpy.bool_ is not JSON serializable.
    confident=bool(part.no_speech_prob<=.8 and part.avg_logprob>=-.85)
    if not part.words:return [(float(offset+part.start),float(offset+part.end),False)]
    return [(float(offset+w.start),float(offset+w.end),bool(confident and w.probability>=.45)) for w in part.words if w.end>w.start]


def energy_envelope(y,sr):
    import numpy as np
    values=[float(np.sqrt(np.mean(y[t:t+sr]**2))) for t in range(0,len(y),sr)]
    scale=float(np.quantile(values,.9)) if values else 1
    return [round(min(1,x/max(.0001,scale)),4) for x in values]


def acoustic_features(y,sr):
    """Twenty-millisecond RMS valleys in one-second bounded NumPy chunks."""
    import numpy as np
    hop=max(1,int(sr*.02));count=math.ceil(len(y)/hop)
    levels=np.empty(count,dtype=np.float32)
    for start in range(0,len(y),sr):
        block=np.array(y[start:min(len(y),start+sr)],dtype=np.float32,copy=True)
        whole=len(block)//hop;at=start//hop
        if whole:levels[at:at+whole]=np.sqrt(np.mean(block[:whole*hop].reshape(whole,hop)**2,axis=1))
        if whole*hop<len(block):levels[at+whole]=float(np.sqrt(np.mean(block[whole*hop:]**2)))
    if not count:return {'hop':.02,'regions':[]}
    smooth=np.convolve(levels,np.ones(5,dtype=np.float32)/5,mode='same') if count>=5 else levels
    if count>=5:
        # Convolution's zero padding is not measured silence at the file edges.
        smooth[:2]=levels[:2];smooth[-2:]=levels[-2:]
    scale=max(.0001,float(np.quantile(smooth,.9)));normalized=np.minimum(1,smooth/scale)
    if float(np.max(normalized)-np.min(normalized))<.001:return {'hop':.02,'regions':[]}
    threshold=min(float(np.quantile(normalized,.25)),float(np.min(normalized)+np.max(normalized))/2);regions=[];begin=None
    for i in range(count+1):
        low=i<count and normalized[i]<=threshold
        if low and begin is None:begin=i
        elif not low and begin is not None:
            if i-begin>=2:
                regions.append([round(begin*hop/sr,4),round(min(len(y),i*hop)/sr,4),round(float(np.mean(normalized[begin:i])),4)])
            begin=None
    return {'hop':.02,'regions':thin_points(regions,512)}


def probe_windows(evidence):
    """Two small endpoint observations first, a third only if still needed."""
    duration=evidence['duration'];energy=evidence.get('energy',[])
    if energy:
        # Seek a strong sustained section, not the loudest individual sample.
        starts=range(18,max(19,int(min(100,duration-100))),4)
        anchor=max(starts,key=lambda t:section_energy(evidence,t,t+24))
    else:anchor=min(60,max(18,duration/4))
    # A late probe can rescue continuous-vocal tracks whose old 45–180s pass
    # found no exit. Selection reuses all earlier observations too.
    centers=[anchor+8,min(duration-14,anchor+112),min(duration-14,anchor+48)]
    if evidence.get('version')==2:
        exits=vocal_gaps(evidence)+[tuple(x) for x in evidence.get('safeExits',[])]
        # Long windows can smear sung words across a rest. Re-observe a small
        # local phrase immediately before a known exit, instead of merely
        # recycling the same uncertain timestamps forever.
        near_exit=max(exits,key=lambda g:g[1]-g[0])[1]-9 if exits else anchor+8
        centers=[near_exit,min(duration-14,200),anchor+8]
    windows=[]
    for center in centers:
        lo=max(0,round(center-14));hi=min(duration,lo+28)
        if hi-lo<12:continue
        if any(abs(a-lo)<2 and abs(b-hi)<2 for a,b in evidence.get('probes',[])):continue
        if evidence.get('version')!=2 and any(a<=lo and hi<=b for a,b in coverage(evidence)):continue
        if not any(abs(lo-old[0])<8 for old in windows):windows.append((lo,hi))
    return windows[:3]


def full_windows(evidence):
    """Observe the entire original in bounded windows; resume completed work.

    Attempted probes include uncertain/empty ASR. Trusted coverage includes only
    observations with enough words. Neither is an instruction to invent a cue.
    """
    duration=float(evidence['duration'])
    if not math.isfinite(duration) or not 0<duration<=MAX_FULL_DURATION+.1:raise ValueError('Invalid full duration')
    all_windows=[];lo=0.
    while lo+28<duration:
        all_windows.append((lo,lo+28));lo+=24
    last=(max(0.,duration-28),duration)
    if not all_windows or last!=all_windows[-1]:all_windows.append(last)
    observed=merged_coverage(coverage(evidence));probes=evidence.get('probes',[])
    return [(lo,hi) for lo,hi in all_windows
            if not any(abs(a-lo)<.25 and abs(b-hi)<.25 for a,b in probes)
            and not any(a<=lo+.001 and hi<=b+.001 for a,b in observed)]


def observe_windows(evidence,windows,model,sample,language=None,checkpoint=None,full=False):
    """Only numeric timings survive; save progress after each small observation."""
    from .config import atomic_json
    asr_start=time.monotonic();analysed=0
    for lo,hi in windows:
        segments,_=model.transcribe(sample(lo,hi),language=language,beam_size=3,temperature=0.0,word_timestamps=True,condition_on_previous_text=False,vad_filter=False)
        words=[]
        for part in segments:words.extend(numeric_words(part,lo))
        # Empty ASR cannot erase previous evidence or be treated as a pause.
        if len(words)>=8:
            evidence['words']=replace_observed_words(evidence['words'],words,lo,hi)
            evidence['coverage']=[*coverage(evidence),[lo,hi]]
        evidence.setdefault('probes',[]).append([lo,hi]);analysed+=hi-lo
        evidence['timings'].update(asr=round(time.monotonic()-asr_start,3),observedSeconds=analysed)
        if checkpoint:atomic_json(checkpoint,evidence)
        if not full:
            chosen=selection(evidence)
            if chosen['cues'] and len(chosen['remix'])>=2:break


def analyse(file,ffmpeg,models,language=None,cached=None,checkpoint=None,full=False,cpu_threads=2):
    if type(cpu_threads) is not int or not 1<=cpu_threads<=8:raise ValueError('Threads must be between 1 and 8')
    from .config import atomic_json
    # Keep the parent small until the separate rhythm process has exited: an
    # eager NumPy import here otherwise duplicates its libraries and workspace
    # in the Mac watchdog's process-tree RSS budget during the heavy beat pass.
    decode_limit=MAX_FULL_DURATION if full else 300
    if not 0<file.stat().st_size<=40*1024**2:raise RuntimeError('SOURCE_TOO_LARGE_OR_EMPTY')
    raw=file.with_suffix('.f32');began=time.monotonic();y=None
    evidence=dict(cached or {})
    if full:
        evidence['analysisScope']='full';evidence['fullComplete']=False
    def stage(value):
        evidence['analysisStage']=value
        if checkpoint:atomic_json(checkpoint,evidence)
    try:
        stage('decode')
        result=subprocess.run([ffmpeg,'-nostdin','-v','error','-threads','1','-i',str(file),'-t',str(decode_limit+.1 if full else decode_limit),'-vn','-ac','1','-ar','16000','-f','f32le','-y',str(raw)],capture_output=True,timeout=60)
        if result.returncode:raise RuntimeError('DECODE_FAILED')
        size=raw.stat().st_size
        if not 0<size<=16000*decode_limit*4+4096:raise RuntimeError('DECODE_TOO_LARGE')
        duration=size/(4*16000);timing=raw.with_suffix('.rhythm.json')
        observed=coverage(evidence) if cached else []
        evidence['duration']=duration;evidence['coverage']=observed;evidence.setdefault('words',[])
        reuse_rhythm=bool(cached and 'beats' in evidence and 'bpm' in evidence)
        stage('rhythm')
        if not reuse_rhythm:
            # Only this child imports librosa/Numba; the parent hasn't imported
            # NumPy or opened the PCM yet. Its beat input is capped at 90s.
            try:subprocess.run([os.sys.executable,'-m','aidj.dj_worker','rhythm','--file',str(raw),'--output',str(timing),'--ffmpeg',ffmpeg],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
            except subprocess.CalledProcessError as exc:
                if exc.returncode in (-9,137):raise MemoryError('RHYTHM_RESOURCE_LIMIT') from None
                raise
            structure=json.loads(timing.read_text());timing.unlink();evidence.update(structure)
        import numpy as np
        y=np.memmap(raw,dtype=np.float32,mode='r')
        if reuse_rhythm and (not evidence.get('energy') or full and len(evidence['energy'])<math.ceil(duration)):
            evidence['energy']=energy_envelope(y,16000)
        if full and 'acousticFeatures' not in evidence:evidence['acousticFeatures']=acoustic_features(y,16000)
        evidence['timings']={'structure':round(time.monotonic()-began,3)}
        windows=full_windows(evidence) if full else probe_windows(evidence)
        # Local controllers can select an interrupted checkpoint only once its
        # schema version is current. Explicit coverage above preserves v2's
        # narrower observations before changing that version.
        evidence['version']=3
        stage('asr')
        model=None
        def speech_model():
            nonlocal model
            if model is None:
                from faster_whisper import WhisperModel
                model=WhisperModel('base',device='cpu',compute_type='int8',cpu_threads=cpu_threads,num_workers=1,download_root=str(models))
            return model
        sample=lambda lo,hi:np.array(y[int(lo*16000):int(hi*16000)],dtype=np.float32,copy=True)
        if windows:observe_windows(evidence,windows,speech_model(),sample,language,checkpoint,full)
        if full and evidence.get('boundaryPass',0)<1:
            plan=evidence.get('rescuePlan')
            if plan is None:
                plan=rescue_windows(evidence);evidence['rescuePlan']=plan;stage('asr')
            for lo,hi in plan:
                if any(abs(a-lo)<.25 and abs(b-hi)<.25 for a,b in evidence.get('probes',[])):continue
                observe_windows(evidence,[(lo,hi)],speech_model(),sample,language,checkpoint,True)
        model=None
        evidence['version']=3
        stage('selection')
        chosen=selection(evidence)
        if full:
            evidence['fullComplete']=not full_windows(evidence);evidence['fullDuration']=duration
            if evidence['fullComplete']:evidence['boundaryPass']=1
        evidence['timings']['total']=round(time.monotonic()-began,3)
        stage('complete')
        return {**chosen,'evidence':evidence}
    finally:
        if y is not None:y._mmap.close()
        raw.unlink(missing_ok=True);raw.with_suffix('.rhythm.json').unlink(missing_ok=True)


def process(settings,db,row,ffmpeg):
    from .disk import Disk
    asset=db.execute('SELECT * FROM web_assets WHERE md5=? ORDER BY id LIMIT 1',(row['md5'],)).fetchone()
    if not asset or asset['bytes']>settings.max_bytes or not 30<=asset['duration']<=600:
        complete(db,row['md5'],{'cues':[]},max(0,asset['duration'] if asset else 0));return
    if asset['duration']<35:
        complete(db,row['md5'],{'cues':[],'reason':'too-short'},asset['duration']);return
    cached=db.execute('SELECT payload FROM dj_evidence WHERE md5=?',(row['md5'],)).fetchone()
    evidence=json.loads(cached[0]) if cached else {'version':2,'words':[],'duration':asset['duration'],'coverage':[]}
    previous=json.loads(row['payload']) if row['payload'] else {}
    evidence['safeExits']=[[c['end']-c['fade'],c['end']] for c in previous.get('cues',[])+previous.get('remix',[]) if .5<=c.get('fade',0)<=8]
    if cached:
        chosen=selection(evidence)
        if evidence.get('version',0)>=3 or chosen['cues'] and len(chosen['remix'])>=2:
            complete(db,row['md5'],chosen,asset['duration']);return
    if shutil.disk_usage(settings.state).free<768*1024**2:raise RuntimeError('LOW_DISK')
    with tempfile.TemporaryDirectory(prefix='dj-',dir=settings.state/'work') as scratch:
        file=Path(scratch)/('source'+Path(asset['remote_path']).suffix)
        Disk(settings).download(asset['remote_path'],file,expected_md5=row['md5'])
        # The child exits after each song, bounding native/JIT/model allocator growth.
        output=Path(scratch)/'cues.json';checkpoint=Path(scratch)/'evidence.json'
        cmd=[os.sys.executable,'-m','aidj.dj_worker','analyse','--file',str(file),'--output',str(output),'--ffmpeg',ffmpeg,'--checkpoint',str(checkpoint)]
        if cached or evidence['safeExits']:
            seed=Path(scratch)/'cached.json';seed.write_text(json.dumps(evidence));cmd+=['--cached',str(seed)]
        import re
        if re.search('[А-Яа-яЁё]',asset['artist']+' '+asset['title']):cmd+=['--language','ru']
        child=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            deadline=time.monotonic()+240
            while child.poll() is None:
                heartbeat(db,'working');remaining=deadline-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired(cmd,240)
                try:child.wait(timeout=min(10,remaining))
                except subprocess.TimeoutExpired:continue
            code=child.returncode
            if code in (-9,137):raise MemoryError('ANALYSIS_RESOURCE_LIMIT')
            if code!=0:raise RuntimeError('ANALYSIS_FAILED')
        except BaseException:
            if child.poll() is None:os.killpg(child.pid,signal.SIGKILL);child.wait()
            if checkpoint.exists() and checkpoint.stat().st_size<=256*1024:
                partial=json.loads(checkpoint.read_text());db.execute('INSERT OR REPLACE INTO dj_evidence VALUES (?,?,?)',(row['md5'],json.dumps(partial),time.time()));db.commit()
            raise
        if output.stat().st_size>256*1024:raise RuntimeError('METADATA_TOO_LARGE')
        value=json.loads(output.read_text());evidence=value.pop('evidence',None)
        if evidence:
            db.execute('INSERT OR REPLACE INTO dj_evidence VALUES (?,?,?)',(row['md5'],json.dumps(evidence),time.time()));db.commit()
        complete(db,row['md5'],value,asset['duration'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['serve','once','analyse','rhythm']);parser.add_argument('--file',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--ffmpeg',required=True);parser.add_argument('--language');parser.add_argument('--cached',type=Path);parser.add_argument('--checkpoint',type=Path);parser.add_argument('--full',action='store_true');parser.add_argument('--threads',type=int,choices=range(1,9),default=2);args=parser.parse_args()
    settings=Settings();settings.prepare()
    def terminate(signum,frame):raise SystemExit(0)
    signal.signal(signal.SIGTERM,terminate)
    if args.action=='rhythm':
        import numpy as np
        from .config import atomic_json
        if not 0<args.file.stat().st_size<=16000*MAX_FULL_DURATION*4+4096:raise RuntimeError('DECODE_TOO_LARGE')
        y=np.memmap(args.file,dtype=np.float32,mode='r')
        try:
            beats,bpm=rhythm(np.array(y[:16000*90],copy=True),16000)
            atomic_json(args.output,{'beats':beats,'bpm':bpm,'energy':energy_envelope(y,16000),'acousticFeatures':acoustic_features(y,16000)})
        finally:y._mmap.close()
        return
    if args.action=='analyse':
        from .config import atomic_json
        atomic_json(args.output,analyse(args.file,args.ffmpeg,settings.state/'dj-models',args.language,json.loads(args.cached.read_text()) if args.cached else None,args.checkpoint,args.full,args.threads));return
    with (settings.state/'dj-worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);db=connect(settings)
        db.execute("UPDATE dj_analysis SET status='pending' WHERE status='working'");db.commit();upgrade_queue(db);idle_heartbeat=0
        try:
            while True:
                row=next_job(db)
                if row:
                    heartbeat(db,'working')
                    claimed=db.execute("UPDATE dj_analysis SET status='working',updated=? WHERE md5=? AND status='pending' AND NOT EXISTS (SELECT 1 FROM dj_batch_jobs b WHERE b.md5=dj_analysis.md5 AND b.state='running' AND b.lease>?)",(time.time(),row['md5'],time.time())).rowcount;db.commit()
                    if not claimed:continue
                    try:
                        process(settings,db,row,args.ffmpeg)
                        print('DJ_PREPARED',flush=True)
                    except MemoryError:
                        db.execute("UPDATE dj_analysis SET status='failed',updated=? WHERE md5=?",(time.time(),row['md5']));db.commit()
                        raise
                    except Exception as exc:
                        # No raw exceptions: network errors may contain signed URLs.
                        db.execute("UPDATE dj_analysis SET status='failed',updated=? WHERE md5=?",(time.time(),row['md5']));db.commit()
                        print('DJ_PREPARATION_FAILED',type(exc).__name__,flush=True)
                    if args.action=='once':break
                elif args.action=='once':break
                else:
                    if time.monotonic()-idle_heartbeat>20:heartbeat(db,'idle');idle_heartbeat=time.monotonic()
                    time.sleep(2)
        finally:
            db.rollback();db.execute("UPDATE dj_analysis SET status='pending' WHERE status='working'");heartbeat(db,'stopped')


if __name__=='__main__':
    try:main()
    except MemoryError:
        print('DJ_RESOURCE_LIMIT_STOPPED',flush=True);raise SystemExit(137)
