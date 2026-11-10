"""Pure/fake-model calibration-feature tests; no ORT, decoding or downloads."""
import hashlib
import pytest
from aidj import vocal_speech as speech


@pytest.fixture
def np():
    return pytest.importorskip('numpy')


@pytest.fixture
def scipy():
    return pytest.importorskip('scipy.signal')


def test_independent_channels_padding_coverage_and_no_waveform_mutation(np,scipy):
    wave=np.full((2,4410),.2,np.float32);wave[1]*=-1
    original=wave.copy();seen={}
    def fake(audio):
        seen['shape']=audio.shape;seen['tail']=audio[:,-64:].copy()
        assert audio.dtype==np.float32 and audio.shape[1]%512==0
        np.testing.assert_allclose(audio[0,:1600],-audio[1,:1600])
        assert float(np.abs(audio[:,:1600]).max())>.1
        result=np.full((2,audio.shape[1]//512),.01,np.float32)
        result[0,:4]=[.1,.7,.2,.3];result[1,:4]=[.8,.2,.4,.1]
        audio[:]=0  # Even a mutating wrapper cannot change the caller's audio.
        result[:,4:]=1  # Pure padding must never become evidence.
        return result
    result=speech.speech_evidence(wave,12.5,model_callable=fake)
    np.testing.assert_array_equal(wave,original)
    assert not seen['tail'].any() and seen['shape']==(2,2048)
    assert result['resampledSamples']==1600
    assert result['frameStartSeconds']==pytest.approx([12.5,12.532,12.564,12.596])
    assert result['frameEndSeconds']==pytest.approx([12.532,12.564,12.596,12.6])
    assert result['maxSpeechProbability']==pytest.approx([.8,.7,.4,.3])
    assert result['lastFrameSeconds']==pytest.approx(.004)
    assert result['lastFramePartial'] and result['lastFrameIncludesPadding']
    assert not result['modelArtifactsVerified'] and not result['vocalAbsenceVerified']
    assert not result['verifiedWordBoundary'] and not result['speechThresholdApplied']
    assert result['rawProbabilityShape']==[2,4]


def test_native_wrapper_trailing_singleton_preserves_both_channels(np,scipy):
    def fake(audio):
        result=np.zeros((2,audio.shape[1]//512,1),np.float32)
        result[0,:,0]=.3;result[1,:,0]=.8
        return result
    result=speech.speech_evidence(np.zeros((2,4410),np.float32),0,model_callable=fake)
    assert result['rawProbabilityShape']==[2,4,1]
    assert result['leftSpeechProbability']==pytest.approx([.3]*4)
    assert result['rightSpeechProbability']==pytest.approx([.8]*4)
    assert result['maxSpeechProbability']==pytest.approx([.8]*4)


@pytest.mark.parametrize('layout',[(1,2,4),(2,1,4),(4,2,1),(2,4,1,1),(1,4,1)])
def test_unknown_native_layout_rejected_without_arbitrary_squeeze(np,scipy,layout):
    with pytest.raises(ValueError,match='model output'):
        speech.speech_evidence(np.zeros((2,4410),np.float32),0,
            model_callable=lambda audio:np.zeros(layout,np.float32))


def test_exact_frame_end_excludes_extra_zero_frames(np,scipy):
    def fake(audio):return np.zeros((2,audio.shape[1]//512),np.float32)
    result=speech.speech_evidence(np.zeros((2,14112),np.float32),0,model_callable=fake)
    assert result['durationSeconds']==.32 and len(result['maxSpeechProbability'])==10
    assert result['paddingFramesExcluded']==1
    assert result['lastFrameSeconds']==pytest.approx(.032)
    assert not result['lastFramePartial'] and not result['lastFrameIncludesPadding']


def test_resampling_preserves_source_time_origin_and_real_fractional_tail(np,scipy):
    wave=np.zeros((2,4411),np.float32);wave[:,2205]=[.8,-.8]
    def fake(audio):
        # Input impulse at50ms remains at50ms after rational resampling.
        assert int(np.abs(audio[0]).argmax())==800
        assert int(np.abs(audio[1]).argmax())==800
        assert audio[0,800]>0 and audio[1,800]<0
        assert not audio[:,1601:].any()
        return np.zeros((2,audio.shape[1]//512),np.float32)
    result=speech.speech_evidence(wave,5,model_callable=fake)
    assert result['resampledSamples']==1601
    assert result['durationSeconds']==4411/44100
    assert result['sourceEndSeconds']==5+4411/44100
    assert result['lastFrameSeconds']==pytest.approx(4411/44100-.096)
    assert result['frameEndSeconds'][-1]==result['sourceEndSeconds']


@pytest.mark.parametrize('offset',[-1,float('inf'),float('nan'),True,'0'])
def test_invalid_offset_rejected_before_model(np,monkeypatch,offset):
    monkeypatch.setattr(speech,'_load_model',lambda:pytest.fail('model must not load'))
    with pytest.raises(ValueError):speech.speech_evidence(np.zeros((2,4410),np.float32),offset)


def test_invalid_audio_rejected_before_model(np,monkeypatch):
    monkeypatch.setattr(speech,'_load_model',lambda:pytest.fail('model must not load'))
    for audio in [np.zeros((2,4409),np.float32),np.zeros((2,1323001),np.float32),
                  np.zeros((1,4410),np.float32),np.zeros((4410,2),np.float32),
                  np.zeros((2,4410),np.float64),np.full((2,4410),np.nan,np.float32)]:
        with pytest.raises(ValueError):speech.speech_evidence(audio,0)


@pytest.mark.parametrize('bad',['shape','nan','negative','greater','complex'])
def test_invalid_model_output_rejected(np,scipy,bad):
    def fake(audio):
        shape=(2,audio.shape[1]//512)
        if bad=='shape':return np.zeros((1,shape[1]))
        value={'nan':float('nan'),'negative':-.1,'greater':1.01,'complex':.5j}[bad]
        return np.full(shape,value)
    with pytest.raises(ValueError,match='model output'):
        speech.speech_evidence(np.zeros((2,4410),np.float32),0,model_callable=fake)


def test_local_model_pins_verify_size_and_hash_without_loading(tmp_path,monkeypatch):
    content=b'fake-small-artifact';name='fake.onnx'
    monkeypatch.setattr(speech,'MODEL_FILES',{name:{'bytes':len(content),
        'sha256':hashlib.sha256(content).hexdigest()}})
    with pytest.raises(ValueError,match='size'):speech.verify_models(tmp_path)
    path=tmp_path/name;path.write_bytes(content)
    assert speech.verify_models(tmp_path)=={name:path}
    path.write_bytes(b'x'*len(content))
    with pytest.raises(ValueError,match='checksum'):speech.verify_models(tmp_path)


def test_real_loader_verifies_local_assets_before_runtime_import(monkeypatch):
    from types import SimpleNamespace
    import importlib.metadata
    def verify(path):raise ValueError('test stopped before model allocation')
    monkeypatch.setattr(importlib.metadata,'distribution',lambda _:SimpleNamespace(
        version='1.1.1',locate_file=lambda path:path))
    monkeypatch.setattr(speech,'verify_models',verify)
    with pytest.raises(ValueError,match='before model allocation'):speech._load_model()
