import math
from pathlib import Path
import struct
import wave

import pytest

from aidj.audio import AudioError, duration, scan, trim


def audio_file(path, parts, amplitude=0.25, right_silent=False):
    rate = 16000
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        offset = 0
        for seconds, audible in parts:
            count = round(seconds * rate)
            for first in range(0, count, 4096):
                chunk = bytearray()
                for frame in range(first, min(first + 4096, count)):
                    sample = int(32767 * amplitude * math.sin(2 * math.pi * 440 * (offset + frame) / rate)) if audible else 0
                    chunk.extend(struct.pack('<hh', sample, 0 if right_silent else sample))
                out.writeframesraw(chunk)
            offset += count
    return path


def test_only_edges_are_removed_and_internal_pause_remains(tmp_path):
    source = audio_file(tmp_path/'original.wav', [(2,False),(3,True),(2,False),(3,True),(4,False)])
    result, edges = trim(source, tmp_path/'trimmed.wav')
    assert edges.removed_start == pytest.approx(1.85, abs=.03)
    assert edges.removed_end == pytest.approx(3.85, abs=.03)
    assert duration(result) == pytest.approx(8.3, abs=.05)
    assert any(2.1 > end-start > 1.9 and start > .5 for start,end in scan(result).silence)


def test_audible_music_does_not_get_reencoded(tmp_path):
    source = audio_file(tmp_path/'original.wav', [(3,True)])
    result, edges = trim(source, tmp_path/'trimmed.wav')
    assert result == source and not edges.changed
    assert not (tmp_path/'trimmed.wav').exists()


def test_all_silent_audio_is_rejected(tmp_path):
    source = audio_file(tmp_path/'silence.wav', [(4,False)])
    with pytest.raises(AudioError):
        scan(source)


def test_quiet_audio_and_one_sided_stereo_are_preserved(tmp_path):
    source = audio_file(tmp_path/'quiet.wav', [(3,True)], amplitude=0.01, right_silent=True)
    assert not scan(source).changed


def test_small_normal_lead_and_trail_are_kept(tmp_path):
    source = audio_file(tmp_path/'small.wav', [(.4,False),(3,True),(.4,False)])
    assert not scan(source).changed
