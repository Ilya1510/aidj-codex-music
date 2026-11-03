"""Find and trim silence at the two edges, preserving pauses inside music."""
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import subprocess

import imageio_ffmpeg
import mutagen


class AudioError(RuntimeError):
    pass


@dataclass
class Edges:
    duration: float
    start: float
    end: float
    threshold_db: float
    silence: list[tuple[float, float]]

    @property
    def removed_start(self):
        return self.start

    @property
    def removed_end(self):
        return max(0.0, self.duration - self.end)

    @property
    def changed(self):
        return self.start > 0 or self.end < self.duration - 0.02

    def to_dict(self):
        return {**asdict(self), 'removed_start': self.removed_start, 'removed_end': self.removed_end}


def ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(path: Path):
    audio = mutagen.File(path)
    if audio is None or audio.info is None or not audio.info.length:
        raise AudioError('Не удалось прочитать длительность аудио.')
    return audio.info.length


def scan(path: Path, threshold_db=-55.0, minimum_silence=0.6, padding=0.15, minimum_cut=0.8):
    total = duration(path)
    if total > 1800:
        raise AudioError('Поддерживаются записи не длиннее 30 минут.')
    command = [ffmpeg(), '-nostdin', '-hide_banner', '-nostats', '-threads', '1',
               '-filter_threads', '1', '-i', str(path), '-vn', '-af',
               f'silencedetect=noise={threshold_db}dB:d={minimum_silence}', '-f', 'null', '-']
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    intervals = []
    started = None
    try:
        for line in process.stderr:
            first = re.search(r'silence_start:\s*([-+0-9.eE]+)', line)
            last = re.search(r'silence_end:\s*([-+0-9.eE]+)', line)
            if first:
                started = max(0.0, float(first.group(1)))
            if last and started is not None:
                intervals.append((started, min(total, float(last.group(1)))))
                started = None
            if len(intervals) > 20000:
                raise AudioError('Слишком много сегментов в записи.')
        if process.wait(timeout=20):
            raise AudioError('FFmpeg не смог декодировать запись.')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stderr.close()
    if started is not None:
        intervals.append((started, total))
    start, end = 0.0, total
    for left, right in intervals:
        if left <= 0.08:
            start = min(total, right) - padding
            start = max(0.0, start)
        if right >= total - 0.15:
            end = min(total, left + padding)
    if total - end < minimum_cut:
        end = total
    if start < minimum_cut:
        start = 0.0
    if end - start < 0.5:
        raise AudioError('В записи не найдено достаточно слышимого звука.')
    return Edges(total, start, end, threshold_db, intervals)


def trim(source: Path, destination: Path, **settings):
    edges = scan(source, **settings)
    if not edges.changed:
        return source, edges
    command = [ffmpeg(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
               '-ss', f'{edges.start:.6f}', '-i', str(source), '-t', f'{edges.end-edges.start:.6f}',
               '-map', '0:a:0', '-map_metadata', '0', '-vn', '-c:a', 'copy',
               '-avoid_negative_ts', 'make_zero', '-y', str(destination)]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise AudioError('Не удалось подготовить аудио.')
    actual = duration(destination)
    expected = edges.end - edges.start
    if abs(actual - expected) > 0.25:
        destination.unlink(missing_ok=True)
        raise AudioError('Подготовленный файл не прошёл проверку длительности.')
    verification = scan(destination, **settings)
    if verification.changed:
        destination.unlink(missing_ok=True)
        raise AudioError('Подготовленный файл не прошёл проверку звука.')
    return destination, edges
