"""Acoustic entry candidates need a measured breath, not a bare beat timestamp."""
import pytest
from aidj.dj_worker import _strict_acoustic_graph as acoustic_graph,selection,usable_anchors


def evidence(words,regions,duration=140,bpm=120,beats=None):
    return {'version':3,'duration':duration,'words':words,'coverage':[[0,duration]],'probes':[],
            'bpm':bpm,'beats':beats if beats is not None else [0,.5,1],
            'energy':[.5]*int(duration),'acousticFeatures':{'hop':.02,'regions':regions}}


def entries(value):return [t for t,_ in acoustic_graph(value)['entries']]


@pytest.mark.parametrize('t,word',[(51.95,(51.9,52.22,False)),(95.744,(95.6,96.08,False))])
def test_detected_uncertain_word_interior_is_blocked_even_inside_a_measured_valley(t,word):
    value=evidence([word],[[t-.05,t+.8,.05]],beats=[t%8])
    graph=acoustic_graph(value)
    assert not any(word[0]-.4<t<word[1]+.4 for t,_ in graph['entries'])
    assert all(not (word[0]-.2<t and word[1]+.2>t-fade) for t,fade,_ in graph['exits'])
    assert graph['wordGuarded'] is False and graph['needsReview'] is True


def test_entry_cannot_start_only_320ms_after_previous_word():
    value=evidence([(69.82,70.16,True)],[[70.43,71.2,.05]],beats=[70.48%8])
    starts=entries(value)
    assert not any(abs(t-70.48)<.001 for t in starts)
    assert all(t==0 or t>=70.56-1e-6 for t in starts)
    assert any(70.56<t<71 for t in starts)  # A later measured alternative survives.


def test_an_actual_breath_survives_with_post_point_headroom_and_without_moving_into_next_word():
    value=evidence([(10,10.5,False),(12,12.5,True)],[[10.7,12.2,.05]],beats=[11%8])
    starts=entries(value)
    assert any(abs(t-11.45)<.001 for t in starts)
    assert all(t==0 or 10.9-1e-6<=t<=11.65+1e-6 for t in starts)
    assert all(t==0 or t+.35<=12+1e-6 for t in starts)


def test_measured_valley_without_enough_remaining_breath_is_not_an_entry():
    value=evidence([],[[20,20.3,.05]],beats=[20%8])
    assert entries(value)==[0]
    value['acousticFeatures']['regions']=[[20,20.9,.05]]
    assert any(20<t<=20.55+1e-6 for t in entries(value))


def test_a_bare_or_extrapolated_beat_does_not_make_an_unobserved_vocal_entry_safe():
    value=evidence([],[],beats=[0,.5,1])
    graph=acoustic_graph(value)
    assert [t for t,_ in graph['entries']]==[0]
    assert graph['exits']  # Exits remain separately available/provisional.
    value['acousticFeatures']['regions']=[[31.9,32.8,.05]]
    assert any(abs(t-32)<.001 for t in entries(value))
    assert not any(abs(t-40)<.001 for t in entries(value))


def test_blocked_strict_points_remain_distinct_from_explicitly_provisional_backups():
    value=evidence([(s,s+1,False) for s in range(180)],[[20,21,.05],[80,81,.05]],duration=180)
    result=selection(value)
    strict=acoustic_graph(value)
    assert strict['entries']==[[0,.5]] and not usable_anchors(strict)
    assert result['needsReview'] and not result['fullFallback']
    assert result['acousticAnchors']['wordGuarded'] is False
    assert len(result['acousticAnchors']['entries'])>=12
