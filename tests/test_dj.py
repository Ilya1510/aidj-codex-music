import json
import pytest
from aidj.dj import enqueue,complete,validate,legacy
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


def test_short_vocal_gap_uses_long_incoming_mix_without_cutting_initial_or_final_words():
    words=[(s,s+3) for s in range(0,180,4)]
    cues=vocal_cues(words,[i*.5 for i in range(360)],0,180)
    assert cues
    for cue in cues:
        assert cue['start']==0 and cue['mix']>=3 and cue['fade']>=.35
        assert cue['mix']>cue['fade']
        fade_start=cue['end']-cue['fade']
        assert all(not (start<cue['end'] and end>fade_start) for start,end in words)
        assert 'grid' not in cue
    value=validate({'cues':cues},180)
    assert legacy(value)['cues']==[]
    assert all(cue['start']==0 for cue in value['cues'])


def test_cached_numeric_evidence_can_refill_cues_without_downloading_or_transcribing(env,monkeypatch):
    from aidj.dj_worker import process
    settings,db,*_=env
    digest='b'*32;aid='a'*24
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(aid,'youtube:abcdefghijk','Song','Artist',180,digest,123,'/Музыка/song.mp3'))
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',(digest,aid,'pending',None,0))
    evidence={'words':[(s,s+3,True) for s in range(0,180,4)],'beats':[i*.5 for i in range(360)],'bpm':120,'duration':180}
    db.execute('INSERT INTO dj_evidence VALUES (?,?,?)',(digest,json.dumps(evidence),0));db.commit()
    def forbidden(*args):raise AssertionError('numeric evidence must avoid another audio download')
    monkeypatch.setattr('aidj.disk.Disk',forbidden)
    row=db.execute('SELECT * FROM dj_analysis WHERE md5=?',(digest,)).fetchone();process(settings,db,row,'unused')
    track=next(t for t in catalog(db)['tracks'] if t['id']==aid)
    assert track['djV2']['cues'] and not track['dj']['cues']
    assert track['djStatus']=='complete' and 'words' not in json.dumps(track)


def test_foreign_boolean_and_numeric_word_probabilities_are_saved_as_json_atoms():
    from types import SimpleNamespace as N
    from aidj.dj_worker import numeric_words
    class ForeignBool:
        def __bool__(self):return True
    class Probability(float):
        def __ge__(self,other):return ForeignBool()
    part=N(no_speech_prob=.2,avg_logprob=-.5,words=[N(start=1,end=2,probability=Probability(.9))])
    rows=numeric_words(part,45)
    assert rows==[(46.,47.,True)] and type(rows[0][2]) is bool
    assert json.loads(json.dumps(rows))==[[46.,47.,True]]


def test_playback_queue_priorities_precede_bulk_preparation_and_worker_state_is_visible(env):
    from aidj.dj import next_job,heartbeat,service_state
    settings,db,*_=env
    ids=[c*24 for c in 'abc']
    for c,aid in zip('abc',ids):db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(aid,'youtube:'+c,'Song '+c,'Artist',180,c*32,123,'/Музыка/'+c+'.mp3'))
    db.commit();enqueue(db,{'trackIds':ids,'nextTrackIds':[ids[2],ids[1]]})
    assert next_job(db)['asset_id']==ids[2]
    db.execute("UPDATE dj_analysis SET status='complete' WHERE asset_id=?",(ids[2],));db.commit()
    assert next_job(db)['asset_id']==ids[1]
    heartbeat(db,'working');assert service_state(db)=={'state':'working'}
    db.execute('UPDATE dj_runtime SET heartbeat=0');db.commit();assert service_state(db)=={'state':'stale'}
    heartbeat(db,'stopped');assert service_state(db)=={'state':'stopped'}
    assert 'heartbeat' not in json.dumps(service_state(db))


def test_upgrade_requeues_empty_legacy_results_once_and_preserves_listened_cues(env):
    from aidj.dj import upgrade_queue
    settings,db,*_=env
    for c in 'ab':db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(c*24,'youtube:'+c,'Song','Artist',180,c*32,123,'/Музыка/'+c+'.mp3'))
    old={'version':1,'cues':[{'start':17.3,'end':136.5,'bpm':125,'fade':1.9}],'method':'listened'}
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',('a'*32,'a'*24,'complete',json.dumps(old),0))
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',('b'*32,'b'*24,'complete',json.dumps({'version':1,'cues':[]}),0));db.commit()
    assert upgrade_queue(db)==1 and upgrade_queue(db)==0
    first=db.execute('SELECT payload FROM dj_analysis WHERE md5=?',('a'*32,)).fetchone()
    assert json.loads(first[0])['cues'][0]['start']==0 and json.loads(first[0])['method']=='listened'
    assert db.execute('SELECT status FROM dj_analysis WHERE md5=?',('b'*32,)).fetchone()[0]=='pending'
