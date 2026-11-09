"""Optional, bounded UVR/MDX vocal evidence for difficult DJ boundaries.

This module never downloads a model, rewrites an original, or stores stems. Use
only short excerpts in a separate memory/time-limited process. The weights below
are publicly distributed by UVR, but have no independently stated weight license;
KUIELab's MIT code license must not be represented as a weight license.

Preprocessing references (UVR's ONNX path, not its PyTorch application wrapper):
https://github.com/Anjok07/ultimatevocalremovergui/blob/master/separate.py
https://github.com/Anjok07/ultimatevocalremovergui/blob/master/lib_v5/tfc_tdf_v3.py
https://raw.githubusercontent.com/TRvlvr/application_data/main/mdx_model_data/model_data.json
"""
import hashlib
import math
from pathlib import Path

MODEL_NAME = 'UVR_MDXNET_1_9703.onnx'
MODEL_URL = ('https://github.com/TRvlvr/model_repo/releases/download/'
             'all_public_uvr_models/' + MODEL_NAME)
MODEL_BYTES = 29_704_436  # Official GitHub release asset metadata.
MODEL_SHA256 = '229ad3bb96a037e89d8ed86732d6d3675856e6a07c3e3f02896eac01ec7ee4be'
# SHA256 published by this mirror; an official-asset download must match it too.
MODEL_SHA256_SOURCE = 'https://huggingface.co/nomadkaraoke/public_uvr_models/blob/main/' + MODEL_NAME
MODEL_UVR_HASH = 'a3cd63058945e777505c01d2507daf37'
SAMPLE_RATE = 44_100
MAX_SECONDS = 30
N_FFT = 6144
HOP = 1024
DIM_F = 2048
DIM_T = 256
COMPENSATE = 1.03


def verify_model(path):
    """Verify a private, already downloaded artifact without loading it in RAM."""
    path = Path(path)
    if path.stat().st_size != MODEL_BYTES:
        raise ValueError('Vocal probe model size does not match the pinned artifact')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        stream.seek(-10000 * 1024, 2)  # UVR's parameter hash is of the last10000KiB.
        tail = hashlib.md5()
        while block := stream.read(64 * 1024):
            tail.update(block)
    if digest != MODEL_SHA256 or tail.hexdigest() != MODEL_UVR_HASH:
        raise ValueError('Vocal probe model checksum does not match the pinned artifact')
    return path


def _fft():
    # scipy preserves float32 efficiently; numpy is sufficient for pure tests
    # and remains an optional fallback, without adding production dependencies.
    try:
        from scipy.fft import rfft, irfft
    except ImportError:
        from numpy.fft import rfft, irfft
    return rfft, irfft


def _stft(wave, n_fft, hop, dim_f):
    """UVR/Torch-compatible periodic-Hann, centered-reflect, complex stereo STFT."""
    import numpy as np
    rfft, _ = _fft()
    window = (.5 - .5 * np.cos(2 * np.pi * np.arange(n_fft) / n_fft)).astype(np.float32)
    padded = np.pad(wave, ((0, 0), (n_fft // 2, n_fft // 2)), mode='reflect')
    frames = np.lib.stride_tricks.sliding_window_view(padded, n_fft, axis=1)[:, ::hop]
    spectrum = rfft(frames * window, axis=-1).astype(np.complex64)
    # Real/imag channel order matches [left-real,left-imag,right-real,right-imag].
    spectrum = spectrum.transpose(0, 2, 1)[:, :dim_f]
    return np.stack((spectrum.real, spectrum.imag), axis=1).reshape(1, 4, dim_f, spectrum.shape[-1]).astype(np.float32)


def _istft(packed, n_fft, hop, length):
    """Inverse STFT with exact centering removed; output sample0 is input sample0."""
    import numpy as np
    _, irfft = _fft()
    channels = packed.reshape(2, 2, packed.shape[-2], packed.shape[-1])
    spectrum = np.zeros((2, n_fft // 2 + 1, packed.shape[-1]), np.complex64)
    spectrum[:, :packed.shape[-2]] = channels[:, 0] + 1j * channels[:, 1]
    frames = irfft(spectrum.transpose(0, 2, 1), n=n_fft, axis=-1).astype(np.float32)
    window = (.5 - .5 * np.cos(2 * np.pi * np.arange(n_fft) / n_fft)).astype(np.float32)
    size = hop * (frames.shape[1] - 1) + n_fft
    wave, denominator = np.zeros((2, size), np.float32), np.zeros(size, np.float32)
    for i in range(frames.shape[1]):
        start = i * hop
        wave[:, start:start+n_fft] += frames[:, i] * window
        denominator[start:start+n_fft] += window * window
    np.divide(wave, denominator, out=wave, where=denominator > 1e-10)
    trim = n_fft // 2
    result = wave[:, trim:trim+length]
    if result.shape[-1] != length:
        raise ValueError('Unexpected vocal probe inverse-STFT length')
    return result


def _separate(wave, predict, *, n_fft=N_FFT, hop=HOP, dim_f=DIM_F,
              dim_t=DIM_T, compensate=COMPENSATE, zero_low_bins=True):
    """Serial fixed-size batch1 inference; no tensors from other chunks are retained."""
    import numpy as np
    trim = n_fft // 2
    chunk = hop * (dim_t - 1)
    step = chunk - 2 * trim
    if step <= 0:
        raise ValueError('Invalid vocal probe chunk settings')
    pad = step + trim - wave.shape[-1] % step
    mixture = np.pad(wave, ((0, 0), (trim, pad)))
    wanted = trim + wave.shape[-1]
    result, denominator = np.zeros((2, wanted), np.float32), np.zeros(wanted, np.float32)
    for start in range(0, wanted, step):
        actual = min(chunk, mixture.shape[-1] - start)
        part = mixture[:, start:start+actual]
        if actual < chunk:
            part = np.pad(part, ((0, 0), (0, chunk-actual)))
        packed = _stft(part, n_fft, hop, dim_f)
        if packed.shape != (1, 4, dim_f, dim_t):
            raise ValueError('Unexpected vocal probe STFT shape')
        if zero_low_bins:
            packed[:, :, :3] = 0
        prediction = np.asarray(predict(packed))
        if prediction.shape != packed.shape or prediction.dtype != np.float32 or not np.isfinite(prediction).all():
            raise ValueError('Invalid vocal probe model output')
        separated = _istft(prediction, n_fft, hop, chunk)
        window = np.hanning(actual).astype(np.float32)
        use = min(actual, wanted - start)
        result[:, start:start+use] += separated[:, :use] * window[:use]
        denominator[start:start+use] += window[:use]
    np.divide(result, denominator, out=result, where=denominator > 1e-10)
    return result[:, trim:wanted] * compensate


def _load_model(path):
    import onnxruntime as ort
    path = verify_model(path)
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.enable_cpu_mem_arena = options.enable_mem_pattern = False
    options.add_session_config_entry('session.intra_op.allow_spinning', '0')
    session = ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or inputs[0].type != 'tensor(float)' or len(inputs[0].shape) != 4:
        raise ValueError('Unexpected vocal probe ONNX input')
    shape = inputs[0].shape
    if any(isinstance(actual, int) and actual != expected for actual, expected in zip(shape, (1, 4, DIM_F, DIM_T))):
        raise ValueError('Vocal probe ONNX input dimensions do not match its preprocessing')
    if len(outputs) != 1:
        raise ValueError('Unexpected vocal probe ONNX output')
    return lambda packed: session.run([outputs[0].name], {inputs[0].name: packed})[0]


def _evidence(wave, vocals, sr, center_seconds):
    import numpy as np
    hop = int(round(sr * .01))
    count = math.ceil(wave.shape[-1] / hop)
    def rms(samples):
        padded = np.pad(samples, ((0, 0), (0, count * hop - samples.shape[-1])))
        squares = np.square(padded.reshape(2, count, hop), dtype=np.float64)
        samples = np.minimum(hop, wave.shape[-1] - np.arange(count) * hop)
        return np.sqrt(np.sum(squares, axis=(0, 2)) / (2 * samples))
    mix_rms, vocal_rms, instrumental_rms = rms(wave), rms(vocals), rms(wave - vocals)
    # Ratios are evidence, not calibrated probabilities. Near-zero mix frames are
    # flagged separately; a ratio by itself would be meaningless there.
    ratio = 20 * np.log10(np.maximum(vocal_rms, 1e-8) / np.maximum(mix_rms, 1e-8))
    mix_level = float(np.sqrt(np.mean(wave ** 2, dtype=np.float64)))
    vocal_level = float(np.sqrt(np.mean(vocals ** 2, dtype=np.float64)))
    issues = []
    if mix_level > 1e-4 and vocal_level < 1e-7:
        issues.append('near-zero-vocal-output')
    if vocal_level > max(1e-4, mix_level * 2):
        issues.append('vocal-output-exceeds-mixture')
    return {'method': 'uvr-mdx-vocal-evidence-v1', 'model': MODEL_NAME,
            'modelSha256': MODEL_SHA256, 'sampleRate': sr,
            'durationSeconds': wave.shape[-1] / sr, 'centerSeconds': center_seconds,
            'frameHopSeconds': hop / sr, 'frameStartSeconds': 0,
            'lastFrameSeconds': (wave.shape[-1] - (count-1)*hop) / sr,
            'centerNearExcerptEdge': min(center_seconds, wave.shape[-1]/sr-center_seconds) < .25,
            'mixRms': mix_rms.tolist(), 'vocalRms': vocal_rms.tolist(),
            'instrumentalRms': instrumental_rms.tolist(), 'vocalToMixDb': ratio.tolist(),
            'mixActive': (mix_rms >= 1e-4).tolist(),
            'vocalActive': ((vocal_rms >= 1e-4) & (ratio >= -35)).tolist(),
            'warnings': issues, 'verifiedWordBoundary': False}


def probe_vocals(pcm, sr=SAMPLE_RATE, model_path=None, *, center_seconds=None):
    """Return 10ms numeric vocal/mix/instrumental evidence for at most30s.

    pcm must be a numpy stereo float32 array, shape(2,samples) or(samples,2).
    Callers decide whether a candidate is acceptable; this is not word alignment
    or proof of a good cut. Existing ASR evidence and source-range checks remain
    required. Peak ONNX memory must be measured in the bounded pilot process.
    """
    import numpy as np
    if sr != SAMPLE_RATE or not isinstance(pcm, np.ndarray) or pcm.ndim != 2 or pcm.dtype != np.float32:
        raise ValueError('Vocal probe requires stereo float32 PCM at44100Hz')
    if pcm.shape[0] == 2:
        wave = pcm
    elif pcm.shape[1] == 2:
        wave = pcm.T
    else:
        raise ValueError('Vocal probe requires exactly two channels')
    if not sr // 10 <= wave.shape[-1] <= sr * MAX_SECONDS or not np.isfinite(wave).all():
        raise ValueError('Vocal probe excerpt must contain0.1–30s of finite PCM')
    duration = wave.shape[-1] / sr
    center = duration / 2 if center_seconds is None else center_seconds
    if not isinstance(center, (int, float)) or not math.isfinite(center) or not 0 <= center <= duration:
        raise ValueError('Invalid vocal probe center')
    if model_path is None:
        raise ValueError('An already downloaded private ONNX model path is required')
    prediction = _load_model(model_path)
    vocals = _separate(wave, prediction)
    return _evidence(wave, vocals, sr, center)
