import copy
import json

import pytest

from aidj.fixed_set_graph import compile_graph, validate_graph, revision
from aidj.fixed_sets import validate, signature, publish, records


def fixture(n=5):
    rows=[];assets={};observations={}
    for i in range(n):
        aid=f'{i+1:024x}';md5=f'{i+1:032x}'
        assets[aid]={'id':aid,'md5':md5,'artist':'Artist '+str(i),'title':'Song '+str(i),'duration':100.}
        rows.append({'trackId':aid,'md5':md5,'start':10.,'end':70.,'fadeIn':5.,'fadeOut':.4,
                     'rate':1.,'transitionSeconds':5. if i<n-1 else 0.,
                     'transitionKind':'crossfade' if i<n-1 else 'clean-cut'})
        obs={'md5':md5,'sourceOffsetSeconds':0.,'sourceEndSeconds':100.,
             'estimatedBpm':120.,'beats':[.12+i*.5 for i in range(200)]}
        observations[aid]={'entry':obs,'exit':copy.deepcopy(obs)}
    return {'id':'dj1','playlistId':1,'revision':signature(rows),'tracks':rows},assets,observations


def test_graph_preserves_source_cuts_rates_and_exact_connected_seed_cycle():
    plan,assets,obs=fixture();before=copy.deepcopy(plan)
    result=compile_graph(plan,assets,obs,degree=4);g=result['graph']
    assert plan==before and g['baseRevision']==plan['revision']
    assert result['summary']['edges']==20 and result['summary']['constantNodeRates']
    for i,left in enumerate(plan['tracks']):
        edges=g['edges'][left['trackId']]
        assert len(edges)==4 and len({e['to'] for e in edges})==4
        right=plan['tracks'][(i+1)%5]
        edge=next(e for e in edges if e['to']==right['trackId'])
        assert all(edge[k]==left[k] for k in ('transitionSeconds','transitionKind'))
        assert left['trackId'] not in {e['to'] for e in edges}
    assert validate({**plan,'graph':g},assets)['graph']==g


def test_strict_graph_proofs_use_real_local_observations_at_fixed_node_rates():
    plan,assets,obs=fixture(3)
    result=compile_graph(plan,assets,obs)
    assert result['summary']['confirmedPulseEdges']>0
    for aid,pairs in result['pairs'].items():
        for p in pairs:
            if not p['beatMatched']:continue
            proof=p['proof'];left=next(r for r in plan['tracks'] if r['trackId']==aid)
            right=next(r for r in plan['tracks'] if r['trackId']==p['to'])
            assert p['leftRate']==left['rate'] and p['rightRate']==right['rate']
            assert proof['leftAnchorSeconds'] in obs[aid]['exit']['beats']
            assert proof['rightAnchorSeconds'] in obs[right['trackId']]['entry']['beats']
            assert proof['transitionSeconds']==p['transitionSeconds']


def test_missing_rhythm_never_becomes_a_confirmed_graph_edge():
    plan,assets,_=fixture(3)
    result=compile_graph(plan,assets,{})
    assert result['summary']['confirmedPulseEdges']==0
    assert all(not e['beatMatched'] for edges in result['graph']['edges'].values() for e in edges)
    assert all(4.75<=e['transitionSeconds']<=5.25 for edges in result['graph']['edges'].values()
               for e in edges if e['transitionKind']=='crossfade')


@pytest.mark.parametrize('kind', ['wrong-base','unknown-target','self','duplicate','bad-length',
                                  'nan','extra-clean','seed-timing','missing-node','wrong-proof','revision'])
def test_graph_validation_rejects_stale_or_invented_connections(kind):
    plan,assets,obs=fixture(3);g=compile_graph(plan,assets,obs)['graph'];aid=plan['tracks'][0]['trackId']
    if kind=='wrong-base':g['baseRevision']='0'*20
    elif kind=='unknown-target':g['edges'][aid][0]['to']='unknown'
    elif kind=='self':g['edges'][aid][0]['to']=aid
    elif kind=='duplicate':g['edges'][aid][1]=copy.deepcopy(g['edges'][aid][0])
    elif kind=='bad-length':g['edges'][aid][0]['transitionSeconds']=1.2
    elif kind=='nan':g['edges'][aid][0]['score']=float('nan')
    elif kind=='extra-clean':g['edges'][aid][0].update(transitionSeconds=0.,transitionKind='clean-cut')
    elif kind=='seed-timing':
        to=plan['tracks'][1]['trackId'];next(e for e in g['edges'][aid] if e['to']==to)['transitionSeconds']=4.9
    elif kind=='missing-node':g['edges'].pop(aid)
    elif kind=='wrong-proof':g['edges'][aid][0]['beatMatched']='yes'
    else:g['revision']='0'*20
    with pytest.raises(ValueError,match='FIXED_GRAPH_'):validate_graph(g,plan)


def test_source_change_invalidates_graph_binding_and_short_nodes_are_rejected():
    plan,assets,obs=fixture(3);g=compile_graph(plan,assets,obs)['graph']
    plan['tracks'][0]['rate']=1.01;plan['revision']=signature(plan['tracks'])
    with pytest.raises(ValueError,match='FIXED_GRAPH_STALE'):validate({**plan,'graph':g},assets)
    plan['tracks'][0]['end']=19.;plan['revision']=signature(plan['tracks'])
    with pytest.raises(ValueError,match='FIXED_GRAPH_SHORT_NODE'):compile_graph(plan,assets,obs)


def test_small_library_degrees_remain_bounded_and_one_song_has_no_self_edge():
    for n in (1,2):
        plan,assets,obs=fixture(n);g=compile_graph(plan,assets,obs)['graph']
        assert all(len(e)==n-1 for e in g['edges'].values())


def test_distant_extra_tempos_do_not_fill_a_quota_with_bad_connections():
    plan,assets,obs=fixture(4)
    ids=[r['trackId'] for r in plan['tracks']]
    for role in ('entry','exit'):
        # No observations can certify phase, but local estimates still reveal
        # that the outlier has no additional compatible tempo neighbours.
        obs[ids[0]][role]['beats']=[];obs[ids[0]][role]['estimatedBpm']=70.
    g=compile_graph(plan,assets,obs)['graph']
    assert [e['to'] for e in g['edges'][ids[0]]]==[ids[1]]
    assert ids[0] not in [e['to'] for e in g['edges'][ids[2]]]
    assert next(e for e in g['edges'][ids[-1]] if e['to']==ids[0])['transitionKind']=='clean-cut'


def test_invalid_optional_graph_does_not_hide_an_existing_valid_set():
    from tests.test_fixed_sets import database,manifest
    with database() as db:
        value=publish(db,manifest());value['graph']={'version':1,'baseRevision':'stale'}
        db.execute('UPDATE dj_fixed_sets SET payload=?',(json.dumps(value),));db.commit()
        assert records(db)==[publish(db,manifest())]


def test_explicit_boundary_correction_drops_edge_timings_for_the_old_cuts():
    from aidj.fixed_sets import revise_boundary
    plan,assets,obs=fixture(3);plan['graph']=compile_graph(plan,assets,obs)['graph']
    row=plan['tracks'][0]
    changed=revise_boundary(plan,{'trackId':row['trackId'],'md5':row['md5'],'previousEnd':70.,
        'end':69.,'fadeOut':.2,'quietWindow':{'start':68.,'end':70.,'maxRelativeRms':.01,'lastVocalEnd':68.}},assets)
    assert 'graph' not in changed and changed['revision']!=plan['revision']
