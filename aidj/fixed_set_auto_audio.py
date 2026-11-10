"""One verified original at a time; bounded chunked features and local pulse fits."""
import gc
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def prepare_song(settings, db, song, snap, scratch, decoder, keep_row=None):
    import numpy as np
    import librosa
    from scipy.signal import find_peaks
    from .disk import Disk
    from .fixed_set_prepare import analyse, SR
    from .fixed_set_auto import VERSION, usable, rejected
    if shutil.disk_usage(scratch).free < 768*1024**2:raise ValueError('AUTO_LOW_DISK')
    ev = db.execute('SELECT payload FROM dj_evidence WHERE md5=?', (song['md5'],)).fetchone()
    graph = db.execute('SELECT payload FROM dj_analysis WHERE md5=?', (song['md5'],)).fetchone()
    if not ev or not graph or not graph[0]:raise ValueError('AUTO_EVIDENCE_MISSING')
    evidence = json.loads(ev[0]);graph = json.loads(graph[0])
    human = [];human_exits = []
    for point, raw_roles, value in snap['votes'].get(song['md5'], []):
        if value != 1:continue
        roles = json.loads(raw_roles)
        if 'entry' in roles:human.append(point/1000)
        if 'exit' in roles:human_exits.append(point/1000)
    def decode(source, output, rate, offset=None, duration=None):
        args = [decoder, '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1']
        if offset is not None:args += ['-ss', str(offset), '-t', str(duration)]
        args += ['-i', str(source), '-t', str(duration if duration is not None else 1800),
                 '-vn', '-sn', '-dn', '-ac', '1', '-ar', str(rate), '-f', 'f32le', '-y', str(output)]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode:raise ValueError('AUTO_DECODE_FAILED')
    with tempfile.TemporaryDirectory(prefix='source-', dir=scratch) as tmp:
        tmp = Path(tmp);source = tmp/('original'+Path(song['remote_path']).suffix)
        Disk(settings).download(song['remote_path'], source, expected_md5=song['md5'])
        if source.stat().st_size != song['bytes']:raise ValueError('AUTO_SOURCE_CHANGED')
        pcm = tmp/'full.f32';decode(source, pcm, SR)
        features = analyse(song, graph, evidence, pcm, human, rejected(song, snap), human_exits)
        pcm.unlink()
        row = keep_row
        if row is None:
            for option in features['options']:
                candidate = {'trackId': song['id'], 'md5': song['md5'],
                             **{k: option[k] for k in ('start', 'end', 'fadeOut')},
                             'fadeIn': 5., 'transitionSeconds': 0., 'transitionKind': 'clean-cut', 'rate': 1.}
                if usable(candidate, song, snap):row = candidate;break
        if row is None:raise ValueError('AUTO_NO_USABLE_CUT')
        observations = {'md5': song['md5'], 'sourceStart': row['start'], 'sourceEnd': row['end'],
                        'sourceBytes': song['bytes'], 'sourceDuration': song['duration'], 'status': 'complete'}
        prior = evidence.get('bpm') or 120.
        for side, point in [('entry', row['start']), ('exit', row['end'])]:
            offset = max(0, min(point-12, song['duration']-32))
            duration = min(32, song['duration']-offset)
            pcm = tmp/(side+'.f32');decode(source, pcm, 16000, offset, duration)
            y = np.fromfile(pcm, dtype='<f4');pcm.unlink()
            if not len(y):raise ValueError('AUTO_NO_AUDIO')
            hop = 256
            onset = librosa.onset.onset_strength(y=y, sr=16000, hop_length=hop)
            tempo, frames = librosa.beat.beat_track(onset_envelope=onset, sr=16000, hop_length=hop,
                                                   start_bpm=prior, trim=False)
            threshold = max(1e-6, float(np.quantile(onset, .65)), float(np.max(onset))*.08)
            peaks, _ = find_peaks(onset, height=threshold, distance=4)
            aligned = [];unsupported = 0
            for frame in frames:
                near = peaks[np.abs(peaks-frame) <= 3]
                if not len(near):unsupported += 1;continue
                nearest = int(near[np.argmax(onset[near])]);t = offset+nearest*hop/16000
                if not aligned or t > aligned[-1]:aligned.append(t)
            estimate = np.asarray(tempo).reshape(-1)
            observations[side] = {'md5': song['md5'], 'sourceOffsetSeconds': offset,
                'sourceEndSeconds': offset+len(y)/16000, 'beats': aligned, 'trackedBeats': len(frames),
                'unsupportedBeats': unsupported, 'onsetSupportedFraction': len(aligned)/max(1, len(frames)),
                'algorithm': 'librosa-local32s-beats-snapped-to-measured-onset-peaks',
                'hopSeconds': hop/16000, 'metadataBpmUsedOnlyAsInitialGuess': prior,
                'estimatedBpm': float(estimate[0]) if len(estimate) else 0.}
            del y, onset;gc.collect()
    return {'version': VERSION, 'md5': song['md5'], 'bytes': song['bytes'], 'duration': song['duration'],
            'row': row, 'observations': observations,
            'features': {k: features[k] for k in ('bpm', 'chroma')}}
