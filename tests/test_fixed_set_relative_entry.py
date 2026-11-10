"""Synthetic complete raw contracts; no decoder/model or real-audio verdict."""
import copy,json,math
import pytest
from aidj import fixed_set_relative_entry as relative
from aidj.fixed_set_entry_review import fingerprint,selection_error as public_gate
from aidj.fixed_set_soft_entry import digest
from aidj.fixed_set_vocal_review import gain_db
from tests.test_fixed_set_soft_entry import fixture as soft_fixture


def fixture():
 song,vocal,_,words,coverage=soft_fixture();point=80.;pin=relative.rms_identity(song,point)
 begin,end=pin['sourceOffsetSeconds'],pin['requestedEndSeconds'];n=600
 normalized=[.1 if 285<=i<305 else .3 for i in range(n)]
 rms={'version':1,'kind':relative.RMS_KIND,'identity':pin,'cacheKey':digest(pin),
  'sourceOriginalMD5Verified':True,'sourcePCMInputSHA256':'c'*64,'normalizedInputSHA256':'d'*64,
  'inputHashProvenance':'native-recorded-source-and-normalized-interleaved-float32-PCM-SHA256',
  'audioRetained':False,'UVRInferred':False,'ASRInferred':False,'SileroInferred':False,
  'channelsAveragedBeforeRMS':False,'zeroPaddingApplied':False,
  'decode':{'method':'ffmpeg-xerror-canonical6s-stereo-f32le','sampleRate':44100,'channels':2,
   'sourceOffsetSeconds':begin,'requestedEndSeconds':end,'actualEndSeconds':end,'pcmFrames':264600,'pcmBytes':2116800},
  'rms':{'method':'independent-stereo-channel-441-sample-RMS','channelPolicy':relative.RMS_CHANNEL_POLICY,
   'normalizationPolicy':relative.NORMALIZATION,'frameHopSeconds':.01,'hopSamples':441,'lastFramePartial':False,
   'combinedStereoDefinition':'sqrt((meanL2+meanR2)/2)-owner-compatible-diagnostic-not-primary-MAX-channel',
   'frameStartSeconds':[begin+i*.01 for i in range(n)],'frameEndSeconds':[begin+(i+1)*.01 for i in range(n)],
   'leftRms':list(normalized),'rightRms':list(normalized),'maxChannelRms':list(normalized),'combinedStereoRms':list(normalized)}}
 # A loud separated vocal is intentional: this tier has NO absolute voice
 # ceiling. Its uncertainty is declared; the real calibration is external.
 gain=10**(gain_db(song)/20);vocal['vocalRms']=[.05]*2000
 vocal['mixRms']=[(.1 if 79.85-1e-7<=70+i*.01<80.05-1e-7 else .3)/gain for i in range(2000)]
 vocal['vocalToMixDb']=[20*math.log10(v/m)for v,m in zip(vocal['vocalRms'],vocal['mixRms'])]
 return song,vocal,rms,words,coverage


def check(data,**kwargs):
 song,vocal,rms,words,coverage=data
 return relative.check_entry(vocal,rms,80.,song,words,coverage,**kwargs)


def checkpoint(data):
 song,vocal,rms,words,coverage=data;proof=check(data);assert proof['accepted']
 item={'trackId':song['id'],'md5':song['md5'],'status':'ready','entryReviewOnly':False,'entryReviewStatus':'ready',
  'entryReviewVersion':3,'row':{'trackId':song['id'],'md5':song['md5'],'start':80.,'end':140.,'fadeIn':proof['fadeInSeconds'],
    'fadeOut':.8,'rate':1,'transitionKind':'clean-cut','transitionSeconds':0},
  'observedWords':copy.deepcopy(words),'observedCoverage':copy.deepcopy(coverage),'entryProof':proof}
 item['entryReviewInputHash']=fingerprint(item,song,[]);return item


def write(data,folder):
 folder.mkdir();(folder.parent/'mix-rms-probes').mkdir()
 (folder/(data[1]['cacheKey']+'.json')).write_text(json.dumps(data[1]))
 (folder.parent/'mix-rms-probes'/(data[2]['cacheKey']+'.json')).write_text(json.dumps(data[2]))


def test_distinct_combined_relative_tier_is_approximate_and_never_calls_vad_or_voice_silence():
 data=fixture();before=copy.deepcopy(data);proof=check(data)
 assert proof['accepted']and proof['method']=='relative-stereo-valley-entry'
 assert proof['stereoValleyDb']==pytest.approx(20*math.log10(3))
 assert proof['rmsVariant']=='combinedStereoRms'and not proof['MAXChannelVariantUsed']
 assert proof['nextObservedWordConfident']is False and proof['nextObservedWord']==[80.4,81.,False]
 assert proof['fadeInSeconds']==.05
 for key in ['verifiedWordBoundary','naturalSilenceClaimed','voiceAbsenceProven','ASRWordsErased','asrOverrideApplied','VADCapApplied','absoluteVocalOrMixCeilingApplied']:assert proof[key]is False
 assert data==before


@pytest.mark.parametrize('word',[[79.99,80.01,False],[79.99,80.01,True]])
def test_every_confidence_word_crossing_vetoes_a_deep_real_stereo_dip(word):
 data=fixture();data[3].append(word)
 assert check(data)['reason']=='known-word-crosses-entry-guard'and data[3][-1]==word


def test_required_future_word_is_never_invented_and_far_lowconfidence_future_remains_uncertain():
 data=fixture();data[3][:]=[[0.,79.7,True]]
 assert check(data)['reason']=='no-observed-future-word'
 data[3].append([95.,96.,False]);proof=check(data)
 assert proof['accepted']and proof['nextObservedWordDelaySeconds']==15. and proof['nextObservedWordConfident']is False
 data[3].append([95.,96.,True]);assert check(data)['nextObservedWordConfident']is False


@pytest.mark.parametrize('mutation',[
 lambda d:d[4].clear(),
 lambda d:d[3].__setitem__(1,[80.09,81.,False]),
 lambda d:d[0].update(lufs=-20.),
 lambda d:d[2]['identity'].update(channels=1),
 lambda d:d[2].update(channelsAveragedBeforeRMS=True),
 lambda d:d[2].update(zeroPaddingApplied=True),
 lambda d:d[2]['rms']['maxChannelRms'].__setitem__(285,0.),
 lambda d:d[2]['rms']['combinedStereoRms'].__setitem__(285,0.),
 lambda d:d[2]['rms']['frameStartSeconds'].__setitem__(285,80.),
 lambda d:d[2]['decode'].update(pcmFrames=1),
 lambda d:d[2].update(sourcePCMInputSHA256='missing'),
])
def test_source_gain_frame_channel_hash_and_actual_observation_guards_fail_closed(mutation):
 data=fixture();mutation(data);assert not check(data)['accepted']


def set_rms(data,index,left,right):
 rms=data[2]['rms'];rms['leftRms'][index]=left;rms['rightRms'][index]=right
 rms['maxChannelRms'][index]=max(left,right);rms['combinedStereoRms'][index]=math.sqrt((left*left+right*right)/2)


def test_body_MAX_and_both_neighbor_p75_are_required_not_a_favorable_average():
 data=fixture();set_rms(data,300,.4,.4)
 assert check(data)['reason']=='relative-stereo-valley-below3dB'
 data=fixture()
 for i in range(335,400):set_rms(data,i,.1,.1)
 assert check(data)['reason']=='relative-stereo-valley-below3dB'


def test_measured_first_vocal_attack_and_word_preroll_cap_the_real_fade():
 data=fixture();v=data[1];i=round((80.08-v['sourceOffsetSeconds'])/.01)
 v['vocalRms'][i]=.2;v['vocalToMixDb'][i]=20*math.log10(.2/v['mixRms'][i])
 assert check(data)['reason']=='observed-voice-preroll-under100ms'
 data=fixture();assert check(data,rejected=[80.2])['reason']=='owner-rejected-entry'
 data[1]['vocalRms']=[0.]*2000;data[1]['vocalToMixDb']=[20*math.log10(1e-8/max(m,1e-8))for m in data[1]['mixRms']]
 assert check(data)['reason']=='observed-voice-preroll-under100ms'


def test_public_gate_reloads_both_exact_caches_and_current_words_proofs_and_version(tmp_path):
 data=fixture();item=checkpoint(data);folder=tmp_path/'work'/'vocal-evidence';folder.parent.mkdir();write(data,folder)
 (tmp_path/'dj1-curation').mkdir();(tmp_path/'dj1-curation'/'owner-rejected.json').write_text('{}')
 assert public_gate(item,data[0],evidence_folder=folder)is None
 for mutation in [lambda p:p.update(version=2),lambda p:p['parameters'].update(minimumStereoValleyDb=0.),
  lambda p:p.update(rmsVariant='maxChannelRms'),lambda p:p.update(verifiedWordBoundary=True),
  lambda p:p.update(stereoBodyMaxDbfs=-100.),lambda p:p.update(nextObservedWordConfident=True)]:
  wrong=copy.deepcopy(item);mutation(wrong['entryProof']);assert public_gate(wrong,data[0],evidence_folder=folder)
 wrong=copy.deepcopy(item);wrong['observedWords'].append([79.99,80.01,False]);wrong['entryReviewInputHash']=fingerprint(wrong,data[0],[])
 assert public_gate(wrong,data[0],evidence_folder=folder)=='relative-entry-known-word-crosses-entry-guard'
 changed=copy.deepcopy(data[2]);set_rms([None,None,changed],300,.5,.5)
 (folder.parent/'mix-rms-probes'/(changed['cacheKey']+'.json')).write_text(json.dumps(changed))
 assert public_gate(item,data[0],evidence_folder=folder)


@pytest.mark.parametrize('field',['decode','rms','identity'])
def test_malformed_nested_raw_mappings_reject_without_throwing(field):
    data=fixture();data[2][field]=[]
    assert not check(data)['accepted']


@pytest.mark.parametrize('field',['row','entryProof'])
def test_malformed_checkpoint_mappings_reject_without_throwing(tmp_path,field):
    data=fixture();item=checkpoint(data);item[field]=[]
    assert relative.selection_error(item,data[0],evidence_folder=tmp_path,rejected=[])


@pytest.mark.parametrize('kind,size',[('vocal',2*1024*1024+1),('rms',1024*1024+1),('vocal',0),('rms',0)])
def test_raw_evidence_size_is_bounded_before_json_parsing(tmp_path,monkeypatch,kind,size):
    data=fixture();item=checkpoint(data);folder=tmp_path/'work'/'vocal-evidence'
    folder.parent.mkdir();write(data,folder)
    path=(folder/(data[1]['cacheKey']+'.json') if kind=='vocal' else
        folder.parent/'mix-rms-probes'/(data[2]['cacheKey']+'.json'))
    with path.open('wb') as stream:
        stream.truncate(size)
    def unexpected_parse(*args,**kwargs):
        raise AssertionError('raw evidence parsed before size validation')
    monkeypatch.setattr(relative,'read_json',unexpected_parse)
    assert relative.selection_error(item,data[0],evidence_folder=folder,rejected=[])=='relative-entry-evidence-size'
