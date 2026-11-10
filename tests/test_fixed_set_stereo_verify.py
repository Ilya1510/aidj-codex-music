"""Opposite-phase channels cannot manufacture a quiet boundary."""
import copy
import sys

import pytest

from aidj.fixed_set_stereo_verify import (attach, input_hash, needs_verification,
    proof_hash, stereo_energy_envelope, stereo_error, verify)


def fixture():
    song = {'id': 'a'*24, 'md5': '1'*32, 'duration': 100, 'lo': 0,
            'hi': 100, 'manualEnd': False, 'lufs': None}
    row = {'trackId': song['id'], 'md5': song['md5'], 'start': 10,
           'end': 70.95, 'fadeOut': .8, 'fadeIn': .1}
    item = {'status': 'ready', 'trackId': song['id'], 'md5': song['md5'],
            'row': row, 'observedWords': [], 'repairProbes': [],
            'exit': {'time': 70.95, 'fade': .8, 'fadeStart': 70.15,
                     'regionStart': 70, 'regionEnd': 72, 'method': 'strict-quiet',
                     'relativeMax': .04, 'maxRms': .004}}
    evidence = {'words': [], 'coverage': [[0, 100]], 'repairProbes': []}
    rms = [.1]*7000+[.004]*200+[.1]*2800
    return item, song, evidence, rms


def test_antiphase_stereo_has_full_energy_and_is_not_a_quiet_exit(monkeypatch):
    # Exercise the dependency-free path used by CI too. Mono averaging these
    # samples gives all-zero audio; channel-energy RMS must still be0.1.
    monkeypatch.setitem(sys.modules, 'numpy', None)
    phase = [.1, -.1, -.1, .1]*2
    measured = stereo_energy_envelope(phase, frames_per_bin=2)
    assert measured == pytest.approx([.1, .1])
    assert stereo_energy_envelope([0., .2]*4, frames_per_bin=2) == pytest.approx([.2/(2**.5)]*2)
    item, song, evidence, _ = fixture()
    snapshot = verify(item, song, evidence, [measured[0]]*10000, 100)
    assert not snapshot['accepted'] and snapshot['reason'] == 'music-not-quiet-in-stereo'


def test_vectorized_energy_matches_known_channel_energy():
    # Production's NumPy path is exercised when optional DSP dependencies are
    # present; all source/word/range proof tests remain dependency-free.
    pytest.importorskip('numpy')
    assert stereo_energy_envelope([.1, -.1, -.1, .1]*2, frames_per_bin=2) == pytest.approx([.1, .1])


def test_real_quiet_stereo_release_has_a_small_replayable_proof():
    item, song, evidence, rms = fixture()
    original = copy.deepcopy(item)
    snapshot = verify(item, song, evidence, rms, 100)
    assert snapshot['accepted'] and snapshot['sourceChannels'] == 2
    assert snapshot['proofStart'] == 70 and snapshot['proofEnd'] == pytest.approx(71.1)
    assert len(snapshot['rms']) == 110  # Not the entire100-second source.
    assert snapshot['measuredMax'] == .004 and snapshot['relativeMax'] == pytest.approx(.04)
    verified = attach(item, snapshot)
    assert item == original and verified['row'] == original['row']
    assert stereo_error(verified, song, evidence) is None


def test_soft_quiet_proof_retains_its_required_minimum_not_only_fade_window():
    item, song, evidence, rms = fixture()
    item['exit']['method'] = 'soft-quiet'
    snapshot = verify(item, song, evidence, rms, 100)
    assert snapshot['accepted'] and len(snapshot['rms']) == 120
    assert snapshot['proofEnd']-snapshot['proofStart'] == pytest.approx(1.2)


def test_proof_cannot_survive_word_changes_or_manual_range_changes():
    item, song, evidence, rms = fixture()
    verified = attach(item, verify(item, song, evidence, rms, 100))
    assert stereo_error(verified, {**song, 'lo': 5}, evidence) == 'stereo-range-mismatch'
    changed = {**evidence, 'words': [[70.2, 70.4]]}
    assert stereo_error(verified, song, changed) == 'stereo-observations-changed'
    assert not verify(item, song, changed, rms, 100)['accepted']
    assert not verify(item, {**song, 'lo': 11}, evidence, rms, 100)['accepted']
    assert not verify(item, {**song, 'md5': '2'*32}, evidence, rms, 100)['accepted']


def test_proof_vectors_are_recomputed_and_not_just_an_accepted_flag():
    item, song, evidence, rms = fixture()
    verified = attach(item, verify(item, song, evidence, rms, 100))
    tampered = copy.deepcopy(verified); tampered['stereoEvidence']['rms'][10] = .1
    assert stereo_error(tampered, song, evidence) == 'changed-stereo-proof'
    # Even a recomputed checksum cannot hide a mismatch to the saved numeric
    # maxima or a physically loud proof vector.
    tampered['stereoEvidence']['proofHash'] = proof_hash(tampered['stereoEvidence'])
    assert stereo_error(tampered, song, evidence) == 'different-stereo-measurement'


def test_entry_shift_invalidates_stereo_proof_even_when_exit_is_unchanged():
    item, song, evidence, rms = fixture()
    verified = attach(item, verify(item, song, evidence, rms, 100))
    verified['row']['start'] += .5
    assert stereo_error(verified, song, evidence) == 'stereo-cut-changed'


def test_vocal_stem_methods_are_skipped_instead_of_gaining_fake_physical_proofs():
    item, _, _, _ = fixture()
    for method in ('vocal-checked-instrumental-fade', 'vocal-isolated-fade', 'reobserved-instrumental-fade'):
        item['exit']['method'] = method
        assert not needs_verification(item)
    item['exit']['method'] = 'strict-quiet'; assert needs_verification(item)
    item['status'] = 'needs-review'; assert not needs_verification(item)
    item['entryReviewOnly'] = True
    assert needs_verification(item)


def test_outgoing_stereo_proof_can_be_saved_without_certifying_a_pending_incoming_entry():
    item,song,evidence,rms=fixture();item.update(status='needs-review',entryReviewOnly=True)
    result=attach(item,verify(item,song,evidence,rms,80))
    assert result['stereoEvidence']['accepted']
    assert result['status']=='needs-review' and result['entryReviewOnly']


def test_unknown_ordinary_quiet_music_is_not_certified_from_missing_coverage():
    item, song, evidence, rms = fixture()
    snapshot = verify(item, song, {**evidence, 'coverage': []}, rms, 100)
    assert not snapshot['accepted'] and snapshot['reason'] == 'unknown-vocals-in-stereo'


def test_short_digital_eof_is_rechecked_with_actual_stereo_tail():
    item, song, evidence, _ = fixture()
    song.update(duration=80, hi=80)
    item['row'].update(end=79.975, fadeOut=.15)
    item['exit'].update(time=79.975, fade=.15, fadeStart=79.825,
        regionStart=79.75, regionEnd=80, method='short-digital-EOF')
    evidence['coverage'] = [[0, 80]]
    rms = [.1]*7975+[.00001]*25
    proof = verify(item, song, evidence, rms, 80)
    assert proof['accepted'] and len(proof['rms']) == 25
    assert stereo_error(attach(item, proof), song, evidence) is None
    assert not verify(item, {**song, 'manualEnd': True}, evidence, rms, 80)['accepted']
    assert not verify(item, song, evidence, [.1]*7975+[.004]*25, 80)['accepted']


def test_duplicate_coverage_intervals_do_not_trigger_an_extra_source_download():
    item, song, evidence, _ = fixture()
    assert input_hash(item, song, evidence) == input_hash(item, song, {**evidence, 'coverage': [[0, 100], [0, 50], [50, 100]]})
