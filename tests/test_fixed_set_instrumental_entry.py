import copy
import hashlib
import json
import math

import pytest

from aidj import fixed_set_instrumental_entry as review
from aidj.fixed_set_entry_review import selection_error as public_gate
from aidj.fixed_set_vocal_review import VERSION as UVR_VERSION
from aidj.vocal_probe import MODEL_SHA256
from aidj.vocal_speech import INPUT_RATE,WINDOW,MODEL_RATE

MD5='1'*32


def fixtures():
    song={'id':'a'*24,'md5':MD5,'bytes':1000,'duration':180.,'lo':0.,'hi':180.,'lufs':-16.,'peak':-2.,
          'loudnessMethod':'ebu-r128-v1','loudnessStatus':'complete','manualStart':False}
    a,b=70.,90.;n=2000
    key=hashlib.sha256(json.dumps([UVR_VERSION,MD5,a,b,MODEL_SHA256],separators=(',',':')).encode()).hexdigest()
    vocal={'md5':MD5,'modelSha256':MODEL_SHA256,'cacheKey':key,'sourceOffsetSeconds':a,
        'requestedEndSeconds':b,'durationSeconds':20.,'frameHopSeconds':.01,'warnings':[],
        'vocalRms':[1e-6]*n,'mixRms':[.3]*n,'instrumentalRms':[.3]*n,
        'vocalToMixDb':[20*math.log10(1e-6/.3)]*n}
    mix=mix_fixture(song,a,b)
    words=[[0.,78.,True],[79.,83.,True],[141.5,180.,True]]
    row={'trackId':song['id'],'md5':MD5,'start':80.,'end':140.,'fadeIn':.1,'fadeOut':.8,
         'rate':1,'transitionSeconds':0,'transitionKind':'clean-cut'}
    point={'time':140.,'fade':.8,'fadeStart':139.2,'regionStart':139.,'regionEnd':141.5,
        'maxRms':1e-4,'relativeMax':.001,'method':'strict-quiet','coverageComplete':True,
        'absoluteSilence':True,'wordPadding':.3}
    item={'trackId':song['id'],'md5':MD5,'row':row,'exit':point,'status':'needs-review',
        'entryReviewOnly':True,'observedWords':words,'observedCoverage':[[0.,180.]],
        'entryVocalEvidence':[{'cacheKey':key}],'stereoEvidence':{'old':True},'stereoVersion':1,
        'sourceEnding':{'untouched':True}}
    return item,song,vocal,mix


def mix_fixture(song,a,b):
    n=round((b-a)*INPUT_RATE);real=math.ceil(n*160/441);padded=math.ceil((real+64)/WINDOW)*WINDOW
    frames=math.ceil(n*160/(441*WINDOW));starts=[a+i*WINDOW/MODEL_RATE for i in range(frames)]
    ends=[min(a+(i+1)*WINDOW/MODEL_RATE,a+n/INPUT_RATE) for i in range(frames)]
    identity=review.mix_identity(song,a,b)
    return {'version':1,'cacheKey':review.digest(identity),'identity':identity,'sourceMD5':MD5,
        'sourceOriginalMD5Verified':True,'sourceDuration':song['duration'],'sourceRange':[song['lo'],song['hi']],
        'playbackGainDb':review.gain_db(song),
        'playbackProfile':{k:song.get(k) for k in ('lufs','peak','loudnessMethod','loudnessStatus')},
        'normalizedInputLayout':'channel-major-float32-little','normalizedInputSHA256':'c'*64,
        'uvrInferred':False,'stemInput':False,'channelsAveraged':False,
        'decode':{'pcmFrames':n,'pcmBytes':n*8,'method':'ffmpeg-xerror-20s-stereo-f32le',
            'sampleRate':INPUT_RATE,'channels':2,'sourceOffsetSeconds':a,'actualEndSeconds':a+n/INPUT_RATE,'pcmSHA256':'d'*64},
        'speech':{'method':'silero-v5-stereo-speech-probability','version':1,'modelArtifactsVerified':True,
            'modelSHA256':identity['modelSHA256'],'modelWrapperVersion':'faster-whisper-1.1.1',
            'sourceOffsetSeconds':a,'sourceEndSeconds':a+n/INPUT_RATE,'durationSeconds':n/INPUT_RATE,
            'inputSampleRate':INPUT_RATE,'inputSamples':n,'modelSampleRate':MODEL_RATE,
            'resampledSamples':real,'modelPaddedSamples':padded,'frameHopSeconds':WINDOW/MODEL_RATE,
            'paddingFramesExcluded':padded//WINDOW-frames,'rawProbabilityShape':[2,padded//WINDOW],
            'frameStartSeconds':starts,'frameEndSeconds':ends,
            'leftSpeechProbability':[.001]*frames,'rightSpeechProbability':[.001]*frames,
            'maxSpeechProbability':[.001]*frames}}


def check(item,song,vocal,mix,point=80.,**kw):
    return review.check_joint(vocal,mix,point,song,item['observedWords'],item['observedCoverage'],**kw)


def test_complete_joint_proof_explicitly_overrides_a_retained_word_without_erasing_it():
    item,song,vocal,mix=fixtures();proof=check(item,song,vocal,mix)
    assert proof['accepted'] and proof['retainedASRCrossings']==[[79.,83.,True]]
    assert proof['asrOverrideReason']=='prolonged-joint-instrumental-interior'
    assert proof['proofStart']==79.25 and proof['proofEnd']==81.25 and proof['fadeInSeconds']==.15
    assert not proof['verifiedWordBoundary'] and not proof['naturalSilenceClaimed'] and not proof['ASRWordsErased']
    assert item['observedWords'][1]==[79.,83.,True]


def test_one_loud_10ms_vocal_frame_or_insufficient_two_second_quiet_is_a_veto():
    item,song,vocal,mix=fixtures()
    vocal['vocalRms'][1000]=.01;vocal['vocalToMixDb'][1000]=20*math.log10(.01/.3)
    assert not check(item,song,vocal,mix)['accepted']
    vocal['vocalRms']=[.002]*2000;vocal['vocalToMixDb']=[20*math.log10(.002/.3)]*2000
    assert not check(item,song,vocal,mix)['accepted']


def test_either_actual_mix_channel_can_veto_and_max_summary_cannot_hide_it():
    item,song,vocal,mix=fixtures();i=round((80-70)/.032)
    mix['speech']['rightSpeechProbability'][i]=.5;mix['speech']['maxSpeechProbability'][i]=.5
    assert check(item,song,vocal,mix)['reason']=='raw-mix-speech-at-entry'
    mix['speech']['maxSpeechProbability'][i]=.001
    assert check(item,song,vocal,mix)['reason']=='invalid-raw-mix-speech-evidence'


def test_unknown_asr_coverage_never_becomes_an_instrumental_exception():
    item,song,vocal,mix=fixtures();item['observedCoverage']=[[0,80.9]]
    assert not check(item,song,vocal,mix)['accepted']


def test_uvr_context_vad_warmup_and_end_guard_are_required_for_the_whole_proof():
    item,song,vocal,mix=fixtures()
    assert not check(item,song,vocal,mix,point=71.7)['accepted']  # VAD warmup extends to71.
    assert not check(item,song,vocal,mix,point=88.6)['accepted']
    assert check(item,song,vocal,mix,point=71.8)['accepted']


def test_only_near_zero_vocal_warning_is_allowed_after_independent_mix_corroboration():
    item,song,vocal,mix=fixtures();vocal['warnings']=['near-zero-vocal-output']
    assert check(item,song,vocal,mix)['accepted']
    vocal['warnings'].append('vocal-output-exceeds-mixture')
    assert not check(item,song,vocal,mix)['accepted']


@pytest.mark.parametrize('change',[{'md5':'2'*32},{'bytes':1001},{'duration':181.},{'lo':1.},
    {'hi':170.},{'lufs':-25.},{'peak':-1.},{'loudnessStatus':'pending'}])
def test_current_source_range_and_playback_profile_must_match_recorded_mix(change):
    _,song,_,mix=fixtures();assert review.validate_mix(mix,{**song,**change}) is None


def test_stem_vad_wrong_model_nonfinite_or_forged_frame_coordinates_cannot_substitute_raw_mix():
    _,song,_,mix=fixtures()
    for mutate in (lambda d:d.update(stemInput=True),
                   lambda d:d['speech'].update(modelArtifactsVerified=False),
                   lambda d:d['speech']['modelSHA256'].update(silero_encoder_v5='wrong'),
                   lambda d:d['speech']['frameStartSeconds'].__setitem__(2,90.),
                   lambda d:d['speech']['leftSpeechProbability'].__setitem__(2,float('nan')),
                   lambda d:d.update(normalizedInputSHA256='missing')):
        changed=copy.deepcopy(mix);mutate(changed);assert review.validate_mix(changed,song) is None


def test_negative_owner_veto_span_and_approved_original_entry_are_preserved():
    item,song,vocal,mix=fixtures()
    assert not check(item,song,vocal,mix,rejected=[80.2])['accepted']
    assert review.select(item,song,[vocal],[mix],owner=[80]) is None
    item['row']['end']=180.
    assert all(45<=180-c['point']<=100 for c in review.candidates(item,song,[vocal],item['observedCoverage']))


def test_selected_incoming_point_preserves_outgoing_json_source_ending_and_asr_exactly():
    item,song,vocal,mix=fixtures();result=review.select(item,song,[vocal],[mix])
    assert result['status']=='ready' and result['row']['fadeIn']==.15
    assert result['exit']==item['exit'] and result['sourceEnding']==item['sourceEnding']
    assert result['row']['end']==item['row']['end'] and result['row']['fadeOut']==item['row']['fadeOut']
    assert result['observedWords']==item['observedWords'] and result['observedCoverage']==item['observedCoverage']
    assert 'stereoEvidence' not in result and 'stereoVersion' not in result and result['entryShiftNeedsStereoRecheck']


def test_public_gate_reloads_actual_raw_arrays_and_current_negative_owner_map(tmp_path):
    item,song,vocal,mix=fixtures();work=tmp_path/'research'/'quiet';folder=work/'vocal-evidence';folder.mkdir(parents=True)
    mixes=work/'mix-vad-probes';mixes.mkdir();curation=work.parent/'dj1-curation';curation.mkdir()
    (curation/'owner-rejected.json').write_text('{}')
    rawfile=folder/(vocal['cacheKey']+'.json');rawfile.write_text(json.dumps(vocal))
    mixfile=mixes/(mix['cacheKey']+'.json');mixfile.write_text(json.dumps(mix))
    result=review.select(item,song,[vocal],[mix]);assert public_gate(result,song,evidence_folder=folder) is None
    changed=copy.deepcopy(result);changed['entryProof']['normalizedVocalMaxDbfs']-=1
    assert public_gate(changed,song,evidence_folder=folder)=='instrumental-entry-proof-mismatch'
    (curation/'owner-rejected.json').write_text(json.dumps({item['trackId']:[80]}))
    assert public_gate(result,song,evidence_folder=folder)=='instrumental-entry-unproven-two-second-vocal-quiet'
    (curation/'owner-rejected.json').write_text('{}')
    mix['speech']['rightSpeechProbability'][315]=.8;mix['speech']['maxSpeechProbability'][315]=.8
    mixfile.write_text(json.dumps(mix))
    assert public_gate(result,song,evidence_folder=folder)=='instrumental-entry-raw-mix-speech-at-entry'
