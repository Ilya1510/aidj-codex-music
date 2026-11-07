"""Verify a tempo-adjusted preview against real FFmpeg, with synthetic audio."""
import json
import math
from pathlib import Path
import struct
import wave

import mutagen
import pytest

from aidj.audio import ffmpeg
from aidj.config import atomic_json
from aidj.dj_preview import render


@pytest.fixture
def preview(tmp_path):
    (tmp_path/'sources').mkdir();(tmp_path/'analysis').mkdir()
    songs=[];items=[]
    for index,target in enumerate([123,123,118]):
        digest=str(index+1)*32;relative=Path('sources')/(digest+'.wav')
        with wave.open(str(tmp_path/relative),'wb') as audio:
            audio.setparams((1,2,8000,0,'NONE','not compressed'))
            # Stream a small synthetic input a second at a time.
            frame=b''.join(struct.pack('<h',int(8000*math.sin(2*math.pi*(220+index*55)*t/8000))) for t in range(8000))
            for _ in range(20):audio.writeframes(frame)
        songs.append({'md5':digest,'label':'Synthetic '+str(index),'file':str(relative),'allowedStart':0,'allowedEnd':20})
        atomic_json(tmp_path/'analysis'/(digest+'.json'),{'bpm':120,'segments':[{'start':1,'end':17,'bars':8}]})
        items.append({'md5':digest,'targetBpm':target})
    atomic_json(tmp_path/'sources.json',songs);atomic_json(tmp_path/'plan.json',{'tracks':items});return tmp_path


def test_preview_preserves_bar_length_after_slowing_and_records_real_overlaps(preview):
    render(preview,ffmpeg());timeline=json.loads((preview/'timeline.json').read_text());items=timeline['tracks']
    for row,target in zip(items,[123,123,118]):
        assert abs((row['end']-row['start'])-32*60/target)<.002
    assert items[1]['transition']['kind']=='beat-overlap'
    assert abs(items[0]['end']-items[1]['start']-8*60/123)<.002
    assert items[2]['transition']['kind']=='short-change'
    assert abs(items[1]['end']-items[2]['start']-.8)<.002
    actual=mutagen.File(preview/'Volleyball-DJ-preview-01.mp3').info.length
    assert abs(actual-items[-1]['end'])<.1
    assert 25<mutagen.File(preview/'Volleyball-DJ-transitions-01.mp3').info.length<27
    assert 'сведение в ритм' in (preview/'tracklist.txt').read_text()


@pytest.mark.parametrize('change',['manual-range','excessive-rate'])
def test_preview_does_not_override_manual_ranges_or_excessively_change_tempo(preview,change):
    if change=='manual-range':
        songs=json.loads((preview/'sources.json').read_text());songs[0]['allowedStart']=2;atomic_json(preview/'sources.json',songs)
    else:
        plan=json.loads((preview/'plan.json').read_text());plan['tracks'][0]['targetBpm']=140;atomic_json(preview/'plan.json',plan)
    with pytest.raises(RuntimeError):render(preview,ffmpeg())
    assert not (preview/'Volleyball-DJ-preview-01.mp3').exists()


def test_listening_correction_keeps_the_vocal_tail_instead_of_truncating_to_old_bars(preview):
    plan=json.loads((preview/'plan.json').read_text());item=plan['tracks'][0]
    item.update(sourceStart=1,sourceEnd=18,sourceBpm=120)
    plan['tracks'][1]['transitionBeats']=4;atomic_json(preview/'plan.json',plan)
    render(preview,ffmpeg());result=json.loads((preview/'timeline.json').read_text())['tracks']
    assert result[0]['sourceEnd']==18
    assert abs(result[0]['end']-17/(123/120))<.002
    assert abs(result[1]['transition']['seconds']-4*60/123)<.002
