"""Preview command/timeline tests with tiny fake WAVs; never invoke FFmpeg."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import wave

import pytest
from aidj import fixed_set_preview as preview


def inputs():
    item={'trackId':'a','md5':'a'*32,'start':1.,'end':10.,'fadeIn':0.,'fadeOut':0.,
          'rate':1.,'transitionSeconds':0.,'transitionKind':'clean-cut'}
    song={'md5':'a'*32,'duration':10.,'range_start':None,'range_end':None,
          'integrated_lufs':-10.,'true_peak_dbtp':-1.,
          'loudness_method':'ebu-r128-v1','loudness_status':'complete','artist':'QA','title':'A'}
    return item,song


def test_completion_omits_duration_and_zero_fades_preserving_both_channels_and_level():
    item,song=inputs();args,completion=preview.clip_arguments(item,song,'original.mp3','clip.wav')
    assert completion and '-t' not in args and '-to' not in args
    assert args[args.index('-ss')+1]==1.
    filters=args[args.index('-af')+1]
    assert filters=='volume=-6.00000dB,volume=0.7' and 'afade' not in filters
    assert args[args.index('-ac')+1]=='2' and 'pan=' not in filters
    assert args[args.index('-ar')+1]=='22050'


def test_manual_crops_internal_cuts_and_invalid_plans_cannot_gain_source_completion():
    item,song=inputs()
    args,completion=preview.clip_arguments(item,{**song,'range_end':9.9995},'a','b')
    assert not completion and args[args.index('-t')+1]==9.
    args,completion=preview.clip_arguments({**item,'end':9.5,'fadeIn':.1,'fadeOut':.5},song,'a','b')
    assert not completion and args[args.index('-t')+1]==8.5
    assert 'afade=t=in:st=0:d=0.10000,afade=t=out:st=8.00000:d=0.50000' in args[args.index('-af')+1]
    for patch in ({'md5':'changed'},{'rate':1.01},{'transitionSeconds':1},{'end':10.01},
                  {'transitionKind':'crossfade'},{'fadeOut':float('nan')}):
        with pytest.raises(RuntimeError):preview.clip_arguments({**item,**patch},song,'a','b')


def test_positive_source_end_fade_uses_actual_pcm_duration_without_double_gain():
    item,song=inputs();item['fadeOut']=4.
    args,completion=preview.clip_arguments(item,song,'a','b')
    assert completion and '-t' not in args and 'afade=t=out' not in args[args.index('-af')+1]
    frames=round(9.052*22050)
    tail=preview.ending_fade_arguments('b','c',frames,4.)
    assert '-t' not in tail and 'volume' not in tail[tail.index('-af')+1]
    assert f'st={frames/22050-4:.8f}' in tail[tail.index('-af')+1]
    unmeasured=dict(song)
    unmeasured['loudness_status']='failed'
    args,_=preview.clip_arguments(item,unmeasured,'a','b')
    assert args[args.index('-af')+1].startswith('volume=0.00000dB,volume=0.7')


def write_wav(path,frames):
    with wave.open(str(path),'wb') as audio:
        audio.setparams((2,2,22050,0,'NONE','not compressed'))
        chunk=b'\0'*4096
        left=frames*4
        while left:
            take=min(left,len(chunk));audio.writeframesraw(chunk[:take]);left-=take


def test_render_uses_actual_sample_counts_for_each_track_and_seam_not_metadata(tmp_path,monkeypatch):
    item,song=inputs();second={**item,'trackId':'b','md5':'b'*32,'start':2.,'end':12.,'fadeIn':.1,'fadeOut':.5}
    plan={'revision':'qa','tracks':[item,second]};(tmp_path/'plan.json').write_text(json.dumps(plan))
    db=sqlite3.connect(tmp_path/'bot.sqlite')
    db.executescript('''CREATE TABLE web_assets(id TEXT,md5 TEXT,duration REAL,remote_path TEXT,artist TEXT,title TEXT);
        CREATE TABLE playback_ranges(track_id TEXT,start REAL,end REAL);
        CREATE TABLE audio_loudness(md5 TEXT,integrated_lufs REAL,true_peak_dbtp REAL,method TEXT,status TEXT);''')
    db.executemany('INSERT INTO web_assets VALUES (?,?,?,?,?,?)',[
        ('a','a'*32,10.,'/a.mp3','QA','A'),('b','b'*32,20.,'/b.mp3','QA','B')])
    db.execute('INSERT INTO playback_ranges VALUES (?,?,?)',('b',0.,16.));db.commit();db.close()
    class FakeDisk:
        def __init__(self,settings):pass
        def download(self,remote,destination,expected_md5):Path(destination).write_bytes(b'fake')
    import aidj.disk
    monkeypatch.setattr(aidj.disk,'Disk',FakeDisk)
    calls=[];a_frames=round(9.052*22050);b_frames=10*22050-1
    def fake_run(args,**kwargs):
        calls.append(list(args));output=Path(args[-1])
        if output.name=='00.wav':write_wav(output,a_frames)
        elif output.name=='01.wav':write_wav(output,b_frames)
        elif output.name=='tail0.wav':write_wav(output,6*22050-1)
        elif output.name=='head0.wav':write_wav(output,6*22050)
        else:output.write_bytes(b'fake encoded output')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(preview.subprocess,'run',fake_run)
    preview.render(SimpleNamespace(state=tmp_path),tmp_path,'fake-decoder',count=2)
    result=json.loads((tmp_path/'preview-timeline.json').read_text());a,b=result['tracks']
    assert a['end']==a_frames/22050 and a['end']!=9.
    assert b['start']==a['end'] and b['end']==(a_frames+b_frames)/22050
    assert a['sourceCompletion'] and not b['sourceCompletion']
    assert a['renderedFrames']==a_frames and b['renderedFrames']==b_frames
    seam=result['seams'][0]
    assert seam['boundary']==(6*22050-1)/22050
    assert seam['end']==(12*22050-1)/22050
    original_calls=[c for c in calls if any(str(x).endswith('source.mp3') for x in c)]
    assert len(original_calls)==2 and '-t' not in original_calls[0] and '-t' in original_calls[1]
