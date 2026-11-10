import math

import pytest

from aidj.fixed_set_rhythm import dominant_pulse_fit, local_fit, plan_rhythm


def observation(bpm, phase=0., start=0., end=100., md5='a', jitter=0.):
    period = 60/bpm
    beats = [phase+i*period+jitter*math.sin(i*1.7) for i in range(500)]
    return {'md5': md5, 'sourceOffsetSeconds': start, 'sourceEndSeconds': end,
            'beats': [b for b in beats if start <= b <= end]}


def rows():
    return [{'trackId':'a', 'md5':'a', 'start':10., 'end':50., 'fadeIn':4., 'fadeOut':.8,
             'rate':1., 'transitionKind':'crossfade', 'transitionSeconds':5.},
            {'trackId':'b', 'md5':'b', 'start':10.2, 'end':50.2, 'fadeIn':4., 'fadeOut':.8,
             'rate':1., 'transitionKind':'clean-cut', 'transitionSeconds':0.}]


def test_actual_local_beats_accept_quantisation_but_reject_drift_missing_beats_and_missing_coverage():
    value = observation(120, jitter=.008)
    fit = local_fit(value, 10, 25)
    assert fit['accepted'] and abs(fit['bpm']-120)<.1 and fit['maxErrorSeconds']<.01
    assert local_fit(value, -1, 15)['reason']=='outside-observed-coverage'
    assert local_fit(value, 10, 15)['reason']=='insufficient-observed-beats'
    missed = {**value, 'beats':[b for i,b in enumerate(value['beats']) if i%4]}
    assert local_fit(missed,10,25)['accepted'] is False
    bad = observation(120, jitter=.11)
    assert local_fit(bad,10,25)['accepted'] is False


def test_real_phase_alignment_changes_overlap_not_reviewed_source_boundaries():
    value = rows()
    observations = {'a':observation(120,phase=.12), 'b':observation(122,phase=.31,md5='b')}
    planned = plan_rhythm(value, observations)
    proof = planned['pairs'][0]
    assert proof['beatMatched'] is True
    assert 4.6 <= proof['transitionSeconds'] <= 5.4
    assert proof['maxPhaseErrorSeconds'] < 1e-10
    for original, actual in zip(value, planned['tracks']):
        for key in ('trackId','md5','start','end','fadeIn','fadeOut'):
            assert actual[key] == original[key]
        assert .97 <= actual['rate'] <= 1.03
    # The same physical observed beats now land at the same audible time.
    audible_left = (proof['leftAnchorSeconds']-proof['leftOverlapStartSeconds'])/proof['leftRate']
    audible_right = (proof['rightAnchorSeconds']-value[1]['start'])/proof['rightRate']
    assert abs(audible_left-audible_right)<1e-10
    assert value[0]['rate']==1 and value[0]['transitionSeconds']==5


def test_incompatible_tempo_ineligible_pair_and_stale_source_do_not_fake_a_match():
    value=rows()
    obs={'a':observation(100), 'b':observation(150,md5='b')}
    assert plan_rhythm(value,obs)['pairs'][0]['reason']=='incompatible-local-tempos'
    obs['b']=observation(102,md5='wrong-source')
    assert plan_rhythm(value,obs)['pairs'][0]['beatMatched'] is False
    obs['b']=observation(102,md5='b')
    result=plan_rhythm(value,obs,eligible={'a':False})
    assert result['pairs'][0]['reason']=='ineligible-overlap' and result['tracks']==value
    value[0]['transitionSeconds']=3
    assert plan_rhythm(value,obs)['tracks']==value


def test_observed_coverage_must_reach_entire_overlap_not_just_an_anchor():
    value=rows()
    obs={'a':observation(120,end=49.), 'b':observation(120,md5='b')}
    result=plan_rhythm(value,obs)
    assert result['pairs'][0]['beatMatched'] is False
    assert result['tracks']==value


def test_small_excerpt_does_not_get_an_overlap_larger_than_its_playable_half():
    value=rows()
    value[0]['start']=42.
    obs={'a':observation(120), 'b':observation(120,md5='b')}
    result=plan_rhythm(value,obs)
    assert not result['pairs'][0]['beatMatched']
    assert result['tracks']==value


def test_a_constant_rate_is_never_overwritten_by_an_incompatible_following_group():
    value=rows()
    value[1].update(transitionKind='crossfade',transitionSeconds=5.)
    value.extend([{**value[0], 'trackId':'c','md5':'c'}, {**value[1], 'trackId':'d','md5':'d'}])
    obs={k:observation(bpm,phase=.12,md5=k) for k,bpm in [('a',100),('b',102),('c',150),('d',152)]}
    result=plan_rhythm(value,obs)
    assert result['pairs'][0]['beatMatched'] and not result['pairs'][1]['beatMatched'] and result['pairs'][2]['beatMatched']
    assert abs(result['tracks'][0]['rate']*100-result['tracks'][1]['rate']*102)<1e-9
    assert abs(result['tracks'][2]['rate']*150-result['tracks'][3]['rate']*152)<1e-9
    assert result['tracks'][1]['rate']==result['pairs'][0]['rightRate']


def test_invalid_duplicate_or_nonfinite_inputs_reject():
    value=rows()
    with pytest.raises(ValueError):plan_rhythm([value[0],value[0]],{})
    with pytest.raises(ValueError):plan_rhythm(value,{},target_seconds=float('nan'))
    value[0]['rate']=float('nan')
    with pytest.raises(ValueError):plan_rhythm(value,{})


def test_dominant_pulse_v2_handles_occasional_accents_and_missing_hits_without_inventing_observations():
    original=observation(120,phase=.1,end=30.)
    original['beats']=sorted(original['beats']+[3.37,6.9,10.32])
    strict=local_fit(original,1.,25.)
    robust=dominant_pulse_fit(original,1.,25.)
    assert not strict['accepted'] and robust['accepted'] and robust['version']==2
    assert abs(robust['bpm']-120)<1e-8
    assert {x['sourceSeconds'] for x in robust['discardedObservedBeats']}=={3.37,6.9,10.32}
    assert robust['observedSupportFraction']>.9 and robust['allAccentsMatched'] is False
    assert set(robust['beats'])<=set(original['beats'])
    missing=observation(120,phase=.1,end=30.)
    missing['beats']=[p for i,p in enumerate(missing['beats']) if i not in (5,12,27,38)]
    robust=dominant_pulse_fit(missing,1.,25.)
    assert robust['accepted'] and robust['maximumObservedPulseGap']==2
    assert len(robust['missingPulseIndices'])==4
    assert set(robust['beats'])=={p for p in missing['beats'] if 1<=p<=25}


def test_dominant_v2_pair_reports_unmatched_real_pulses_and_never_claims_all_accents_match():
    value=rows()
    a=observation(120,phase=.12)
    a['beats']=sorted(a['beats']+[45.23,46.33])
    b=observation(122,phase=.31,md5='b',jitter=.008)
    b['beats'].pop(26)
    obs={'a':a,'b':b}
    assert not plan_rhythm(value,obs)['pairs'][0]['beatMatched']
    robust=plan_rhythm(value,obs,robust=True)
    proof=robust['pairs'][0]
    assert robust['version']==2 and proof['beatMatched']
    assert proof['method']=='dominant-observed-pulse-matching'
    assert proof['allAccentsMatched'] is False
    assert min(proof['leftMatchedPulseFraction'],proof['rightMatchedPulseFraction'])>=.8
    assert proof['unmatchedLeftObservedBeats']
    assert proof['medianPhaseErrorSeconds']<=.025 and proof['maxPhaseErrorSeconds']<=.05
    for left,right,error in proof['matchedObservedBeats']:
        assert left in a['beats'] and right in b['beats']
    assert value==rows()


@pytest.mark.parametrize('kind',['drift','noise','double','three-beat-hole','sparse-pulse'])
def test_dominant_v2_rejects_nonperiodic_or_ambiguous_rhythm_instead_of_relaxing_phase_error(kind):
    value=observation(120,phase=.1,end=30.)
    if kind=='drift':value['beats']=[.1+i*.5+.002*i*i for i in range(50)]
    elif kind=='noise':value['beats']=[.1+i*.5+.11*math.sin(i*2.1) for i in range(50)]
    elif kind=='double':value['beats']=[.1+i*.25 for i in range(110)]
    elif kind=='three-beat-hole':value['beats']=[x for i,x in enumerate(value['beats']) if i not in (20,21)]
    else:value['beats']=[x for i,x in enumerate(value['beats']) if i%4]
    assert not dominant_pulse_fit(value,1.,25.)['accepted']


def test_dominant_v2_does_not_double_a_measured_slow_pulse_to_follow_metadata_guess():
    value=observation(60,phase=.1,end=30.)
    value['metadataBpmUsedOnlyAsInitialGuess']=120
    result=dominant_pulse_fit(value,1.,25.)
    assert result['accepted'] and result['bpm']==60.


def test_v1_is_still_default_and_does_not_enter_the_robust_v2_route(monkeypatch):
    import aidj.fixed_set_rhythm as rhythm
    obs={'a':observation(120,phase=.12),'b':observation(122,phase=.31,md5='b')}
    expected=plan_rhythm(rows(),obs)
    def unexpected(*args,**kwargs):raise AssertionError('Default policy changed')
    monkeypatch.setattr(rhythm,'dominant_pulse_fit',unexpected)
    monkeypatch.setattr(rhythm,'_dominant_phase_candidate',unexpected)
    assert plan_rhythm(rows(),obs)==expected
    assert expected['version']==expected['pairs'][0]['version']==1
