"""Sparse backups are measured, distributed, provisional, and feedback-safe."""
import copy

import pytest

from aidj.dj import filter_boundary_points,validate
from aidj.dj_worker import acoustic_graph,_strict_acoustic_graph,anchor_graph,selection


def evidence(duration=120,words=None,regions=None,beats=None,energy=None):
    return {'version':3,'duration':duration,'words':words or [],'coverage':[[0,duration]],
            'probes':[],'bpm':120,'beats':beats or [],
            'energy':[.5]*int(duration) if energy is None else energy,
            'acousticFeatures':{'hop':.02,'regions':regions or []}}


def assert_review_graph(graph,duration):
    assert graph['quality']=='acoustic' and graph['wordGuarded'] is False and graph['needsReview'] is True
    assert graph['entries'][0][0]==0
    for kind in ('entries','exits'):
        assert graph[kind]==sorted(graph[kind])
        assert len(graph[kind])<=256 and len({round(row[0]*1000) for row in graph[kind]})==len(graph[kind])
        assert all(0<=row[0]<=duration for row in graph[kind])
    assert all(row[1]>=.5 for row in graph['exits'])
    assert validate({'cueSchema':4,'acousticAnchors':graph},duration)['acousticAnchors']==graph


@pytest.mark.parametrize('confident',[False,True])
def test_continuous_vocals_get_approximate_options_without_fabricating_primary_verification(confident):
    e=evidence(words=[(s,s+1,confident) for s in range(120)])
    original=copy.deepcopy(e);primary=anchor_graph(e);graph=acoustic_graph(e)
    assert not primary['exits'] and anchor_graph(e)==primary
    assert len(graph['entries'])>=12 and len(graph['exits'])>=12
    assert_review_graph(graph,120)
    assert e==original  # A cached refresh never edits the original observations.
    selected=selection(e)
    assert selected['needsReview'] and not selected['fullFallback']
    assert not selected['anchors']['exits']


def test_instrumental_quiet_plateaus_and_observed_attacks_are_valid_provisional_sources():
    e=evidence(regions=[[20,21,.05],[60,61,.02]],beats=[12.25,40.25,90.25],energy=[])
    strict=_strict_acoustic_graph(e);graph=acoustic_graph(e)
    assert len(graph['entries'])>=len(strict['entries'])
    assert all(row in graph['entries'] for row in strict['entries'])
    assert all(row in graph['exits'] for row in strict['exits'])
    # A 20ms lead to an actual observed attack no longer requires350ms quiet.
    assert any(abs(row[0]-90.23)<1e-6 for row in graph['entries'])
    assert_review_graph(graph,120)


def test_rich_strict_graph_is_identical_and_supplementation_is_per_role():
    e=evidence(regions=[[s,s+.8,.02] for s in range(2,116,3)],beats=[0,.5,1])
    strict=_strict_acoustic_graph(e)
    assert len(strict['entries'])>=10 and len(strict['exits'])>=10
    assert acoustic_graph(e)==strict


def test_uniform_measured_energy_gets_weak_distributed_choices_instead_of_fake_words():
    e=evidence(duration=180,energy=[.6]*180);graph=acoustic_graph(e)
    assert len(graph['entries'])>=12 and len(graph['exits'])>=12
    assert_review_graph(graph,180)
    for kind in ('entries','exits'):
        times=[0]+[row[0] for row in graph[kind]]+[180]
        assert max(b-a for a,b in zip(times,times[1:]))<=16
        assert graph[kind][-1][0]>150
    assert anchor_graph(e)['entries']==[[0,.6]]


def test_many_clustered_points_still_fill_observed_middle_of_song():
    e=evidence(duration=180,regions=[[s,s+.8,.02] for s in range(2,28,2)])
    strict=_strict_acoustic_graph(e);graph=acoustic_graph(e)
    assert min(len(strict['entries']),len(strict['exits']))>=10
    for kind in ('entries','exits'):
        assert all(row in graph[kind] for row in strict[kind])
        assert sum(40<row[0]<150 for row in graph[kind])>=10
    assert_review_graph(graph,180)


def test_no_observations_and_tiny_inputs_do_not_get_a_manufactured_twelve_point_grid():
    empty=evidence(120,energy=[])
    assert acoustic_graph(empty)==_strict_acoustic_graph(empty)
    for duration in (.1,2,8):
        e=evidence(duration,energy=[.5]*int(duration),beats=[0,.5,1])
        assert acoustic_graph(e)==_strict_acoustic_graph(e)
        assert len(acoustic_graph(e)['entries'])<10


@pytest.mark.parametrize('index',range(20))
def test_twenty_varied_sources_keep_ten_choices_after_rejected_physical_points_are_removed(index):
    duration=60+index*3
    words=[(float(s),s+.9,index%2==0) for s in range(duration)]
    graph=acoustic_graph(evidence(duration,words=words))
    # A rejection removes the same physical instant from every role. Two
    # independent bad labels still fit the deliberately small12-point margin.
    rejected=[round(graph['entries'][3][0]*1000),round(graph['exits'][5][0]*1000)]
    value=filter_boundary_points({'cueSchema':4,'acousticAnchors':graph},rejected)
    kept=value['acousticAnchors']
    assert len(kept['entries'])>=10 and len(kept['exits'])>=10
    assert all(abs(row[0]*1000-bad)>20 for kind in ('entries','exits') for row in kept[kind] for bad in rejected)
    assert_review_graph(kept,duration)


def test_sparse_precise_points_survive_even_when_asr_penalizes_other_backup_choices():
    e=evidence(words=[(10,20,True),(50,70,False)],regions=[[30,32,.02]])
    strict=_strict_acoustic_graph(e);graph=acoustic_graph(e)
    for kind in ('entries','exits'):assert all(row in graph[kind] for row in strict[kind])
    assert_review_graph(graph,120)


def test_one_good_primary_pair_does_not_hide_supplemented_options_from_review_collection():
    words=[(s,s+.4,True) for s in range(9)]+[(32,32.5,True),(35.5,36,True)]
    value=selection(evidence(80,words=words))
    assert value['needsReview'] is False
    assert value['reviewRecommended'] is True
    assert len(value['acousticAnchors']['entries'])>=12
