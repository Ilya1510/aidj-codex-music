"""Secondary resolution never swaps order, invents ASR coverage, or fakes EOF."""
import copy
import sys
from types import SimpleNamespace

import pytest

import aidj.fixed_set_resolve as module
from aidj.fixed_set_resolve import (certify_reobserved, eof_points, resolve,
                                    restore_evidence, select_eof)


def test_restore_keeps_local_words_and_exact_probe_coverage_without_mutating_input():
    raw = {'words': [[0, 1, True]], 'coverage': [[0, 2]]}
    checkpoint = {'observedWords': [[3, 4, False]],
                  'repairProbes': [{'start': 2, 'end': 6, 'confident': 0}],
                  'observedCoverage': [[8, 10]]}
    before = copy.deepcopy((raw, checkpoint)); evidence = restore_evidence(raw, checkpoint)
    assert evidence['words'] == [[3, 4, False]]
    assert evidence['coverage'] == [[0, 2], [8, 10], [2, 6]]
    assert (raw, checkpoint) == before
    evidence['words'].append([11, 12, True])
    assert len(checkpoint['observedWords']) == 1


def test_reobserved_exit_requires_a_probe_here_and_no_padded_vocals():
    candidate = {'status': 'needs-probe', 'exit': {'time': 71.4, 'fadeStart': 70.2}}
    for evidence in [
        {'words': [], 'repairProbes': [{'start': 0, 'end': 28, 'confident': 12}]},
        {'words': [], 'repairProbes': [{'start': 60, 'end': 88, 'confident': 0}]},
        {'words': [[69.9, 70.1]], 'repairProbes': [{'start': 60, 'end': 88, 'confident': 12}]},
    ]:
        assert certify_reobserved(copy.deepcopy(candidate), evidence)['status'] == 'needs-probe'
    good = {'words': [], 'repairProbes': [{'start': 60, 'end': 88, 'confident': 12}]}
    result = certify_reobserved(copy.deepcopy(candidate), good)
    assert result['status'] == 'ready'
    assert result['exit']['method'] == 'reobserved-instrumental-fade'


def test_eof_exception_checks_actual_decode_and_manual_crop():
    rms = [.1]*175+[.00001]*25
    song = {'duration': 2, 'lo': 0, 'hi': 2, 'manualEnd': False}
    points = eof_points(song, {'words': [], 'repairProbes': []}, rms, 2, .1)
    assert len(points) == 1
    point = points[0]
    assert point['method'] == 'short-digital-EOF' and point['actualEOF']
    assert point['sourceDuration'] == point['metadataDuration'] == 2
    assert point['regionStart']+.025 <= point['fadeStart']
    assert point['time'] <= point['regionEnd']-.025+1e-8
    assert eof_points({**song, 'manualEnd': True}, {}, rms, 2, .1) == []
    assert eof_points({**song, 'hi': 1.9}, {}, rms, 2, .1) == []
    assert eof_points(song, {}, rms, 2.051, .1) == []


def test_natural_release_requires_a_new_resolver_probe_not_an_old_generic_window():
    song = {'duration': 10, 'lo': 0, 'hi': 10, 'manualEnd': False}
    rms = [.1]*960+[.01]*40
    probe = {'start': 8, 'end': 10, 'confident': 9}
    evidence = {'words': [[8, 9]], 'repairProbes': [probe]}
    assert eof_points(song, evidence, rms, 10, .1) == []
    evidence['repairProbes'][0] = {**probe, 'resolver': module.VERSION}
    point = eof_points(song, evidence, rms, 10, .1)[0]
    assert point['method'] == 'short-natural-release' and not point['asrIgnored']
    evidence['words'] = [[9.7, 9.9]]
    assert eof_points(song, evidence, rms, 10, .1) == []


def test_eof_select_preserves_owner_entry_and_never_crosses_manual_range():
    old = {'trackId': 'a'*24, 'md5': '1'*32, 'start': 10, 'end': 72}
    song = {'lo': 10.12344, 'hi': 100, 'duration': 100,
            'artist': 'Example', 'title': 'Ending', 'lufs': None}
    evidence = {'words': [], 'coverage': [[0, 100]]}
    point = {'time': 79.975, 'fade': .15, 'fadeStart': 79.825,
             'regionStart': 79.6, 'regionEnd': 80,
             'maxRms': .00001, 'method': 'short-digital-EOF'}
    result = select_eof(old, song, evidence, [], [point], owner=[15])
    assert result['row']['start'] == 15
    assert result['row']['trackId'] == old['trackId']
    assert result['row']['end']-result['row']['start'] < 100
    # A lower bound with more than four decimal places must not round down.
    bounded = select_eof(old, song, evidence, [], [point])
    assert bounded['row']['start'] >= song['lo']
    assert select_eof(old, song, evidence, [], [point], rejected=[79.975]) is None


class PCM:
    def __init__(self, seconds): self.seconds = seconds
    def __len__(self): return round(self.seconds*module.SR)


class ForbiddenObserver:
    def observe(self, *args):
        raise AssertionError('ASR must stay unloaded for physically justified digital EOF')


def test_digital_resolution_does_not_load_a_model_or_change_order(monkeypatch):
    old = {'trackId': 'a'*24, 'md5': '1'*32, 'start': 10, 'end': 72}
    song = {'lo': 0, 'hi': 80, 'duration': 80, 'manualEnd': False,
            'artist': 'Example', 'title': 'Ending', 'lufs': None}
    evidence = {'words': [], 'coverage': [[0, 80]]}
    rms = [.1]*7975+[.00001]*25
    monkeypatch.setattr(module, 'quiet_regions', lambda *a: ([], .1))
    monkeypatch.setattr(module, 'choose', lambda *a: {'status': 'needs-review'})
    result = resolve(old, song, evidence, rms, PCM(80), ForbiddenObserver(), owner=[10])
    assert result['status'] == 'ready'
    assert result['row']['trackId'] == old['trackId']
    assert result['row']['start'] == 10
    assert result['exit']['method'] == 'short-digital-EOF'


def test_conflicting_already_probed_instrumental_is_left_for_independent_review(monkeypatch):
    old = {'trackId': 'a'*24, 'md5': '1'*32, 'start': 10, 'end': 72}
    song = {'lo': 0, 'hi': 80, 'duration': 80, 'manualEnd': False,
            'artist': 'Example', 'title': 'Ending', 'lufs': None}
    evidence = {'words': [[70, 72]], 'coverage': [[0, 80]],
                'repairProbes': [{'start': 60, 'end': 80, 'confident': 10}]}
    candidate = {'status': 'needs-probe', 'exit': {'time': 71.4, 'fadeStart': 70.2}}
    monkeypatch.setattr(module, 'quiet_regions', lambda *a: ([], .1))
    monkeypatch.setattr(module, 'choose', lambda *a: copy.deepcopy(candidate))
    result = resolve(old, song, evidence, [.1]*8000, PCM(80), ForbiddenObserver())
    assert result['status'] == 'needs-probe'


def test_probe_of_previous_candidate_cannot_certify_a_changed_exit(monkeypatch):
    old = {'trackId': 'a'*24, 'md5': '1'*32, 'start': 10, 'end': 72}
    song = {'lo': 0, 'hi': 180, 'duration': 180, 'manualEnd': False,
            'artist': 'Example', 'title': 'Ending', 'lufs': None}
    evidence = {'words': [], 'coverage': [[0, 180]], 'repairProbes': []}
    candidates = iter([
        {'status': 'needs-probe', 'exit': {'time': 71.4, 'fadeStart': 70.2}},
        {'status': 'needs-probe', 'exit': {'time': 141.4, 'fadeStart': 140.2}},
    ])
    monkeypatch.setattr(module, 'quiet_regions', lambda *a: ([], .1))
    monkeypatch.setattr(module, 'choose', lambda *a: next(candidates))
    class Observer:
        def observe(self, evidence, pcm, begin, end):
            evidence['repairProbes'].append({'start': begin, 'end': end, 'confident': 10})
    result = resolve(old, song, evidence, [.1]*18000, PCM(180), Observer())
    assert result['status'] == 'needs-probe' and result['exit']['time'] == 141.4
    assert evidence['repairProbes'][0]['end'] < 100


def test_local_observer_rejects_long_unbounded_probe_before_model_load(monkeypatch):
    # Input bounds are checked before importing/loading faster_whisper.
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace())
    observer = module.LocalObserver(SimpleNamespace(state=None))
    with pytest.raises(RuntimeError, match='RESOLVE_PROBE_BOUNDS'):
        observer.observe({}, PCM(1000), 0, 40)
