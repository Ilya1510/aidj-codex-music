"""Optional phrase-finish validator is inert, frame-conservative and source-bound."""
import copy
import json
import math
from pathlib import Path
import pytest
from aidj import fixed_set_phrase_release as release
from aidj.vocal_probe import MODEL_SHA256


def fixture(word_end=67.15,voice_end=67.2):
    offset=64.;duration=8.;hop=.01;n=800
    voice=[.05 if offset+i*hop<voice_end else .0002 for i in range(n)]
    mix=[.2]*n
    vocal={'modelSha256':MODEL_SHA256,'md5':'1'*32,'sourceOffsetSeconds':offset,
           'durationSeconds':duration,'frameHopSeconds':hop,'warnings':[],
           'vocalRms':voice,'mixRms':mix,'instrumentalRms':[a-b for a,b in zip(mix,voice)],
           'vocalToMixDb':[20*math.log10(b/a) for a,b in zip(mix,voice)]}
    source={'md5':'1'*32,'duration':180.,'lo':0.,'hi':180.,'lufs':-16.,'peak':-2.,
            'loudnessMethod':'ebu-r128-v1','loudnessStatus':'complete'}
    point={'time':70.,'fade':3.2,'fadeStart':66.8}
    words=[[66.9,word_end,True]]
    coverage=[[0.,68.],[68.,180.]]
    return vocal,point,words,coverage,source


def update_ratio(vocal):
    vocal['vocalToMixDb']=[20*math.log10(max(1e-8,b)/max(1e-8,a)) for a,b in zip(vocal['mixRms'],vocal['vocalRms'])]


def test_phrase_finishes_early_with_long_quiet_terminal_despite_loud_instrumental():
    args=fixture();result=release.check_release(*args)
    assert result['accepted'] and result['preference']=='preferred'
    assert result['voiceFraction']==.25 and result['minimumProtectedGain']==.75
    assert result['lastWordGain']>.75 and result['lastActiveVocalGain']>=.75
    assert result['terminalDurationBeforeEnd']==pytest.approx(2.4)
    assert result['normalizedTerminalVocalMaxDbfs']<-43
    assert result['normalizedMixP95Dbfs']>-30  # Engineered musical fade, not quiet music.
    assert result['naturalSilenceClaimed'] is False and result['verifiedWordBoundary'] is False
    assert result['asrIgnored'] is False and result['incomingEntryVerifiedHere'] is False
    assert result['enabled'] is False and result['method']=='phrase-finish-generated-release'


def test_secondary_fraction_still_keeps_completed_word_above_sixty_five_percent_gain():
    result=release.check_release(*fixture(word_end=67.6,voice_end=67.75))
    assert result['accepted'] and result['preference']=='secondary'
    assert result['minimumProtectedGain']==.65 and result['lastWordGain']>=.65
    assert result['terminalDurationBeforeEnd']>=1.56


def test_late_word_is_a_veto_even_if_low_confidence_and_model_quiet():
    args=list(fixture());args[2]=[[68.,68.1,False]]
    result=release.check_release(*args)
    assert not result['accepted'] and 'late-ASR-word' in result['attempts']
    args=list(fixture());args[2]=[[69.95,70.3,True]]
    assert not release.check_release(*args)['accepted']


def test_single_late_vocal_frame_vetoes_even_when_p95_would_be_quiet():
    args=list(fixture());vocal=args[0]
    vocal['vocalRms'][555]=.05  #69.55s: one isolated late syllable.
    update_ratio(vocal)
    tail=vocal['vocalRms'][400:620]
    assert sorted(tail)[int(.95*len(tail))]==.0002
    assert not release.check_release(*args)['accepted']


def test_guitar_like_model_positive_energy_through_tail_cannot_be_ignored():
    args=list(fixture());args[0]['vocalRms']=[.05]*800;update_ratio(args[0])
    assert not release.check_release(*args)['accepted']


def test_actual_asr_coverage_union_required_including_small_holes():
    args=list(fixture());args[3]=[[66.6,68.],[68.,70.2]]
    assert release.check_release(*args)['accepted']
    args[3]=[[66.6,68.],[68.001,70.2]]
    assert release.check_release(*args)['reason']=='unknown-ASR-coverage'
    args[3]=[]
    assert release.check_release(*args)['reason']=='unknown-ASR-coverage'


def test_source_model_and_manual_scope_must_match_and_replay_current_gain():
    args=list(fixture());stored=release.check_release(*args)
    assert release.replay_release(*args,stored)['accepted']
    changed=copy.deepcopy(args);changed[4]['hi']=69.99
    assert not release.check_release(*changed)['accepted']
    assert not release.replay_release(*changed,stored)['accepted']
    changed=copy.deepcopy(args);changed[4]['lufs']=-26.
    assert not release.replay_release(*changed,stored)['accepted']
    changed=copy.deepcopy(args);changed[0]['md5']='2'*32
    assert not release.check_release(*changed)['accepted']
    changed=copy.deepcopy(args);changed[0]['modelSha256']='bad'
    assert not release.check_release(*changed)['accepted']
    tampered={**stored,'playbackGainDb':-30}
    assert not release.replay_release(*args,tampered)['accepted']


def test_candidate_finder_is_pure_and_changes_no_source_order_or_data():
    vocal,point,words,coverage,source=fixture()
    before=copy.deepcopy((vocal,words,coverage,source))
    candidates=release.find_release_candidates(vocal,words,coverage,source,target_end=70.,limit=6)
    assert 1<=len(candidates)<=6
    assert all(c['releaseCheck']['accepted'] and not c['releaseCheck']['enabled'] for c in candidates)
    assert all(c['point']['fade'] in (2.4,3.2,4.) for c in candidates)
    assert (vocal,words,coverage,source)==before


def test_private_known_bad_awoo_cut_stays_ineligible():
    path=Path('/home/ilya-grid/.local/share/aidj-codex-music/research/dj1-quiet-v2/vocal-pilot/bad.json')
    if not path.exists():pytest.skip('Private calibration vectors are not bundled')
    vocal=json.loads(path.read_text());vocal['sourceOffsetSeconds']=vocal['sourceStart'];vocal['md5']='e314a1b98432867cb7b2d4588f425f99'
    source={'md5':vocal['md5'],'duration':194.14,'lo':0.,'hi':194.14,'lufs':-12.45,'peak':-1.74}
    point={'time':166.674,'fade':2.4,'fadeStart':164.274}
    assert not release.check_release(vocal,point,[[166.3,166.9,True]],[[0,194.14]],source)['accepted']
