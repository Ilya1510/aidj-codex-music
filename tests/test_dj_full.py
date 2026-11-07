"""Full-song scan scheduling and resumable numeric evidence, with a fake ASR."""
import json
from types import SimpleNamespace as N

import pytest

from aidj.dj_worker import full_windows,merged_coverage,observe_windows,selection


def evidence(duration=100):
    return {'version':3,'duration':duration,'words':[],'coverage':[],'beats':[],'bpm':120,
            'energy':[.7]*int(duration),'timings':{},'analysisScope':'full','fullComplete':False}


@pytest.mark.parametrize('duration',[12,28,30,50,100,240,599.2,600])
def test_full_schedule_covers_every_second_in_bounded_overlapping_observations(duration):
    windows=full_windows(evidence(duration))
    assert windows[0][0]==0 and windows[-1][1]==duration
    assert all(0<=lo<hi<=duration and hi-lo<=28 for lo,hi in windows)
    assert merged_coverage(windows)==[[0.,duration]]
    assert len(windows)<=26


def test_full_resume_unions_coverage_without_treating_unobserved_holes_as_done():
    base=evidence(100)
    base['coverage']=[[0,24],[24,52],[80,100]]
    scheduled=full_windows(base)
    assert (0,28) not in scheduled and (24,52) not in scheduled
    assert (48,76) in scheduled and (72,100) in scheduled
    # An empty ASR window was attempted, so a restart doesn't repeatedly spend
    # CPU on it; the same hole still is not trusted coverage for choosing cues.
    base['probes']=[[48,76]]
    assert (48,76) not in full_windows(base)
    assert base['coverage']==[[0,24],[24,52],[80,100]]


class Model:
    def __init__(self,stop=None,empty=False):self.calls=[];self.stop=stop;self.empty=empty
    def transcribe(self,sample,**kwargs):
        if self.stop is not None and len(self.calls)>=self.stop:raise InterruptedError('test stop')
        self.calls.append((sample,kwargs))
        # A short independent phrase with numeric whole-word boundaries.
        words=[] if self.empty else [N(start=i*2,end=i*2+1,probability=.9,word='never saved') for i in range(10)]
        segment=N(start=0,end=20,no_speech_prob=.1,avg_logprob=-.3,words=words)
        return iter([segment]),None


def test_full_asr_does_not_stop_after_first_success_and_saves_only_numeric_data(tmp_path,monkeypatch):
    base=evidence();model=Model();file=tmp_path/'progress.json';windows=full_windows(base)
    # The targeted VM path deliberately exits early when enough cuts are ready.
    # A full pass must keep observing the rest of the original regardless.
    monkeypatch.setattr('aidj.dj_worker.selection',lambda data:{'cues':[{}],'remix':[{},{}]})
    observe_windows(base,windows,model,lambda lo,hi:(lo,hi),checkpoint=file,full=True)
    assert len(model.calls)==len(windows)
    assert all(call[1]['temperature']==0.0 and call[1]['beam_size']==3 for call in model.calls)
    saved=json.loads(file.read_text())
    assert len(saved['probes'])==len(windows) and not full_windows(saved)
    assert 'never saved' not in file.read_text()
    assert all(len(word)==3 and type(word[2]) is bool for word in saved['words'])
    assert saved['fullComplete'] is False  # Final completion belongs to analyse(), after success.
    limited=evidence();limited_model=Model()
    observe_windows(limited,windows,limited_model,lambda lo,hi:(lo,hi),full=False)
    assert len(limited_model.calls)==1


def test_interrupted_full_asr_can_resume_from_last_completed_window(tmp_path):
    base=evidence();file=tmp_path/'progress.json';model=Model(stop=2)
    with pytest.raises(InterruptedError):
        observe_windows(base,full_windows(base),model,lambda lo,hi:(lo,hi),checkpoint=file,full=True)
    saved=json.loads(file.read_text())
    assert len(saved['probes'])==2 and saved['fullComplete'] is False
    rest=full_windows(saved);assert rest and all(window not in [call[0] for call in model.calls] for window in rest)
    resumed=Model();observe_windows(saved,rest,resumed,lambda lo,hi:(lo,hi),checkpoint=file,full=True)
    assert not full_windows(saved) and len(saved['probes'])==len(full_windows(evidence()))


def test_empty_full_asr_is_attempted_but_never_becomes_evidence_of_silence(tmp_path):
    base=evidence();model=Model(empty=True)
    observe_windows(base,full_windows(base),model,lambda lo,hi:None,checkpoint=tmp_path/'progress.json',full=True)
    assert not full_windows(base)
    assert not base['coverage'] and all(not word[2] for word in base['words'])
    assert selection(base)['cues']==[] and selection(base)['remix']==[]


def test_full_selection_can_choose_an_active_section_later_than_old_prefix_limit():
    base=evidence(360)
    base['coverage']=[[0,360]]
    base['words']=[(s,s+2,True) for s in range(0,360,4)]
    base['energy']=[.05]*190+[1]*160+[.05]*10
    value=selection(base)
    assert value['cues'] and any(c['start']>180 for c in value['cues'])
    assert 6<len(value['remix'])<=32
    assert len({int(8*c['start']/360) for c in value['remix']})>=4
    for index,cue in enumerate(value['remix']):
        assert cue['wordGuarded'] and 8<=cue['end']-cue['start']<=24
        for other in value['remix'][:index]:
            assert abs(cue['start']-other['start'])>=2 or abs(cue['end']-other['end'])>=2


@pytest.mark.parametrize('duration',[0,-1,601,float('nan'),float('inf')])
def test_full_scan_rejects_unbounded_input(duration):
    with pytest.raises(ValueError):full_windows(evidence(duration) if 0<=duration<1000 else {'duration':duration})


def test_analyse_full_finalizes_only_after_pass_and_passes_worker_thread_budget(tmp_path,monkeypatch):
    import sys
    from aidj.dj_worker import analyse
    duration=32;source=tmp_path/'original.mp3';source.write_bytes(b'original');checkpoint=tmp_path/'checkpoint.json'
    arrays=[];constructors=[];calls=[]
    class Samples:
        def __init__(self,path,**kwargs):
            self.length=path.stat().st_size//4;self.closed=False;self._mmap=self;arrays.append(self)
        def __len__(self):return self.length
        def __getitem__(self,slice):return (slice.start,slice.stop)
        def close(self):self.closed=True
    def command(args,**kwargs):
        calls.append(args)
        assert '-t' in args and args[args.index('-t')+1]=='600.1'
        with open(args[-1],'wb') as target:target.truncate(16000*duration*4)
        return N(returncode=0)
    def whisper(*args,**kwargs):constructors.append(kwargs);return Model()
    monkeypatch.setitem(sys.modules,'numpy',N(float32=object(),memmap=Samples,array=lambda value,**kwargs:value))
    monkeypatch.setitem(sys.modules,'faster_whisper',N(WhisperModel=whisper))
    monkeypatch.setattr('aidj.dj_worker.subprocess.run',command)
    base=evidence(duration)
    result=analyse(source,'ffmpeg',tmp_path/'models',cached=base,checkpoint=checkpoint,full=True,cpu_threads=4)
    saved=json.loads(checkpoint.read_text())
    assert result['evidence']['fullComplete'] is True and saved['fullComplete'] is True
    assert saved['fullDuration']==duration and saved['analysisScope']=='full'
    assert len(saved['probes'])==2 and constructors[0]['cpu_threads']==4
    assert len(calls)==1 and arrays[0].closed and not source.with_suffix('.f32').exists()
    assert source.read_bytes()==b'original'
    # A previously completed full pass avoids loading the model at all.
    constructors.clear();result=analyse(source,'ffmpeg',tmp_path/'models',cached=saved,full=True,cpu_threads=2)
    assert result['evidence']['fullComplete'] and not constructors


@pytest.mark.parametrize('threads',[0,9,-1,True,2.5])
def test_analysis_rejects_unbounded_thread_requests_before_loading_libraries(tmp_path,threads):
    from aidj.dj_worker import analyse
    with pytest.raises(ValueError):analyse(tmp_path/'file','ffmpeg',tmp_path/'models',cpu_threads=threads)


def test_full_source_size_is_bounded_before_decoding(tmp_path,monkeypatch):
    import sys
    from aidj.dj_worker import analyse
    source=tmp_path/'too-large.mp3'
    with source.open('wb') as stream:stream.truncate(40*1024**2+1)
    monkeypatch.setitem(sys.modules,'numpy',N())
    with pytest.raises(RuntimeError,match='SOURCE_TOO_LARGE_OR_EMPTY'):
        analyse(source,'ffmpeg',tmp_path/'models',full=True)
