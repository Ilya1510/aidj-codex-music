"""Explicit long residual range never replaces current ENTRY/OUT verification."""
import copy
import pytest
from aidj import fixed_set_duration_exception as duration
from aidj import fixed_set_closing as closing
from aidj.fixed_set_entry_review import fingerprint,selection_error as entry_error
from aidj.fixed_set_repair import selection_error as exit_error
from tests.test_fixed_set_closing import create


def owner_long():
    item,source=create(duration=180.)
    item['row']['start']=70.
    item['entry']={'time':70.,'kind':'owner-approved','fade':item['row']['fadeIn']}
    item['entryProof']={'accepted':True,'method':'owner-approved','sourceStart':70.,
        'sourceMD5':source['md5'],'verifiedWordBoundary':False,'acousticProofClaimed':False}
    item['entryReviewVersion']=3
    item['entryReviewInputHash']=fingerprint(item,source,[70.])
    return item,source


def marked_owner():
    item,source=owner_long();item['durationException']=duration.make_exception(item,source)
    return item,source


def test_unmarked_normal100_is_unchanged_and_marker_is_pure_structure_only(tmp_path):
    item,source=owner_long();before=copy.deepcopy(item)
    assert duration.maximum_span(item,source)==100.
    assert closing.selection_error(item,source)=='closing-row-bounds'
    marker=duration.make_exception(item,source)
    assert item==before
    item['durationException']=marker
    assert duration.maximum_span(item,source)==120.
    assert closing.selection_error(item,source)is None
    assert closing.saved_selection_error(item)is None and exit_error(item)is None
    assert entry_error(item,source,owner=[70.],evidence_folder=tmp_path)is None
    assert not marker.get('quietClaimed') and not marker.get('wordBoundaryCertified')


def test_current_owner_removal_rejects_final_entry_even_when_structural_range_passes(tmp_path):
    item,source=marked_owner()
    assert closing.selection_error(item,source)is None
    assert entry_error(item,source,owner=[],evidence_folder=tmp_path)=='entry-input-changed'
    # Even a recomputed fingerprint cannot fabricate the removed owner mark.
    item['entryReviewInputHash']=fingerprint(item,source,[])
    assert entry_error(item,source,owner=[],evidence_folder=tmp_path)=='entry-exemption-invalid'


@pytest.mark.parametrize('mutate',[
    lambda x:x.update(durationException=None),
    lambda x:x.update(durationExceptionOther={}),
    lambda x:x['durationException'].update(version=True),
    lambda x:x['durationException'].update(version=2),
    lambda x:x['durationException'].update(kind='any-long-song'),
    lambda x:x['durationException'].update(maximumSeconds=121.),
    lambda x:x['durationException'].update(minimumSourceStartFraction=.1),
    lambda x:x['durationException'].update(sourceId='f'*24),
    lambda x:x['durationException'].update(sourceMD5='f'*32),
    lambda x:x['durationException'].update(sourceBytes=True),
    lambda x:x['durationException'].update(sourceDuration=181.),
    lambda x:x['durationException'].update(allowedStart=1.),
    lambda x:x['durationException'].update(allowedEnd=179.),
    lambda x:x['durationException'].update(sourceStart=70.1),
    lambda x:x['durationException'].update(sourceEnd=179.),
    lambda x:x['durationException'].update(entryMethod='relative-stereo-valley-entry'),
    lambda x:x['durationException'].update(extra=True),
    lambda x:x['durationException'].pop('maximumSeconds'),
    lambda x:x['entryProof'].update(accepted=False),
    lambda x:x['entryProof'].update(sourceStart=71.),
    lambda x:x['entryProof'].update(method='unverified-estimate'),
])
def test_marker_policy_source_bounds_method_and_shape_tamper_fails(mutate):
    item,source=marked_owner();mutate(item)
    with pytest.raises((ValueError,TypeError)):duration.maximum_span(item,source)
    assert closing.selection_error(item,source)=='closing-duration-exception-invalid'


def test_marker_is_rejected_when_not_needed_or_longer_than120_or_near_whole_source():
    item,source=owner_long()
    for start in [80.,59.,0.]:
        wrong=copy.deepcopy(item);wrong['row']['start']=start;wrong['entryProof']['sourceStart']=start
        with pytest.raises(ValueError):duration.make_exception(wrong,source)
    # Isolate the25% source-prefix constraint with an otherwise110s range.
    larger={**source,'duration':480.,'hi':480.}
    wrong=copy.deepcopy(item);wrong['row'].update(start=90.,end=200.);wrong['entryProof']['sourceStart']=90.
    with pytest.raises(ValueError,match='prefix'):duration.make_exception(wrong,larger)


def test_matching_entry_claim_is_required_but_raw_entry_proof_is_still_mandatory(tmp_path):
    item,source=owner_long();item['entryProof']['method']='relative-stereo-valley-entry'
    item['entryReviewInputHash']=fingerprint(item,source,[])
    item['durationException']=duration.make_exception(item,source)
    assert duration.maximum_span(item,source)==120.
    # This is only a range claim: a fabricated bare accepted relative proof
    # cannot pass the independent final ENTRY handler.
    assert entry_error(item,source,owner=[],evidence_folder=tmp_path)


def test_relative_raw_gate_allows_only_explicit_long_marker_and_retains_word_veto(tmp_path):
    from aidj import fixed_set_relative_entry as relative
    from aidj.fixed_set_soft_entry import digest
    from tests.test_fixed_set_relative_entry import fixture,checkpoint,write
    data=fixture();song=data[0];song.update(duration=220.,hi=220.)
    data[2]['identity']=relative.rms_identity(song,80.);data[2]['cacheKey']=digest(data[2]['identity'])
    item=checkpoint(data);item['row']['end']=190.;item['entryReviewInputHash']=fingerprint(item,song,[])
    folder=tmp_path/'work'/'vocal-evidence';folder.parent.mkdir();write(data,folder)
    assert relative.selection_error(item,song,evidence_folder=folder,rejected=[])=='relative-entry-row'
    item['durationException']=duration.make_exception(item,song)
    assert relative.selection_error(item,song,evidence_folder=folder,rejected=[])is None
    item['observedWords'].append([79.99,80.01,False]);item['entryReviewInputHash']=fingerprint(item,song,[])
    assert relative.selection_error(item,song,evidence_folder=folder,rejected=[])=='relative-entry-known-word-crosses-entry-guard'


def test_soft_raw_gate_uses_same_explicit_range_contract_without_changing_proof_math(tmp_path):
    import json
    from aidj import fixed_set_soft_entry as soft
    from tests.test_fixed_set_soft_entry import fixture,checkpoint
    data=fixture();song=data[0];song.update(duration=220.,hi=220.)
    data[2].update(sourceDuration=220.,sourceRange=[0.,220.])
    item=checkpoint(data);item['row']['end']=190.;item['entryReviewInputHash']=fingerprint(item,song,[])
    folder=tmp_path/'work'/'vocal-evidence';folder.mkdir(parents=True);mixes=folder.parent/'mix-vad-probes';mixes.mkdir()
    (folder/(data[1]['cacheKey']+'.json')).write_text(json.dumps(data[1]));(mixes/(data[2]['cacheKey']+'.json')).write_text(json.dumps(data[2]))
    assert soft.selection_error(item,song,evidence_folder=folder,rejected=[])=='soft-entry-row'
    before=copy.deepcopy(item['entryProof']);item['durationException']=duration.make_exception(item,song)
    assert soft.selection_error(item,song,evidence_folder=folder,rejected=[])is None
    assert item['entryProof']==before


def test_assembler_blocks_unmarked_long_generic_owner_entry_before_any_stereo_replay(tmp_path):
    import json,sqlite3
    from types import SimpleNamespace
    from aidj.fixed_set_repair import assemble
    work=tmp_path/'work';work.mkdir();(work/'tracks').mkdir()
    state=tmp_path/'state';state.mkdir();curation=state/'research'/'dj1-curation';curation.mkdir(parents=True)
    aid='a'*24;md5='1'*32
    row={'trackId':aid,'md5':md5,'start':80.,'end':190.,'fadeIn':.08,'fadeOut':.8,
        'transitionKind':'clean-cut','transitionSeconds':0,'rate':1}
    item={'status':'ready','trackId':aid,'md5':md5,'row':row,'observedWords':[],
        'observedCoverage':[[0.,220.]],'exit':{'method':'strict-quiet','time':190.,'fade':.8,'fadeStart':189.2,
        'regionStart':188.,'regionEnd':191.,'maxRms':.0001,'relativeMax':.001,'coverageComplete':True,
        'absoluteSilence':True},'entryReviewVersion':3,'entryProof':{'accepted':True,'method':'owner-approved',
        'sourceStart':80.,'sourceMD5':md5,'acousticProofClaimed':False}}
    assert exit_error(item)is None
    (work/'baseline.json').write_text(json.dumps({'tracks':[row],'revision':'synthetic'}))
    (work/'tracks'/(aid+'.json')).write_text(json.dumps(item));(curation/'owner-entries.json').write_text(json.dumps({aid:[80.]}))
    db=sqlite3.connect(state/'bot.sqlite')
    db.executescript('CREATE TABLE web_assets(id TEXT,md5 TEXT,bytes INTEGER,duration REAL); CREATE TABLE playback_ranges(track_id TEXT,start REAL,end REAL); CREATE TABLE audio_loudness(md5 TEXT,integrated_lufs REAL,true_peak_dbtp REAL,method TEXT,status TEXT);')
    db.execute('INSERT INTO web_assets VALUES(?,?,?,?)',(aid,md5,1000000,220.));db.commit();db.close()
    with pytest.raises(RuntimeError,match='REPAIR_INVALID_DURATION_EXCEPTION'):
        assemble(SimpleNamespace(state=state),work)
    assert not (work/'plan.json').exists()
