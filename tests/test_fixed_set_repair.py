import math
import sys
from types import SimpleNamespace

import pytest

from aidj.fixed_set_repair import choose, covered, merge, probe_supports, word_clear


def test_fresh_model_tuples_and_cached_json_lists_merge_without_losing_vocals():
    words=[[1,5,True],(2,3,False),(7,8,True)]
    assert merge(words)==[[1,5],[7,8]]
    assert not word_clear(words,2,4)
    assert word_clear(words,5.5,6.5)
    assert covered([[0,4],(4,9)],1,8)


def test_probe_elsewhere_cannot_validate_a_newly_selected_exit():
    point={'fadeStart':170,'time':171.2}
    assert not probe_supports({'start':48,'end':76,'confident':53},point)
    assert not probe_supports({'start':160,'end':180,'confident':0},point)
    assert probe_supports({'start':160,'end':180,'confident':12},point)


@pytest.fixture
def numeric_fixture(monkeypatch):
    # choose() only needs these three elementary numeric operations. Keep
    # selection-policy regressions runnable in CI without installing NumPy or
    # running FFT/ASR work; actual extraction owns those separate dependencies.
    def quantile(values, q):
        ordered = sorted(values)
        if not ordered: raise ValueError('empty envelope')
        at = (len(ordered)-1)*q; lo = math.floor(at); hi = math.ceil(at)
        return ordered[lo]+(ordered[hi]-ordered[lo])*(at-lo)
    def arange(start, end, step):
        for i in range(max(0, math.ceil((end-start)/step))):
            yield start+i*step
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace(quantile=quantile, arange=arange, max=max))
    old = {'trackId': 'a'*24, 'md5': '1'*32, 'start': 10, 'end': 72,
           'fadeIn': .1, 'fadeOut': .4, 'rate': 1,
           'transitionKind': 'clean-cut', 'transitionSeconds': 0}
    song = {'lo': 0, 'hi': 100, 'title': 'Quiet release', 'artist': 'Example', 'lufs': None}
    evidence = {'words': [], 'coverage': [[0, 100]]}
    # A genuinely continuous quiet area, surrounded by full-level music.
    rms = [.1]*7000+[.0002]*200+[.1]*2800
    return old, song, evidence, rms


def test_gold_boundary_keeps_the_actual_approved_entry(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 71.5, 'fadeOut': .65}
    result = choose(old, song, evidence, rms, owner=[10], gold=gold)
    assert result['status'] == 'ready'
    assert result['row']['start'] == 10 and result['row']['end'] == 71.5
    assert result['exit']['method'] == 'owner-listened-quiet'
    assert result['row']['end']-result['row']['fadeOut'] >= 70.15


def test_gold_label_cannot_validate_fade_outside_measured_quiet_window(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 70.2, 'fadeOut': .8}
    result = choose(old, song, evidence, rms, owner=[10], gold=gold)
    assert result['status'] == 'ready'
    assert result['exit']['method'] != 'owner-listened-quiet'
    assert result['row']['end']-result['row']['fadeOut'] >= result['exit']['regionStart']+.15-1e-4


def test_gold_label_does_not_bypass_current_manual_source_range(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    song = {**song, 'hi': 71}
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 71.5, 'fadeOut': .65}
    result = choose(old, song, evidence, rms, owner=[10], gold=gold)
    assert result['status'] == 'ready'
    assert result['row']['end'] <= 71
    assert result['exit']['method'] != 'owner-listened-quiet'


def test_explicitly_rejected_boundary_cannot_be_restored_by_old_gold_label(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    gold = {'quietWindow': {'start': 70, 'end': 72}, 'end': 71.5, 'fadeOut': .65}
    result = choose(old, song, evidence, rms, owner=[10], rejected=[71.5], gold=gold)
    assert result['status'] == 'ready'
    assert abs(result['row']['end']-71.5) >= .35


def test_unobserved_ordinary_quiet_music_is_not_declared_vocal_free(numeric_fixture):
    old, song, evidence, rms = numeric_fixture
    rms = [.004 if level == .0002 else level for level in rms]
    result = choose(old, song, {**evidence, 'coverage': []}, rms, owner=[10])
    assert result['status'] == 'needs-review'
    assert 'row' not in result
