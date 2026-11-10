"""Complete synthetic raw-contract tests; no real-audio quality verdict/model."""
import copy,json,math
import pytest
from aidj import fixed_set_short_out as short
from aidj.fixed_set_repair import selection_error as public_out
from tests.test_fixed_set_soft_entry import fixture as soft_fixture,voice_at
from tests.test_fixed_set_relative_entry import fixture as rms_fixture


def fixture():
    song,vocal,mix,_,coverage=soft_fixture();rms=rms_fixture()[2]
    rms['sourcePCMInputSHA256']=mix['decode']['pcmSHA256']
    # A False-confidence previous word is retained; the body itself is quiet.
    words=[[79.,79.8,False],[80.4,81.,True]]
    return song,vocal,mix,rms,words,coverage


def check(data,**kw):
    return short.check_out(data[1],data[2],data[3],80.,data[0],data[4],data[5],**kw)


def checkpoint(data):
    proof=check(data);assert proof['accepted']
    return {'trackId':data[0]['id'],'md5':data[0]['md5'],'status':'ready',
        'row':{'trackId':data[0]['id'],'md5':data[0]['md5'],'start':0.,'end':80.,'fadeIn':0.,'fadeOut':proof['fadeOutSeconds']},
        'exit':short.exit_point(proof,vocal_key=data[1]['cacheKey'],mix_key=data[2]['cacheKey'],rms_key=data[3]['cacheKey']),
        'observedWords':copy.deepcopy(data[4]),'observedCoverage':copy.deepcopy(data[5])}


def write(data,folder):
    folder.mkdir(parents=True);(folder.parent/'mix-vad-probes').mkdir();(folder.parent/'mix-rms-probes').mkdir()
    for sub,raw in [('vocal-evidence',data[1]),('mix-vad-probes',data[2]),('mix-rms-probes',data[3])]:
        (folder.parent/sub/(raw['cacheKey']+'.json')).write_text(json.dumps(raw))


def test_explicit200ms_method_is_approximate_preserves_words_and_reports_loud_music():
    data=fixture();before=copy.deepcopy(data);proof=check(data)
    assert proof['accepted']and proof['version']==1 and proof['method']==short.METHOD
    assert proof['proofStart']==79.95 and proof['proofEnd']==80.15
    assert proof['lastObservedWord']==[79.,79.8,False]and proof['lastObservedWordConfident']is False
    assert proof['fadeOutSeconds']==.05 and proof['fadeStartsAtSeconds']>=79.8+.03
    assert proof['actualOriginalStereoRMSP95Dbfs']>-30 and not proof['quietMusicP95AtMostMinus30']
    for key in('ASRWordsErased','asrOverrideApplied','verifiedWholeWordBoundary','voiceAbsenceProven','naturalSilenceClaimed','quietMusicClaimed'):assert proof[key]is False
    assert data==before


@pytest.mark.parametrize('word',[[79.99,80.01,True],[79.99,80.01,False]])
def test_known_whole_word_cannot_be_split_at_quiet_syllable(word):
    data=fixture();data[4].append(word);assert check(data)['reason']=='known-word-crosses-outpoint-guard'


def test_last_word_and_last_loud_frame_END_need100ms_then_protect_fade_start():
    data=fixture();data[4][0]=[79.,79.91,False]
    assert check(data)['reason']=='observed-word-postroll-under100ms'
    data=fixture();voice_at(data[1],data[0],79.90,-20.)
    # Loud frame starts100ms before the cut but ENDS90ms before it.
    assert check(data)['reason']=='observed-voice-postroll-under100ms'
    data=fixture();voice_at(data[1],data[0],79.89,-20.)
    proof=check(data);assert proof['accepted']and proof['lastObservedVoiceDelaySeconds']==pytest.approx(.1)
    assert proof['fadeStartsAtSeconds']>=proof['lastRelativeLoudFrameEndSeconds']+.03


def test_no_previous_observation_stays_unknown_not_invented():
    data=fixture();data[4].clear();proof=check(data)
    assert proof['accepted']and proof['lastObservedWord']is None and proof['lastObservedWordConfident']is None
    assert proof['noObservedRelativeVoiceBranch']and proof['lastRelativeLoudFrameEndSeconds']is None


@pytest.mark.parametrize('mutation',[
    lambda d:d[5].clear(),lambda d:voice_at(d[1],d[0],80.1,-29.9),
    lambda d:d[0].update(lufs=-25.),lambda d:d[2]['identity'].update(requestedEndSeconds=84.),
    lambda d:d[2]['speech']['rightSpeechProbability'].__setitem__(94,.1),
    lambda d:d[2]['speech'].update(lastFrameIncludesPadding=False),
    lambda d:d[3].update(channelsAveragedBeforeRMS=True),
    lambda d:d[3].update(sourcePCMInputSHA256='f'*64),
    lambda d:d[3]['rms']['combinedStereoRms'].__setitem__(300,0.),
    lambda d:d[3]['identity']['source'].update(hi=170.),
])
def test_actual_source_gain_channels_models_geometry_words_and_pcm_agreement_are_mandatory(mutation):
    data=fixture();mutation(data);assert not check(data)['accepted']


def test_public_dispatch_and_current_raw_triple_replay_refuse_tamper(tmp_path):
    data=fixture();item=checkpoint(data);folder=tmp_path/'work'/'vocal-evidence';write(data,folder)
    assert public_out(item)is None
    assert short.selection_error(item,data[0],evidence_folder=folder)is None
    for mutation in[lambda p:p.update(version=2),lambda p:p['parameters'].update(bodyEndSeconds=.1),
            lambda p:p.update(actualOriginalStereoRMSP95Dbfs=-60.),lambda p:p.update(quietMusicClaimed=True),
            lambda p:p.update(lastObservedWordConfident=True),lambda p:p.update(ASRWordsErased=0)]:
        wrong=copy.deepcopy(item);mutation(wrong['exit']['shortOutProof']);assert short.selection_error(wrong,data[0],evidence_folder=folder)
    assert short.selection_error(item,data[0],evidence_folder=folder,rejected=[80.2])=='short-out-owner-rejected-outpoint'
    wrong=copy.deepcopy(item);wrong['observedWords'].append([79.99,80.01,False])
    assert short.selection_error(wrong,data[0],evidence_folder=folder)=='short-out-known-word-crosses-outpoint-guard'


@pytest.mark.parametrize('target,size',[('vocal-evidence',2*1024**2+1),('mix-vad-probes',2*1024**2+1),('mix-rms-probes',1024**2+1)])
def test_evidence_is_bounded_before_parsing(tmp_path,monkeypatch,target,size):
    data=fixture();item=checkpoint(data);folder=tmp_path/'work'/'vocal-evidence';write(data,folder)
    raw={'vocal-evidence':data[1],'mix-vad-probes':data[2],'mix-rms-probes':data[3]}[target]
    with (folder.parent/target/(raw['cacheKey']+'.json')).open('wb')as f:f.truncate(size)
    monkeypatch.setattr(short,'read_json',lambda *a:pytest.fail('oversized evidence parsed'))
    assert short.selection_error(item,data[0],evidence_folder=folder)=='short-out-evidence-size-or-kind'


def test_old_strong_dispatch_and_no_arbitrary_policy_or_long_fade():
    data=fixture()
    assert not check(data,fade_out=.1)['accepted']
    with pytest.raises(TypeError):check(data,body_after=.1)
    assert short.METHOD not in ('vocal-isolated-fade','vocal-checked-instrumental-fade')


def test_exit_is_typed_validated_raw_approximation_without_natural_or_music_quiet_claim():
    item=checkpoint(fixture());point=item['exit']
    assert point['validated']is True and point['proof']==short.PROOF_KIND
    assert not point['naturalSilenceClaimed']and not point['quietMusicClaimed']
    for change in[lambda p:p.update(validated=False),lambda p:p.update(proof='natural-silence'),lambda p:p.update(quietMusicClaimed=True)]:
        wrong=copy.deepcopy(item);change(wrong['exit']);assert public_out(wrong)


def test_real_assembly_branch_reopens_short_out_native_triple(tmp_path):
    import sqlite3
    from aidj.config import Settings
    from aidj.fixed_set_entry_review import fingerprint
    from aidj.fixed_set_repair import assemble
    from aidj.fixed_sets import signature
    data=fixture();song=data[0];item=checkpoint(data)
    item['row'].update(rate=1,transitionKind='clean-cut',transitionSeconds=0)
    item.update(entryReviewVersion=3,entryReviewOnly=False,entryReviewStatus='ready',
        entryProof={'accepted':True,'method':'physical-source-start','sourceStart':0.,'sourceMD5':song['md5'],'acousticProofClaimed':False,'verifiedWordBoundary':False})
    item['entryReviewInputHash']=fingerprint(item,song,[])
    state=tmp_path/'state';state.mkdir();work=tmp_path/'work';work.mkdir();(work/'tracks').mkdir();write(data,work/'vocal-evidence')
    curated=state/'research'/'dj1-curation';curated.mkdir(parents=True)
    for name in('owner-entries.json','owner-rejected.json'):(curated/name).write_text('{}')
    db=sqlite3.connect(state/'bot.sqlite')
    db.execute('create table web_assets(id,md5,bytes,duration)');db.execute('insert into web_assets values(?,?,?,?)',(song['id'],song['md5'],song['bytes'],song['duration']))
    db.execute('create table playback_ranges(track_id,start,end)')
    db.execute('create table audio_loudness(md5,integrated_lufs,true_peak_dbtp,method,status)');db.execute('insert into audio_loudness values(?,?,?,?,?)',(song['md5'],song['lufs'],song['peak'],song['loudnessMethod'],song['loudnessStatus']))
    db.execute('create table dj_boundary_points(id,md5,point_ms)');db.execute('create table dj_boundary_votes(point_id,value)');db.commit();db.close()
    baseline={'id':'dj1','playlistId':1,'revision':signature([item['row']]),'tracks':[item['row']]}
    (work/'baseline.json').write_text(json.dumps(baseline));(work/'tracks'/(song['id']+'.json')).write_text(json.dumps(item))
    settings=Settings(state=state);assemble(settings,work)
    assert json.loads((work/'plan.json').read_text())['tracks'][0]['fadeIn']==0.
    # A native RMS cache changes while the saved accepted verdict is unchanged.
    broken=copy.deepcopy(data[3]);stats=broken['rms']
    for name in('leftRms','rightRms','maxChannelRms','combinedStereoRms'):stats[name][300]=.6
    (work/'mix-rms-probes'/(broken['cacheKey']+'.json')).write_text(json.dumps(broken))
    with pytest.raises(RuntimeError,match='REPAIR_short-out-current-raw-proof-mismatch'):assemble(settings,work)
