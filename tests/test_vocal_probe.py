"""No model downloads or inference: validate UVR packing, exact time origin and guards."""
import pytest
from aidj import vocal_probe as probe


@pytest.fixture
def np():
    return pytest.importorskip('numpy')


def test_vocal_model_is_explicit_pinned_and_never_downloaded(tmp_path):
    artifact=tmp_path/'model.onnx';artifact.write_bytes(b'not a model')
    with pytest.raises(ValueError,match='size'):
        probe.verify_model(artifact)
    assert probe.MODEL_URL.endswith('/all_public_uvr_models/UVR_MDXNET_1_9703.onnx')
    assert len(probe.MODEL_SHA256)==64 and probe.MODEL_BYTES<32*1024**2


def test_centered_stft_real_imag_stereo_order_and_inverse_sample_origin(np):
    rng=np.random.default_rng(42)
    wave=rng.normal(0,.1,(2,960)).astype(np.float32)
    packed=probe._stft(wave,256,64,129)
    assert packed.shape==(1,4,129,16)
    window=(.5-.5*np.cos(2*np.pi*np.arange(256)/256)).astype(np.float32)
    expected=np.fft.rfft(wave[:,128:384]*window,axis=-1)
    np.testing.assert_allclose(packed[0,0,:,4],expected[0].real,atol=2e-6)
    np.testing.assert_allclose(packed[0,1,:,4],expected[0].imag,atol=2e-6)
    np.testing.assert_allclose(packed[0,2,:,4],expected[1].real,atol=2e-6)
    np.testing.assert_allclose(packed[0,3,:,4],expected[1].imag,atol=2e-6)
    np.testing.assert_allclose(probe._istft(packed,256,64,960),wave,atol=2e-7)


def test_serial_chunks_preserve_length_phase_and_short_final_window(np):
    wave=np.zeros((2,5003),np.float32)
    wave[0,[0,703,704,705,4400,5002]]=[.1,.2,.3,.4,.5,.6]
    wave[1]=np.sin(np.arange(5003)*.27)*.1
    seen=[]
    def identity(packed):seen.append(packed.shape);return packed
    result=probe._separate(wave,identity,n_fft=256,hop=64,dim_f=129,dim_t=16,compensate=1,zero_low_bins=False)
    assert len(seen)>2 and set(seen)=={(1,4,129,16)}
    assert result.shape==wave.shape
    np.testing.assert_allclose(result,wave,atol=2e-7)
    assert int(np.argmax(result[0]))==5002


def test_probe_input_budget_and_model_output_fail_closed(np,monkeypatch):
    def no_model(*args):raise AssertionError('invalid input must fail before model loading')
    monkeypatch.setattr(probe,'_load_model',no_model)
    for wave,sr in [(np.zeros((2,20),np.float32),44100),(np.zeros((3,5000),np.float32),44100),
                    (np.zeros((2,5000),np.float64),44100),(np.zeros((2,5000),np.float32),16000),
                    (np.full((2,5000),np.nan,np.float32),44100)]:
        with pytest.raises(ValueError):probe.probe_vocals(wave,sr,'unused')
    wave=np.zeros((2,5000),np.float32)
    with pytest.raises(ValueError,match='output'):
        probe._separate(wave,lambda x:np.zeros((1,1,1,1),np.float32),n_fft=256,hop=64,dim_f=129,dim_t=16)


def test_vocal_energy_keeps_instrumental_and_flags_all_zero_output(np):
    wave=np.full((2,4410),.1,np.float32)
    silent=probe._evidence(wave,np.zeros_like(wave),44100,.05)
    assert len(silent['vocalRms'])==10 and silent['frameHopSeconds']==.01
    assert 'near-zero-vocal-output' in silent['warnings']
    assert silent['verifiedWordBoundary'] is False
    assert not any(silent['vocalActive']) and all(silent['mixActive'])
    np.testing.assert_allclose(silent['instrumentalRms'],silent['mixRms'])
    part=probe._evidence(wave,wave*.5,44100,.05)
    np.testing.assert_allclose(part['vocalToMixDb'],20*np.log10(.5),atol=1e-6)
    np.testing.assert_allclose(part['vocalRms'],part['instrumentalRms'])
    assert not part['warnings'] and all(part['vocalActive'])


def test_partial_final_frame_does_not_invent_quiet_at_cropped_eof(np):
    wave=np.full((2,442),.1,np.float32)
    result=probe._evidence(wave,wave*.5,44100,.005)
    assert len(result['vocalRms'])==2
    np.testing.assert_allclose(result['mixRms'],[.1,.1],atol=1e-8)
    np.testing.assert_allclose(result['vocalRms'],[.05,.05],atol=1e-8)
    assert result['lastFrameSeconds']==1/44100
    assert result['centerNearExcerptEdge'] is True


def test_cpu_threads_default_and_explicit_supported_values(monkeypatch):
    monkeypatch.delenv('AIDJ_VOCAL_CPU_THREADS',raising=False)
    assert probe.cpu_threads()==1
    for value in ('1','2'):
        monkeypatch.setenv('AIDJ_VOCAL_CPU_THREADS',value)
        assert probe.cpu_threads()==int(value)


@pytest.mark.parametrize('value',['','0','3','-1','1.0',' 2','2 ','+2','secret-like-invalid'])
def test_cpu_threads_reject_invalid_before_optional_runtime_import_or_model_allocation(monkeypatch,value):
    import sys
    from types import SimpleNamespace
    def forbidden(*args,**kwargs):raise AssertionError('model allocation must not run')
    monkeypatch.setenv('AIDJ_VOCAL_CPU_THREADS',value)
    monkeypatch.setitem(sys.modules,'onnxruntime',SimpleNamespace(SessionOptions=forbidden))
    with pytest.raises(ValueError,match='CPU threads must be1 or2') as error:
        probe._load_model('unused')
    assert str(error.value)=='Vocal probe CPU threads must be1 or2'


def test_opt_in_two_threads_changes_only_intra_op_and_keeps_memory_controls(monkeypatch):
    import sys
    from types import SimpleNamespace
    captured={}
    class Options:
        def add_session_config_entry(self,key,value):captured[key]=value
    class Session:
        def __init__(self,path,*,sess_options,providers):
            captured['options']=sess_options;captured['providers']=providers
        def get_inputs(self):return [SimpleNamespace(name='input',type='tensor(float)',shape=[None,4,2048,256])]
        def get_outputs(self):return [SimpleNamespace(name='output')]
        def run(self,*args):return ['numeric-output']
    fake=SimpleNamespace(SessionOptions=Options,InferenceSession=Session,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL='sequential'),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_BASIC='basic'))
    monkeypatch.setitem(sys.modules,'onnxruntime',fake)
    monkeypatch.setattr(probe,'verify_model',lambda path:path)
    monkeypatch.setenv('AIDJ_VOCAL_CPU_THREADS','2')
    predict=probe._load_model('unused');options=captured['options']
    assert options.intra_op_num_threads==2 and options.inter_op_num_threads==1
    assert options.execution_mode=='sequential' and options.graph_optimization_level=='basic'
    assert options.enable_cpu_mem_arena is False and options.enable_mem_pattern is False
    assert captured['providers']==['CPUExecutionProvider']
    assert captured['session.intra_op.allow_spinning']=='0'
    assert predict(None)=='numeric-output'
