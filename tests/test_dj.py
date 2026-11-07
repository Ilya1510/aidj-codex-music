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
    assert track['djV3']['cues'] and track['djV3']['remix']
    assert all(c['start']==0 for c in track['djV2']['cues'])
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


def test_startup_does_not_requeue_the_library_and_preserves_finished_metadata(env):
    from aidj.dj import upgrade_queue
    settings,db,*_=env
    old={'version':1,'cues':[{'start':0,'end':136.5,'bpm':125,'fade':1.9}],'method':'listened'}
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',('a'*32,'a'*24,'complete',json.dumps(old),0))
    for c in 'bc':db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',(c*32,c*24,'pending',None,0))
    db.commit()
    assert upgrade_queue(db)==2 and upgrade_queue(db)==0
    assert db.execute('SELECT payload FROM dj_analysis WHERE md5=?',('a'*32,)).fetchone()[0]==json.dumps(old)
    assert db.execute("SELECT COUNT(*) FROM dj_analysis WHERE status='pending'").fetchone()[0]==0


def test_verified_middle_fragments_and_short_remix_are_not_exposed_to_old_apks(env):
    from aidj.dj import compatible_v2
    cue={'start':55,'end':155,'bpm':128,'fade':1,'mix':3,'entryVerified':True,'energy':.9}
    short={**cue,'end':69}
    value=validate({'cues':[cue],'remix':[short]},240)
    assert value['version']==3 and value['cues'][0]['start']==55 and value['remix'][0]['end']==69
    assert compatible_v2(value)['cues']==[] and legacy(value)['cues']==[]
    assert validate({'cues':[{**cue,'entryVerified':False}]},240)['cues'][0]['start']==0
    with pytest.raises(ValueError):validate({'remix':[{**short,'entryVerified':False}]},240)


def test_partial_asr_coverage_cannot_invent_a_vocal_gap_between_windows():
    from aidj.dj_worker import vocal_gaps,selection
    words=[(s,s+.5,True) for s in range(0,100,2)]
    evidence={'words':words,'beats':list(range(100)),'bpm':120,'duration':100,'coverage':[[0,20],[22,45]]}
    gaps=vocal_gaps(evidence)
    assert gaps and all(not (lo<22 and hi>20) for lo,hi in gaps)
    assert all(hi<45 for lo,hi in gaps)
    chosen=selection({**evidence,'energy':[.1]*20+[.95]*80})
    assert chosen['remix']
    for cue in chosen['remix']:
        assert cue['entryVerified'] and 8<=cue['end']-cue['start']<=24
        assert not any(lo<cue['start']<hi or lo<cue['end']-cue['fade']<hi for lo,hi,_ in words)


def test_selection_prefers_an_energetic_middle_over_a_quiet_prefix():
    from aidj.dj_worker import selection
    evidence={'words':[(s,s+2,True) for s in range(0,240,4)],'beats':[i*.5 for i in range(480)],'bpm':120,'duration':240,'coverage':[[0,240]],'energy':[.1]*55+[1]*130+[.4]*55}
    value=selection(evidence)
    assert value['cues'] and any(cue['start']>40 for cue in value['cues'])
    assert len(value['remix'])>=2
    assert all(c['entryVerified'] for c in value['cues']+value['remix'])


def test_active_queue_is_capped_at_ten_even_for_old_whole_playlist_requests(env):
    from aidj.dj import next_job
    settings,db,*_=env
    ids=[format(n,'024x') for n in range(1,31)]
    for n,aid in enumerate(ids):db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(aid,'youtube:'+str(n),'Song','Artist',180,format(n,'032x'),123,'/Музыка/'+str(n)+'.mp3'))
    db.commit();enqueue(db,{'trackIds':ids})
    assert db.execute("SELECT COUNT(*) FROM dj_analysis WHERE status='pending'").fetchone()[0]==10
    running=next_job(db)
    db.execute("UPDATE dj_analysis SET status='working' WHERE md5=?",(running['md5'],));db.commit()
    enqueue(db,{'trackIds':ids[15:25],'nextTrackIds':ids[15:25]})
    assert db.execute("SELECT COUNT(*) FROM dj_analysis WHERE status IN ('pending','working')").fetchone()[0]==10
    assert db.execute('SELECT status FROM dj_analysis WHERE md5=?',(running['md5'],)).fetchone()[0]=='working'
    assert db.execute('SELECT COUNT(*) FROM dj_analysis WHERE asset_id=?',(ids[1],)).fetchone()[0]==0


def test_refinement_preserves_a_ready_cue_when_no_new_safe_exit_is_found(env):
    settings,db,*_=env
    cue={'start':0,'end':110,'bpm':120,'fade':1.2}
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',('a'*32,'b'*24,'pending',json.dumps({'version':2,'cues':[cue]}),0));db.commit()
    complete(db,'a'*32,{'cues':[],'remix':[],'reason':'no-safe-gap'},240)
    value=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',('a'*32,)).fetchone()[0])
    assert value['cues'][0]['end']==110 and value['reason']=='kept-previous'


def test_targeted_probes_are_bounded_and_completed_observations_are_not_repeated():
    from aidj.dj_worker import probe_windows
    evidence={'version':2,'words':[(s,s+2,True) for s in range(45,180,4)],'beats':[],'bpm':0,'duration':235,'energy':[.4]*50+[1]*100+[.2]*85}
    first=probe_windows(evidence)
    assert 1<=len(first)<=3 and all(12<=hi-lo<=28 for lo,hi in first)
    assert sum(hi-lo for lo,hi in first)<=84
    assert any(lo>180 for lo,hi in first)
    assert probe_windows({**evidence,'probes':first})==[]


def test_refinement_keeps_previous_remix_independently_of_new_regular_cues(env):
    settings,db,*_=env
    short={'start':55,'end':69,'bpm':128,'fade':1,'entryVerified':True}
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',('a'*32,'b'*24,'pending',json.dumps({'version':3,'cues':[],'remix':[short]}),0));db.commit()
    complete(db,'a'*32,{'cues':[{'start':0,'end':110,'bpm':128,'fade':1}],'remix':[]},240)
    value=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',('a'*32,)).fetchone()[0])
    assert value['cues'][0]['end']==110 and value['remix'][0]['start']==55


def test_previously_validated_exit_can_pair_with_a_new_whole_word_entry():
    from aidj.dj_worker import selection
    # The old known-safe 71.8s exit remains usable even when the new local ASR
    # window ends before it. The entry has separately observed confident words.
    words=[(s,s+1.4,True) for s in range(40,67,2)]
    evidence={'words':words,'beats':[],'bpm':120,'duration':190,'coverage':[[40,68]],'safeExits':[[69.8,71.8]]}
    remix=selection(evidence)['remix']
    assert any(c['start']>50 and c['end']==71.8 for c in remix)


def narrow_gap_evidence():
    # Ordinary gaps are too short for an exit. Only these two deliberately
    # wider observations leave 160ms / 400ms after BOTH 200ms word guards.
    words=[[s,s+.5,True] for s in range(180)]
    words[49][1]=49.44
    words[124][1]=124.2
    return {'version':3,'duration':180,'words':words,'beats':[],'bpm':120,'coverage':[[0,180]],'energy':[.8]*180,'analysisScope':'full'}


def test_v4_shorter_exit_fades_stay_between_the_same_observed_whole_words():
    from aidj.dj_worker import selection
    evidence=narrow_gap_evidence();value=validate(selection(evidence),180)
    assert value['version']==3 and value['cueSchema']==4
    assert value['cues'] and all(.25<=c['fade']<.5 for c in value['cues'])
    assert value['remix'] and any(.15<=c['fade']<.25 for c in value['remix'])
    for cue in value['cues']+value['remix']:
        assert cue['entryVerified'] is True and cue['wordGuarded'] is True and cue['mix']>=3
        before=max(w[1] for w in evidence['words'] if w[1]<cue['end'])
        after=min(w[0] for w in evidence['words'] if w[0]>cue['end'])
        assert cue['end']-cue['fade']>=before+.2-1e-6
        assert cue['end']<=after-.2+1e-6
        assert not any(lo<cue['end'] and hi>cue['end']-cue['fade'] for lo,hi,_ in evidence['words'])
        if cue['start']:
            assert cue['start']>=max(w[1] for w in evidence['words'] if w[1]<cue['start'])+.2-1e-6
            assert cue['start']<=min(w[0] for w in evidence['words'] if w[0]>cue['start'])-.2+1e-6


@pytest.mark.parametrize('change',['uncertain','unobserved','occupied'])
def test_narrow_fade_fallback_does_not_relax_word_confidence_or_observed_coverage(change):
    from aidj.dj_worker import selection
    evidence=narrow_gap_evidence()
    if change=='uncertain':
        evidence['words'][49][2]=False;evidence['words'][124][2]=False
    elif change=='unobserved':evidence['coverage']=[[0,40],[60,110],[140,180]]
    else:evidence['words'] += [[49.5,49.95,False],[124.25,124.95,False]]
    value=selection(evidence)
    assert value['cues']==[] and value['remix']==[]


def test_regular_selection_prefers_an_available_wider_exit_to_a_shorter_fade():
    from aidj.dj_worker import selection
    evidence=narrow_gap_evidence()
    evidence['words'][128][1]=128.2
    evidence['words'][129][0]=129.8;evidence['words'][129][1]=130
    value=selection(evidence)
    assert value['cues'] and all(c['fade']>=.5 for c in value['cues'])
    assert value['remix'][0]['fade']>=.5


@pytest.mark.parametrize('change',[
 {'cueSchema':3},{'cueSchema':4.0},{'entryVerified':False},{'entryVerified':1},
 {'wordGuarded':False},{'wordGuarded':1},{'wordGuarded':'true'},
])
def test_narrow_fades_require_new_schema_and_boolean_guard_markers(change):
    cue={'start':35,'end':125,'bpm':120,'fade':.4,'entryVerified':True,'wordGuarded':True,'mix':3.2}
    body={'version':3,'cueSchema':4,'cues':[cue]}
    for k,v in change.items():
        if k=='cueSchema':body[k]=v
        else:cue[k]=v
    with pytest.raises(ValueError):validate(body,180)


def test_unguarded_cues_keep_old_fade_minimum_and_guarded_regular_has_own_floor():
    cue={'start':35,'end':125,'bpm':120,'fade':.2,'entryVerified':True,'wordGuarded':True,'mix':3.2}
    with pytest.raises(ValueError):validate({'cueSchema':4,'cues':[cue]},180)
    assert validate({'cueSchema':4,'remix':[{**cue,'end':49}]},180)['remix'][0]['fade']==.2
    with pytest.raises(ValueError):validate({'cueSchema':4,'remix':[{**cue,'end':49,'fade':.14}]},180)
    with pytest.raises(ValueError):validate({'cues':[{**cue,'fade':.4}]},180)


def test_catalog_v4_pool_and_retained_broad_fades_are_compatible_with_older_apks(env):
    settings,db,*_=env;aid='a'*24;digest='b'*32
    db.execute('INSERT INTO web_assets VALUES (?,?,?,?,?,?,?,?)',(aid,'youtube:abcdefghijk','Song','Artist',180,digest,123,'/Музыка/song.mp3'))
    oldcue={'start':0,'end':110,'bpm':120,'fade':1.2,'entryVerified':True}
    oldclip={'start':55,'end':69,'bpm':120,'fade':.8,'entryVerified':True}
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',(digest,aid,'pending',json.dumps({'version':3,'cues':[oldcue],'remix':[oldclip]}),0));db.commit()
    fresh={'cueSchema':4,'cues':[{**oldcue,'start':20,'fade':.4,'wordGuarded':True}],
           'remix':[{**oldclip,'start':35,'end':49,'fade':.16,'wordGuarded':True}]}
    complete(db,digest,fresh,180)
    complete(db,digest,fresh,180) # Duplicate completion never duplicates the alternatives.
    track=next(t for t in catalog(db)['tracks'] if t['id']==aid)
    assert len(track['djV4']['cues'])==2 and len(track['djV4']['remix'])==2
    assert track['djV4']['cues'][0]['fade']==.4 and track['djV4']['remix'][0]['fade']==.16
    assert track['djV3']['cues']==[oldcue] and track['djV3']['remix']==[oldclip]
    assert track['djV2']['cues']==[oldcue] and track['dj']['cues']==[oldcue]
    assert 'cueSchema' not in track['djV3']
    # An old client finishing another broad-fade result cannot lose the v4
    # discriminator for the narrow candidates retained from earlier output.
    complete(db,digest,{'cues':[oldcue],'remix':[oldclip]},180)
    saved=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',(digest,)).fetchone()[0])
    assert saved['cueSchema']==4 and validate(saved,180)['remix']


def test_new_remix_pool_is_bounded_but_keeps_different_lengths_and_temporal_sections():
    from aidj.dj_worker import selection
    evidence={'words':[[s,s+2,True] for s in range(0,360,4)],'coverage':[[0,360]],'beats':[],
              'bpm':120,'duration':360,'energy':[.8]*360,'analysisScope':'full'}
    value=validate(selection(evidence),360);clips=value['remix']
    assert 12<len(clips)<=32
    assert len({int(8*c['start']/360) for c in clips})>=6
    assert any(a['start']==b['start'] and abs(a['end']-b['end'])>=2 for a in clips for b in clips)
    assert all(abs(a['start']-b['start'])>=2 or abs(a['end']-b['end'])>=2 for i,a in enumerate(clips) for b in clips[:i])
    from aidj.dj import compatible_v3
    assert len(compatible_v3(value)['remix'])<=6


def test_full_new_pool_reserves_previous_broad_fades_for_old_players(env):
    settings,db,*_=env
    broad={'start':0,'end':14,'bpm':120,'fade':1,'entryVerified':True}
    db.execute('INSERT INTO dj_analysis VALUES (?,?,?,?,?)',('a'*32,'b'*24,'pending',json.dumps({'version':3,'cues':[],'remix':[broad]}),0));db.commit()
    clips=[{'start':10+i*5,'end':24+i*5,'bpm':120,'fade':.2,'entryVerified':True,'wordGuarded':True} for i in range(32)]
    complete(db,'a'*32,{'cueSchema':4,'cues':[],'remix':clips},200)
    saved=json.loads(db.execute('SELECT payload FROM dj_analysis WHERE md5=?',('a'*32,)).fetchone()[0])
    assert len(saved['remix'])==32
    from aidj.dj import compatible_v3
    assert compatible_v3(saved)['remix']==[broad]
