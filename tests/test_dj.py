import json
import pytest
from aidj.dj import enqueue,complete,validate
from aidj.dj_worker import vocal_cues
from aidj.web_catalog import catalog
from aidj.device_inbox import library_revision
from aidj.backup import snapshot
from tests.test_playlists import env


def test_dj_metadata_is_content_addressed_small_and_does_not_mutate_original_or_manual_range(env,tmp_path):
    settings,db,disk,*rest=env
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',('a'*24,'youtube:abcdefghijk','Song','Artist',240,'b'*32,123,'/Музыка/song.mp3'));db.commit()
    before=library_revision(db)
    assert enqueue(db,{'trackIds':['a'*24,'a'*24]})=={'queued':1}
    assert enqueue(db,{'trackIds':['a'*24]})=={'queued':0}
    complete(db,'b'*32,{'cues':[{'start':20,'end':130,'bpm':128,'fade':1.8}]},240)
    track=next(t for t in catalog(db)['tracks'] if t['id']=='a'*24)
    assert track['dj']['cues'][0]['end']==130 and track['duration']==240 and track['bytes']==123
    assert 'rangeEnd' not in track and library_revision(db)!=before
    db.execute('INSERT INTO playback_ranges VALUES (?,?,?,?)',('a'*24,15,200,0));db.commit()
    assert next(t for t in catalog(db)['tracks'] if t['id']=='a'*24)['rangeEnd']==200
    work=tmp_path/'backup';work.mkdir();file,counts=snapshot(settings,work)
    assert counts['dj_analysis']==1
    assert len(json.dumps(track['dj']))<1024


@pytest.mark.parametrize('cue',[
 {'start':0,'end':500,'bpm':128,'fade':2}, {'start':10,'end':100,'bpm':float('nan'),'fade':2},
 {'start':10,'end':100,'bpm':128,'fade':.1}, {'start':-1,'end':100,'bpm':128,'fade':2},
])
def test_bad_cues_cannot_reach_devices(cue):
    with pytest.raises(ValueError):validate({'cues':[cue]},240)


def test_asr_gap_does_not_start_fade_inside_word_and_uncertain_words_are_not_discarded():
    # Known phrases separated by instrumental gaps. Individual words are numeric timings only.
    words=[(s,s+1) for s in range(0,190,3)]
    cues=vocal_cues(words,list(range(200)),120,200)
    assert cues
    for cue in cues:
        fade_start=cue['end']-cue['fade']
        assert all(not (start<cue['end'] and end>fade_start) for start,end in words)
        assert 45<=cue['end']-cue['start']<=190
    # Adding uncertain vocals occupies every gap: no apparent missing transcription means a safe cut.
    occupied=[(s,s+1,True) for s in range(0,190,3)]+[(s+1,s+3,False) for s in range(0,190,3)]
    assert vocal_cues(occupied,list(range(200)),120,200)==[]
    assert vocal_cues([],list(range(200)),120,200)==[]
