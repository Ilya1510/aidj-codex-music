"""Syllable gaps, missing ASR windows, and short RMS dips are not safe exits."""
import math

import pytest

from aidj.quiet_boundaries import (boundary_points, detect_quiet_regions,
    digital_silence_regions, natural_ending, short_digital_eof, short_natural_release)


def envelope(gap_seconds=1, quiet=.004):
    return [.1]*100+[quiet]*round(gap_seconds*100)+[.1]*100


def test_small_gap_between_syllables_or_words_is_not_a_boundary():
    for span in (.05, .15, .28, .68):
        assert detect_quiet_regions(envelope(span), coverage=[(0, 10)]) == []


def test_entire_quiet_interval_and_fade_are_inside_observed_vocal_clear_gap():
    regions = detect_quiet_regions(envelope(), words=[(0, .7), (2.3, 3)], coverage=[(0, 3)])
    assert len(regions) == 1
    region = regions[0]
    assert region['start'] == 1 and region['end'] == 2
    assert region['validated'] and region['coverageComplete']
    assert region['proof'] == 'observed-vocal-clear-quiet'
    assert region['relativeMax'] == pytest.approx(.04)
    points = boundary_points(regions)
    for entry in points['entries']:
        assert 1.15 <= entry['time'] < entry['time']+entry['fade'] <= 1.85+1e-8
    for exit_ in points['exits']:
        assert 1.15 <= exit_['fadeStart'] < exit_['time'] <= 1.85+1e-8
        assert exit_['time']-exit_['fadeStart'] == pytest.approx(exit_['fade'])


def test_single_quiet_fft_dip_between_loud_frames_does_not_pass():
    rms = envelope(); rms[150] = .08
    assert detect_quiet_regions(rms, coverage=[(0, 3)]) == []


def test_unobserved_asr_interval_never_means_vocal_free():
    regions = detect_quiet_regions(envelope(), coverage=[(0, .9), (2.1, 3)])
    assert len(regions) == 1
    assert not regions[0]['validated'] and not regions[0]['absoluteSilence']
    assert regions[0]['proof'] == 'unknown-vocal-coverage'
    assert boundary_points(regions) == {'entries': [], 'exits': []}
    uncertain = boundary_points(regions, include_unknown=True)
    assert uncertain['exits'] and not uncertain['exits'][0]['validated']


def test_physical_near_silence_can_be_used_when_asr_did_not_cover_the_tail():
    regions = detect_quiet_regions(envelope(quiet=.0003), coverage=[])
    assert len(regions) == 1
    assert regions[0]['validated'] and regions[0]['absoluteSilence']
    assert not regions[0]['coverageComplete']
    assert regions[0]['proof'] == 'physical-near-silence'


def test_known_vocal_inside_quiet_audio_is_vetoed_even_if_low_confidence():
    # Contradictory ASR/audio observations are kept conservative, not promoted
    # to a proof that the recognised syllable was silent.
    for confident in (True, False):
        assert detect_quiet_regions(envelope(quiet=.0003), words=[(1.2, 1.8, confident)], coverage=[(0, 3)]) == []


def test_overlap_and_nested_asr_spans_are_unioned_before_vocal_filter():
    rms = [.1]*100+[.004]*300+[.1]*100
    words = [(1, 3.8), (1.1, 1.2), (2, 2.1)]
    assert detect_quiet_regions(rms, words=words, coverage=[(0, 5)]) == []


def test_coverage_windows_can_touch_but_cannot_leave_a_hole():
    rms = envelope(2)
    complete = detect_quiet_regions(rms, coverage=[(0, 2), (2, 4)])
    assert len(complete) == 1 and complete[0]['validated']
    # A 200ms missing segment splits the surrounding runs; it is never filled
    # merely because windows existed on either side.
    partial = detect_quiet_regions(rms, coverage=[(0, 1.9), (2.1, 4)])
    assert len(partial) == 2 and all(row['validated'] for row in partial)
    assert all(row['end'] <= 1.9+1e-8 or row['start'] >= 2.1-1e-8 for row in partial)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, None, True])
def test_invalid_measurement_breaks_continuous_quiet_proof(bad):
    rms = envelope(); rms[150] = bad
    assert detect_quiet_regions(rms, coverage=[(0, 3)]) == []


def test_long_quiet_runs_do_not_insert_long_silent_playback():
    regions = detect_quiet_regions(envelope(10), coverage=[(0, 12)], reference=.1)
    points = boundary_points(regions)
    assert points['entries'][0]['time'] == pytest.approx(10.75)
    assert points['exits'][0]['time'] == pytest.approx(1.95)
    assert points['exits'][0]['fadeStart'] == pytest.approx(1.15)


def test_explicit_digital_silence_exception_is_stricter_and_labelled():
    values = envelope(1.5, quiet=.00001)
    assert not detect_quiet_regions(values, words=[(1, 2.5)])
    digital = digital_silence_regions(values)
    assert len(digital) == 1 and digital[0]['validated']
    assert digital[0]['proof'] == 'near-digital-silence-without-asr'
    assert digital[0]['asrIgnored']
    assert digital_silence_regions(envelope(1, quiet=0)) == []
    assert digital_silence_regions(envelope(2, quiet=.0002)) == []
    for kwargs in ({'maximum_rms': .001}, {'min_seconds': .7}):
        with pytest.raises(ValueError): digital_silence_regions(values, **kwargs)


def test_short_digital_padding_is_only_an_original_eof_exception():
    rms = [.1]*175+[.00001]*25
    assert digital_silence_regions(rms) == []
    point = short_digital_eof(rms, source_duration=2)
    assert point['proof'] == 'short-digital-EOF' and point['asrIgnored']
    assert point['regionStart'] == pytest.approx(1.75)
    assert 1.75+.025 <= point['fadeStart'] < point['time'] <= 2-.025+1e-8
    assert point['fade'] == pytest.approx(.15)
    # The same waveform is not EOF if future audio exists or the user cropped
    # the original to this point; no timestamp inference may override that.
    assert short_digital_eof(rms, source_duration=3, allowed_end=2) is None
    assert short_digital_eof(rms, source_duration=2.5) is None
    assert short_digital_eof(rms+[.1], source_duration=2.01) is None
    assert short_digital_eof([.1]*176+[0.]*24, source_duration=2) is None


def test_short_digital_eof_never_fades_unmeasured_fractional_final_bin():
    point = short_digital_eof([.1]*175+[0.]*25, source_duration=2.007)
    assert point['regionEnd'] == 2 and point['time'] < 2
    assert point['unmeasuredTail'] == pytest.approx(.007)
    assert short_digital_eof([.1]*175+[0.]*25, source_duration=2.02) is None
    for kwargs in ({'maximum_rms': .001}, {'min_seconds': .1}):
        with pytest.raises(ValueError): short_digital_eof([0.]*100, source_duration=1, **kwargs)


def test_short_natural_release_requires_exact_fresh_probe_and_padded_vocal_clearance():
    rms = [.1]*960+[.01]*40
    kwargs = {'source_duration': 10, 'reference': .1, 'words': [(8, 9)],
              'probe': {'start': 8, 'end': 10, 'confident': 8}}
    point = short_natural_release(rms, **kwargs)
    assert point['proof'] == 'short-natural-release' and point['validated']
    assert not point['asrIgnored'] and point['coverageComplete']
    assert 9.6+.025 <= point['fadeStart'] < point['time'] <= 10-.025+1e-8
    for probe in (None, {'start': 0, 'end': 5, 'confident': 8},
                  {'start': 8, 'end': 10, 'confident': 0},
                  {'start': 9.5, 'end': 10, 'confident': 8}):
        assert short_natural_release(rms, **{**kwargs, 'probe': probe}) is None
    assert short_natural_release(rms, **{**kwargs, 'words': [(9.75, 9.9)]}) is None
    assert short_natural_release(rms, **{**kwargs, 'allowed_end': 9.99}) is None
    assert short_natural_release([.1]*975+[.01]*25, **kwargs) is None


def test_short_natural_release_cannot_call_rising_quiet_audio_a_decay():
    rms = [.1]*960+[.001+i*.0002 for i in range(40)]
    assert short_natural_release(rms, source_duration=10, reference=.1,
        words=[], probe={'start': 8, 'end': 10, 'confident': 8}) is None


def test_nonzero_excerpt_start_and_silent_recording_have_correct_timestamps():
    regions = detect_quiet_regions([0.]*100, start_seconds=90)
    assert len(regions) == 1 and regions[0]['start'] == 90 and regions[0]['end'] == 91
    assert regions[0]['validated'] and regions[0]['referenceRms'] == 0
    assert all(math.isfinite(row['relativeMax']) for row in regions)


def test_uncertain_natural_ending_never_fades_over_known_final_vocal():
    fallback = natural_ending(100, words=[(97, 99.6)])
    assert not fallback['validated'] and fallback['proof'] == 'natural-ending-unverified'
    assert fallback['fadeStart'] >= 99.8-1e-8
    assert fallback['fade'] == pytest.approx(.2)
    assert fallback['knownVocalProtected']


def test_manual_end_inside_word_cannot_invent_safe_fade():
    fallback = natural_ending(100, words=[(89, 91)], allowed_end=90)
    assert fallback['time'] == 90 and fallback['fade'] == 0
    assert not fallback['knownVocalProtected'] and not fallback['validated']


@pytest.mark.parametrize('kwargs', [
    {'hop_seconds': 0}, {'reference': -1}, {'min_seconds': -1},
    {'word_padding': -1}, {'absolute_silence': float('nan')},
])
def test_invalid_analysis_settings_fail_explicitly(kwargs):
    with pytest.raises(ValueError): detect_quiet_regions([0.]*100, **kwargs)
