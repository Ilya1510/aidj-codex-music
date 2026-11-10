"""Stored completion proves a file boundary, never invented vocal silence."""
from array import array
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from aidj import fixed_set_closing as closing
from aidj import fixed_set_hard_review as hard
from aidj.fixed_set_entry_review import selection_error as entry_error
from aidj.fixed_set_repair import selection_error as repair_error


def fixture(duration=180.,level=.2):
    source={'id':'a'*24,'md5':'1'*32,'bytes':3000000,'track_key':'yandex:123',
            'duration':duration,'lo':0.,'hi':duration,'artist':'Artist','title':'Song',
            'lufs':-16.,'peak':-2.,'loudnessMethod':'ebu-r128-v1','loudnessStatus':'complete'}
    old={'trackId':source['id'],'md5':source['md5'],'start':0.,'end':62.,'fadeIn':.08,
         'fadeOut':.8,'transitionKind':'clean-cut','transitionSeconds':0,'rate':1}
    frames=round(duration*16000)
    timing={'decodedDuration':frames/16000,'metadataDuration':duration,'sourceMD5':source['md5'],
            'manualEnd':False,'sourceTimingMethod':'full-verified-decode-strict-v2',
            'decoderMethod':'full-verified-decode-strict-v2','decoderErrorPolicy':'xerror',
            'completeSource':True,'decoderExitCode':0,'sampleRate':16000,'channels':2,
            'pcmFrames':frames,'pcmBytes':frames*8}
    first=frames-80000
    tail={'method':'strict-complete-stereo-pcm-tail-v1','sourceMD5':source['md5'],
          'sampleRate':16000,'channels':2,'sourceFirstFrame':first,'sourceLastFrameExclusive':frames,
          'sourceStartSeconds':first/16000,'sourceEndSeconds':frames/16000,
          'frameHopSeconds':.01,'frameSamples':[160]*500,'rmsLeft':[level]*500,
          'rmsRight':[level]*500,'peakLeft':[level]*500,'peakRight':[level]*500,
          'maxChannelRms':[level]*500,'lastStereoSamples':[level]*64,
          'silenceClaimed':False,'wordAlignmentClaimed':False}
    previous={'status':'needs-review','trackId':source['id'],'md5':source['md5'],
              'entryProof':{'stale':True},'stereoEvidence':{'stale':True}}
    evidence={'words':[[duration-3,duration,True]],'coverage':[[0,duration]],'repairProbes':[]}
    return old,source,previous,evidence,timing,tail


def create(*,duration=180.,level=.2,owner=()):
    old,source,previous,evidence,timing,tail=fixture(duration,level)
    result=closing.create_checkpoint(old,source,previous,evidence,timing,tail,owner=owner)
    return result,source


def test_loud_vocal_at_eof_is_preserved_without_an_internal_cut_or_quiet_claim():
    old,source,previous,evidence,timing,tail=fixture()
    result=closing.create_checkpoint(old,source,previous,evidence,timing,tail,owner=[118.])
    assert result['row']['start']==118 and result['row']['end']==source['duration']
    assert result['row']['fadeOut']==0 and result['row']['transitionSeconds']==0
    assert result['exit']['method']=='stored-file-ending'
    assert result['observedWords']==evidence['words']  # No ASR word was erased.
    assert result['sourceEnding']['nativeCompletionRequired'] is True
    assert result['sourceEnding']['quietClaimed'] is False
    assert result['sourceEnding']['artistIntendedEndingClaimed'] is False
    assert 'maxRms' not in result['exit'] and 'vocalCheck' not in result['exit']
    assert closing.selection_error(result,source) is None
    assert closing.saved_selection_error(result) is None
    assert repair_error(result) is None  # No fabricated quiet-region fields needed.
    assert previous['status']=='needs-review' and previous['entryProof']=={'stale':True}


def test_ending_ready_is_not_an_entry_certificate(tmp_path):
    result,source=create()
    assert result['status']=='ready' and result['entryReviewStatus']=='pending-final-entry-proof'
    assert result['entry']['kind']=='closing-estimated-entry'
    assert result['entry']['requiresFinalEntryProof'] and 'entryProof' not in result
    assert 'stereoEvidence' not in result
    assert entry_error(result,source,evidence_folder=tmp_path)=='entry-not-reviewed'


def test_short_source_can_play_whole_but_long_song_must_use_only_a_tail():
    result,source=create(duration=57.)
    assert result['row']['start']==0 and result['row']['end']==57
    result,source=create(duration=240.)
    assert result['row']['start']==178 and result['row']['end']-result['row']['start']==62
    invalid=copy.deepcopy(result);invalid['row']['start']=0
    assert closing.selection_error(invalid,source)=='closing-row-bounds'


def test_metadata_end_is_exact_and_pcm_end_is_kept_as_a_distinct_observation():
    old,source,previous,evidence,timing,tail=fixture(180.052235969337)
    # A valid full decode need not share container/encoder-padding duration.
    source['duration']=source['hi']=180.052235969337
    timing['metadataDuration']=source['duration']
    result=closing.create_checkpoint(old,source,previous,evidence,timing,tail)
    assert result['row']['end']==180.052235969337
    assert result['row']['end']!=round(result['row']['end'],4)
    assert result['sourceEnding']['decodedDuration']==timing['pcmFrames']/16000
    assert closing.selection_error(result,source) is None


@pytest.mark.parametrize('change',[{'decoderErrorPolicy':'ignore'},{'completeSource':False},
    {'sourceMD5':'2'*32},{'pcmFrames':1},{'pcmBytes':16000*1800*8},
    {'sourceTimingMethod':'full-verified-decode','decoderMethod':'full-verified-decode'}])
def test_incomplete_error_tolerant_wrong_source_or_capped_decode_never_proves_ending(change):
    old,source,previous,evidence,timing,tail=fixture();timing.update(change)
    with pytest.raises(RuntimeError,match='CLOSING_STRICT_DECODE_REQUIRED'):
        closing.create_checkpoint(old,source,previous,evidence,timing,tail)


def test_changed_current_range_gain_bytes_or_md5_invalidates_saved_proof():
    result,source=create()
    for change in [{'hi':179.9},{'lufs':-10},{'peak':0.},{'bytes':3000001},{'md5':'2'*32}]:
        assert closing.selection_error(result,{**source,**change})=='closing-source-snapshot-changed'
    old,source,previous,evidence,timing,tail=fixture();source['hi']=179.9
    with pytest.raises(RuntimeError,match='CLOSING_MANUAL_END_HOLD'):
        closing.create_checkpoint(old,source,previous,evidence,timing,tail)


def clip_fixture(level=.2):
    old,source,previous,evidence,timing,tail=fixture(85.0232,level)
    original='2'*32;identity=hashlib.sha256(f'{original}:0.000:85.000:aac-v1'.encode()).hexdigest()[:24]
    source['track_key']='clip:'+identity
    lineage={'method':'app-created-aac-clip-v1','trackKey':source['track_key'],'start':0.,'end':85.,
             'originalMD5':original,'originalAssetId':'b'*24,'originalTrackKey':'youtube:abcdefghijk',
             'originalDuration':241.3714}
    return old,source,previous,evidence,timing,tail,lineage


def test_owner_clip_has_distinct_lineage_and_active_tail_softout():
    old,source,previous,evidence,timing,tail,lineage=clip_fixture()
    result=closing.create_checkpoint(old,source,previous,evidence,timing,tail,lineage=lineage)
    assert result['exit']['method']=='stored-owner-clip-ending' and result['row']['fadeOut']==4
    assert result['sourceEnding']['clipLineage']['originalDuration']==241.3714
    assert closing.selection_error(result,source,clip_lineage=lineage) is None
    assert closing.selection_error(result,source,clip_lineage={**lineage,'end':84.})=='closing-clip-lineage-changed'
    assert closing.selection_error(result,source,clip_lineage=None)=='closing-clip-lineage-changed'
    with pytest.raises(RuntimeError,match='CLOSING_CLIP_LINEAGE_INVALID'):
        closing.create_checkpoint(old,source,previous,evidence,timing,tail)


def test_quiet_owner_clip_does_not_receive_an_unnecessary_long_fade():
    old,source,previous,evidence,timing,tail,lineage=clip_fixture(.00001)
    result=closing.create_checkpoint(old,source,previous,evidence,timing,tail,lineage=lineage)
    assert result['row']['fadeOut']==0
    assert closing.selection_error(result,source,clip_lineage=lineage) is None


def test_replay_checks_raw_tail_values_hash_last_samples_and_false_claims():
    result,source=create()
    for mutate in [lambda v:v['tailEvidence']['maxChannelRms'].__setitem__(0,.9),
                   lambda v:v['tailEvidence']['lastStereoSamples'].__setitem__(0,.9),
                   lambda v:v['tailEvidence'].update(sourceFirstFrame=0),
                   lambda v:v.update(quietClaimed=True),
                   lambda v:v.update(artistIntendedEndingClaimed=True)]:
        bad=copy.deepcopy(result);mutate(bad['sourceEnding'])
        assert closing.selection_error(bad,source) is not None
    bad=copy.deepcopy(result);bad['row']['end']=round(source['duration']-.0001,4)
    assert closing.selection_error(bad,source)=='closing-not-exact-stored-end'


def test_end_proof_survives_a_later_independently_validated_start_shift():
    result,source=create();proof=copy.deepcopy(result['sourceEnding'])
    result['row']['start']-=1
    assert closing.selection_error(result,source) is None and result['sourceEnding']==proof


def test_tail_observer_reads_real_channels_and_partial_final_frame_without_mono_cancellation(tmp_path):
    samples=array('f',[.5,-.5]*160+[.1,-.1]);path=tmp_path/'pcm.f32';path.write_bytes(samples.tobytes())
    timing={'pcmFrames':161,'sourceMD5':'1'*32}
    tail=closing.observe_tail(path,timing)
    assert tail['frameSamples']==[160,1]
    assert tail['rmsLeft'][0]==.5 and tail['rmsRight'][0]==.5
    assert tail['maxChannelRms'][0]==.5  # Signed mono average would be zero.
    assert tail['lastStereoSamples'][-2:]==pytest.approx([.1,-.1])
    assert closing.tail_error(tail,timing,'1'*32) is None


def test_optional_decode_observer_runs_before_cleanup_and_preserves_timing_contract(tmp_path,monkeypatch):
    source={'md5':'1'*32,'duration':1.,'manualEnd':False};seen=[]
    def fake_run(command,**kwargs):
        Path(command[-1]).write_bytes(array('f',[.5,-.5]*16000).tobytes())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(hard.subprocess,'run',fake_run)
    def observer(path,timing):
        assert path.exists();seen.append(closing.observe_tail(path,timing))
    timing=hard.measure_source_timing(tmp_path/'source.mp3',source,'unused',tmp_path,observer=observer)
    assert len(seen)==1 and timing['decodedDuration']==1
    assert not (tmp_path/'hard-source-timing.f32').exists()


def test_entry_only_failure_keeps_a_confirmed_exit():
    old,source,previous,evidence,timing,tail=fixture()
    previous.update(status='needs-review',entryReviewOnly=True,row={**old,'end':70,'fadeOut':.3},
        exit={'method':'digital-silence','time':70.,'fade':.3,'fadeStart':69.7,
              'regionStart':69.,'regionEnd':71.,'maxRms':.00001,'relativeMax':.0001})
    assert closing.preserve_existing_exit(previous)


def test_asr_only_instrumental_exit_still_requires_model_review_or_source_completion(monkeypatch):
    monkeypatch.setattr(closing,'exit_error',lambda item:None)
    for method in ('reobserved-instrumental-fade','instrumental-fade'):
        assert not closing.preserve_existing_exit({'exit':{'method':method}})


def test_current_clip_lineage_is_derived_from_clip_record_not_display_title():
    old,source,previous,evidence,timing,tail,lineage=clip_fixture()
    db=sqlite3.connect(':memory:');db.execute('create table web_assets(id,track_key,duration,md5)')
    db.execute('create table audio_clips(track_key,result)')
    db.execute('insert into web_assets values(?,?,?,?)',(source['id'],source['track_key'],source['duration'],source['md5']))
    db.execute('insert into web_assets values(?,?,?,?)',('b'*24,'youtube:abcdefghijk',241.3714,'2'*32))
    db.execute('insert into audio_clips values(?,?)',(source['track_key'],json.dumps({'clip':{'start':0.,'end':85.,'original_md5':'2'*32}})))
    assert closing.clip_lineage(db,source['id'])==lineage
    assert closing.clip_lineage(db,'b'*24) is None
    db.close()
