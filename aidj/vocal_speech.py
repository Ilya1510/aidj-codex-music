"""Optional numeric speech features for short isolated-vocal excerpts.

No downloads, decisions, word-boundary claims or production-gate changes. Low
Silero speech probability does not prove absence of singing. Call only inside a
separate bounded calibration process; the real model is loaded lazily.

Wrapper contract: faster-whisper 1.1.1/faster_whisper/vad.py. Model SHA256 values
below were measured from the locally installed artifacts, not a remote manifest.
https://github.com/SYSTRAN/faster-whisper/blob/v1.1.1/faster_whisper/vad.py
"""
import hashlib
import math
from pathlib import Path

INPUT_RATE = 44100
MODEL_RATE = 16000
WINDOW = 512
MODEL_FILES = {
    'silero_encoder_v5.onnx': {
        'bytes': 713415,
        'sha256': '0e9fc8f56407693d283f99045fb95c2b90fdca3433ce5f83e2c121fb4e615075'},
    'silero_decoder_v5.onnx': {
        'bytes': 532505,
        'sha256': '8c20344f509846a07ccd85827e8857017fae67fabd58689bec1af79e1d488307'},
}


def verify_models(directory):
    """Check existing small local assets before allocating model sessions."""
    paths = {}
    for name, pin in MODEL_FILES.items():
        path = Path(directory) / name
        if not path.is_file() or path.stat().st_size != pin['bytes']:
            raise ValueError('Speech evidence model size does not match pinned artifact')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != pin['sha256']:
            raise ValueError('Speech evidence model checksum does not match pinned artifact')
        paths[name] = path
    return paths


def _load_model():
    # Metadata locates package files without importing faster-whisper/native ORT.
    from importlib.metadata import distribution
    installed = distribution('faster-whisper')
    if installed.version != '1.1.1':
        raise ValueError('Speech evidence requires pinned faster-whisper 1.1.1 wrapper')
    verify_models(installed.locate_file('faster_whisper/assets'))
    from faster_whisper.vad import get_vad_model
    # Existing wrapper uses CPU, intra/inter threads1 and disabled CPU arena.
    return get_vad_model()


def speech_evidence(stereo_float32_44100, sourceOffsetSeconds, *, model_callable=None):
    """Return independent-channel 32ms scores, with real coverage explicit.

    Input is [2,N] float32 at44100Hz, 0.1..30s, finite and unmodified. The
    injected callable is for pure tests; its result is marked unverified. No
    gain normalization, clipping, channel averaging or speech threshold occurs.
    """
    import numpy as np
    wave = stereo_float32_44100
    if (not isinstance(wave, np.ndarray) or wave.dtype != np.float32 or
            wave.ndim != 2 or wave.shape[0] != 2 or
            not 4410 <= wave.shape[1] <= 30 * INPUT_RATE or
            not np.isfinite(wave).all()):
        raise ValueError('Speech evidence requires finite stereo float32 at44100Hz for0.1..30s')
    if (isinstance(sourceOffsetSeconds, (bool, np.bool_)) or
            not isinstance(sourceOffsetSeconds, (int, float, np.integer, np.floating)) or
            not math.isfinite(float(sourceOffsetSeconds)) or sourceOffsetSeconds < 0):
        raise ValueError('Speech evidence source offset must be finite and nonnegative')
    offset = float(sourceOffsetSeconds)
    duration = wave.shape[1] / INPUT_RATE
    if not math.isfinite(offset + duration):
        raise ValueError('Speech evidence source end must be finite')
    from scipy.signal import resample_poly
    # axis1 keeps opposite-phase channels independent. scipy's zero-phase
    # resampling aligns the first output sample with the first input sample.
    resampled = resample_poly(wave, 160, 441, axis=1).astype(np.float32, copy=False)
    real_samples = resampled.shape[1]
    # At least64 zeros protect the final real samples from the wrapper's
    # context-view mutation. Never pass a caller-owned buffer to the wrapper.
    padded_samples = ((real_samples + 64 + WINDOW - 1) // WINDOW) * WINDOW
    padded = np.zeros((2, padded_samples), np.float32)
    padded[:, :real_samples] = resampled
    real_frames = (wave.shape[1] * 160 + 441 * WINDOW - 1) // (441 * WINDOW)
    model = _load_model() if model_callable is None else model_callable
    probabilities = np.asarray(model(padded))
    raw_shape = list(probabilities.shape)
    if probabilities.shape == (2, padded_samples // WINDOW, 1):
        probabilities = probabilities[:, :, 0]
    if (probabilities.shape != (2, padded_samples // WINDOW) or
            not np.issubdtype(probabilities.dtype, np.number) or
            np.iscomplexobj(probabilities) or not np.isfinite(probabilities).all() or
            (probabilities < 0).any() or (probabilities > 1).any()):
        raise ValueError('Speech evidence model output must be finite channel probabilities')
    probabilities = probabilities[:, :real_frames]
    starts = np.arange(real_frames, dtype=np.float64) * WINDOW / MODEL_RATE
    ends = np.minimum(starts + WINDOW / MODEL_RATE, duration)
    last_duration = float(ends[-1] - starts[-1])
    return {
        'method': 'silero-v5-stereo-speech-probability', 'version': 1,
        'modelSHA256': {name: pin['sha256'] for name, pin in MODEL_FILES.items()},
        'modelArtifactsVerified': model_callable is None,
        'modelWrapperVersion': 'faster-whisper-1.1.1',
        'rawProbabilityShape': raw_shape,
        'sourceOffsetSeconds': offset, 'sourceEndSeconds': offset + duration,
        'durationSeconds': duration, 'inputSampleRate': INPUT_RATE,
        'inputSamples': wave.shape[1], 'modelSampleRate': MODEL_RATE,
        'resampledSamples': real_samples, 'modelPaddedSamples': padded_samples,
        'frameHopSeconds': WINDOW / MODEL_RATE,
        'frameStartSeconds': (starts + offset).tolist(),
        'frameEndSeconds': (ends + offset).tolist(),
        'leftSpeechProbability': probabilities[0].astype(float).tolist(),
        'rightSpeechProbability': probabilities[1].astype(float).tolist(),
        'maxSpeechProbability': probabilities.max(axis=0).astype(float).tolist(),
        'lastFrameSeconds': last_duration,
        'lastFramePartial': last_duration < WINDOW / MODEL_RATE - 1e-12,
        'lastFrameIncludesPadding': last_duration < WINDOW / MODEL_RATE - 1e-12,
        'paddingFramesExcluded': padded_samples // WINDOW - real_frames,
        'verifiedWordBoundary': False, 'vocalAbsenceVerified': False,
        'speechThresholdApplied': False,
    }
