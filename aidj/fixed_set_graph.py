"""A sparse, source-pinned transition graph for alternative DJ 1 cycles.

Every node keeps one constant playback rate. Edge timing is calculated at those
exact rates, so choosing another successor never invalidates the incoming mix.
The published cycle is retained as a connected seed; alternatives are ranked by
measured local tempo and approximate chroma, not arbitrary playlist shuffle.
"""
import hashlib
import json
import math

from .fixed_set_blend import artist_keys
from .fixed_set_rhythm import _role_fit, _dominant_phase_candidate
from .fixed_set_uniform import _tempo, _chroma, _approximate_phase

VERSION = 1
DEGREE = 10


def revision(graph):
    body = {k: graph[k] for k in ('version', 'baseRevision', 'degree', 'artists', 'edges')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:20]


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_graph(graph, plan):
    def need(condition, reason):
        if not condition:raise ValueError('FIXED_GRAPH_'+reason)
    need(isinstance(graph, dict) and type(graph.get('version')) is int and graph.get('version') == VERSION, 'VERSION')
    need(graph.get('baseRevision') == plan['revision'], 'STALE')
    need(type(graph.get('degree')) is int and 1 <= graph['degree'] <= DEGREE, 'DEGREE')
    need(isinstance(graph.get('artists'), dict) and isinstance(graph.get('edges'), dict), 'SHAPE')
    rows = plan['tracks'];ids = {r['trackId'] for r in rows};by_id = {r['trackId']: r for r in rows}
    need(set(graph['artists']) == set(graph['edges']) == ids, 'MEMBERSHIP')
    clean_edges = {};clean_artists = {};count = min(graph['degree'], len(rows)-1)
    for i, row in enumerate(rows):
        aid = row['trackId'];artists = graph['artists'][aid];edges = graph['edges'][aid]
        need(isinstance(artists, list) and 1 <= len(artists) <= 50 and
             all(isinstance(a, str) and 1 <= len(a) <= 256 for a in artists) and
             len(set(artists)) == len(artists), 'ARTISTS')
        clean_artists[aid] = list(artists)
        need(isinstance(edges, list) and (1 <= len(edges) <= count if count else len(edges) == 0), 'EDGE_COUNT')
        seen = set();clean_edges[aid] = []
        for edge in edges:
            need(isinstance(edge, dict), 'EDGE')
            to = edge.get('to');seconds = edge.get('transitionSeconds');kind = edge.get('transitionKind')
            need(isinstance(to, str) and to in ids and to != aid and to not in seen, 'TARGET');seen.add(to)
            need(finite(seconds) and finite(edge.get('score')) and edge['score'] >= 0 and
                 type(edge.get('beatMatched')) is bool, 'NUMBER')
            need((kind == 'crossfade' and 4.75 <= seconds <= 5.25) or
                 (kind == 'clean-cut' and seconds == 0), 'TRANSITION')
            # The only clean connection is the already authored final ending.
            if kind == 'clean-cut':need(i == len(rows)-1 and to == rows[0]['trackId'], 'CLEAN_EDGE')
            if seconds:
                need(all((r['end']-r['start'])/r['rate'] > 2*seconds for r in (row, by_id[to])), 'SHORT_NODE')
            clean_edges[aid].append({k: edge[k] for k in
                ('to', 'transitionSeconds', 'transitionKind', 'beatMatched', 'score')})
        if len(rows) > 1:
            successor = rows[(i+1) % len(rows)]['trackId']
            need(successor in seen, 'SEED_CYCLE')
            saved = next(e for e in clean_edges[aid] if e['to'] == successor)
            need(saved['transitionSeconds'] == row['transitionSeconds'] and
                 saved['transitionKind'] == row['transitionKind'], 'SEED_TIMING')
    out = {'version': VERSION, 'baseRevision': graph['baseRevision'], 'degree': graph['degree'],
           'artists': clean_artists, 'edges': clean_edges}
    out['revision'] = revision(out)
    need(graph.get('revision') == out['revision'], 'REVISION')
    return out


def compile_graph(plan, assets, observations, features=None, *, fits=None, degree=DEGREE):
    if type(degree) is not int or not 1 <= degree <= DEGREE:raise ValueError('FIXED_GRAPH_DEGREE')
    from .fixed_sets import validate
    plan = validate({k: v for k, v in plan.items() if k != 'graph'}, assets)
    rows = plan['tracks'];features = features or {};by_id = {r['trackId']: r for r in rows}
    if any((r['end']-r['start'])/r['rate'] <= 10.5 for r in rows):raise ValueError('FIXED_GRAPH_SHORT_NODE')
    fits = fits or {r['trackId']: {role: _role_fit(r, observations.get(r['trackId']), role, robust=True)
                                 for role in ('entry', 'exit')} for r in rows}
    tempos = {r['trackId']: {role: _tempo(r, role, fits[r['trackId']][role], observations, assets, features)
                           for role in ('entry', 'exit')} for r in rows}
    colors = {r['trackId']: _chroma(r, features) for r in rows}
    artists = {r['trackId']: sorted(artist_keys(assets[r['trackId']])) for r in rows}
    def score(left, right):
        aid, bid = left['trackId'], right['trackId']
        a, b = tempos[aid]['exit'][0], tempos[bid]['entry'][0]
        tempo = abs(math.log(a*left['rate']/(b*right['rate']))) if a and b else .18
        x, y = colors[aid], colors[bid]
        harmony = max(0, 1-sum(v*w for v, w in zip(x, y))) if x and y else .5
        uncertainty = int(not fits[aid]['exit'].get('accepted'))+int(not fits[bid]['entry'].get('accepted'))
        return tempo*12+harmony*.18+uncertainty*.10+bool(set(artists[aid]) & set(artists[bid]))*.20
    edges = {};audit = {};matched = 0
    for i, left in enumerate(rows):
        aid = left['trackId'];edges[aid] = [];audit[aid] = []
        if len(rows) == 1:continue
        successor = rows[(i+1) % len(rows)]
        ranked = sorted((r for r in rows if r['trackId'] != aid), key=lambda r:(score(left, r), r['trackId']))
        targets = [successor]
        # A good reverse neighbour enables varied cycles without spending the
        # ten choices on distant musical jumps merely to make a graph dense.
        predecessor = rows[(i-1) % len(rows)]
        a, b = tempos[aid]['exit'][0], tempos[predecessor['trackId']]['entry'][0]
        reverse_close = a and b and abs(math.log(a*left['rate']/(b*predecessor['rate']))) <= .035
        if (degree > 1 and predecessor['trackId'] != successor['trackId'] and reverse_close and
                predecessor in ranked[:max(degree*2, 1)]):targets.append(predecessor)
        for right in ranked:
            if len(targets) >= min(degree, len(rows)-1):break
            a, b = tempos[aid]['exit'][0], tempos[right['trackId']]['entry'][0]
            if a and b and abs(math.log(a*left['rate']/(b*right['rate']))) > .035:continue
            if right not in targets:targets.append(right)
        for right in targets:
            bid = right['trackId'];seed = bid == successor['trackId']
            seconds = left['transitionSeconds'] if seed else 5.
            kind = left['transitionKind'] if seed else 'crossfade'
            proof = None;approximation = None
            lf, rf = fits[aid]['exit'], fits[bid]['entry']
            if kind == 'crossfade':
                eof = abs(left['end']-assets[aid]['duration']) <= .001
                if not eof and lf.get('accepted') and rf.get('accepted'):
                    candidate = _dominant_phase_candidate(left, right, lf, rf, left['rate'], right['rate'],
                                                         seconds if seed else 5., 0. if seed else .25)
                    if candidate.get('beatMatched'):proof = candidate
                if not seed:
                    if proof:seconds = proof['transitionSeconds']
                    else:
                        approximation = _approximate_phase(left, right, observations, 5., .25)
                        if approximation:seconds = approximation['transitionSeconds']
            edge = {'to': bid, 'transitionSeconds': seconds, 'transitionKind': kind,
                    'beatMatched': proof is not None, 'score': round(score(left, right), 6)}
            edges[aid].append(edge)
            audit[aid].append({**edge, 'leftRate': left['rate'], 'rightRate': right['rate'],
                'seedCycle': seed, 'proof': proof, 'approximation': approximation,
                'leftTempoBasis': tempos[aid]['exit'], 'rightTempoBasis': tempos[bid]['entry']})
            matched += bool(proof)
        edges[aid].sort(key=lambda e:(e['score'], e['to']))
    graph = {'version': VERSION, 'baseRevision': plan['revision'], 'degree': degree,
             'artists': artists, 'edges': edges}
    graph['revision'] = revision(graph);graph = validate_graph(graph, plan)
    return {'graph': graph, 'pairs': audit, 'fits': fits, 'tempoBases': tempos,
            'summary': {'tracks': len(rows), 'edges': sum(map(len, edges.values())),
                        'minimumOutdegree': min(map(len, edges.values())),
                        'maximumOutdegree': max(map(len, edges.values())),
                        'confirmedPulseEdges': matched, 'constantNodeRates': True,
                        'sourceCutsPreserved': True, 'baselineCyclePreserved': True}}
