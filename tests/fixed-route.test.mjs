import test from 'node:test';
import assert from 'node:assert/strict';
import {graphReady,graphRoute} from '../miniapp/fixed-route.mjs';
import {fixedSetEntries} from '../miniapp/dj.mjs';

function random(seed){return ()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};}
function fixture(n=9,{sparse=false,ring=false}={}){
  const rows=Array.from({length:n},(_,i)=>({trackId:'t'+i,md5:i.toString(16).padStart(32,'0'),
    start:20+i*.1,end:82+i*.1,fadeIn:5,fadeOut:.4,rate:1+(i%3-1)*.005,
    transitionSeconds:i===n-1?0:5,transitionKind:i===n-1?'clean-cut':'crossfade'}));
  const tracks=new Map(rows.map(row=>[row.trackId,{id:row.trackId,md5:row.md5,duration:180,title:row.trackId,artist:'A'}]));
  const degree=sparse?1:Math.min(10,n-1),graph={version:1,revision:'a'.repeat(20),baseRevision:'b'.repeat(20),degree,artists:{},edges:{}};
  for(let i=0;i<n;i++){
    graph.artists[rows[i].trackId]=['artist'+i];
    const destinations=sparse?[(i+1)%n]:ring?[...Array(5)].flatMap((_,j)=>[(i+j+1)%n,(i-j-1+n)%n]):rows.map((_,j)=>j).filter(j=>j!==i);
    graph.edges[rows[i].trackId]=[...new Set(destinations)].slice(0,degree).map(j=>({to:rows[j].trackId,
      transitionSeconds:i===n-1&&j===0?0:5,transitionKind:i===n-1&&j===0?'clean-cut':'crossfade',score:Math.abs(i-j),beatMatched:false}));
  }
  return {set:{id:'dj1',playlistId:1,revision:graph.baseRevision,tracks:rows,graph},tracks};
}
function canonical(ids){const at=ids.indexOf([...ids].sort()[0]);return [...ids.slice(at),...ids.slice(0,at)].join(',');}
function check(result,set){
  assert.equal(new Set(result.trackIds).size,result.trackIds.length);
  for(let i=0;i<result.entries.length;i++){
    const entry=result.entries[i],row=set.tracks.find(r=>r.trackId===entry.trackId);
    const edge=set.graph.edges[entry.trackId].find(e=>e.to===result.entries[(i+1)%result.entries.length].trackId);
    assert(edge,'every actual outgoing edge, including closure, is published');
    assert.equal(entry.fixed.transitionSeconds,edge.transitionSeconds);assert.equal(entry.fixed.transitionKind,edge.transitionKind);
    for(const key of ['start','end','fadeIn','fadeOut','rate'])assert.equal(entry.fixed[key],row[key]);
    assert.equal(entry.fixed.sourceMd5,row.md5);
    if(result.entries.length>2){
      const a=set.graph.artists[entry.trackId],b=set.graph.artists[result.entries[(i+1)%result.entries.length].trackId],c=set.graph.artists[result.entries[(i+2)%result.entries.length].trackId];
      assert(!a.some(key=>b.includes(key)&&c.includes(key)),'max two shared artists at cyclic boundary');
    }
  }
}

test('prepared graph routes preserve all nodes, cuts and edge-valid closure with varied deterministic cycles',()=>{
  const {set,tracks}=fixture(),before=JSON.stringify(set),cycles=new Set();
  assert(graphReady(set));
  for(let seed=1;seed<=30;seed++){
    const result=graphRoute(set,tracks,{random:random(seed)});
    const same=graphRoute(set,tracks,{random:random(seed)});
    assert.deepEqual(result.trackIds,same.trackIds);assert.equal(result.coverage,set.tracks.length);
    check(result,set);cycles.add(canonical(result.trackIds));
  }
  assert(cycles.size>15,'randomness changes actual edges, not only the opening');
  assert.equal(JSON.stringify(set),before,'published graph and source rows remain immutable');
});

test('directed sparse cycle cannot be replaced by an unrestricted random shuffle',()=>{
  const {set,tracks}=fixture(20,{sparse:true});
  const result=graphRoute(set,tracks,{random:random(42),startId:'t7'});
  assert.equal(result.trackIds[0],'t7');assert.equal(result.coverage,20);assert.equal(result.changes,0);
  check(result,set);
  delete set.graph.edges.t19;
  assert.throws(()=>graphRoute(set,tracks),/изменились/);
});

test('active shuffle pins current and already mixed next source byte-for-byte without mutating the seed',()=>{
  const {set,tracks}=fixture(12,{ring:true}),base=fixedSetEntries(set,tracks);
  const seed=[...base.slice(6),...base.slice(0,6)],before=JSON.stringify(seed);
  const result=graphRoute(set,tracks,{random:random(8),seedEntries:seed,prefixLength:3});
  assert.equal(result.entries[0],seed[0]);assert.equal(result.entries[1],seed[1]);
  assert.equal(result.entries[2].id,seed[2].id);
  assert.deepEqual(result.trackIds.slice(0,3),seed.slice(0,3).map(e=>e.trackId));
  assert.equal(result.coverage,12);assert.equal(JSON.stringify(seed),before);check(result,set);
  const stale=structuredClone(seed);stale[0].fixed.transitionSeconds=4.9;
  assert.throws(()=>graphRoute(set,tracks,{seedEntries:stale,prefixLength:3}),/изменились/);
  stale[0].fixed.transitionSeconds=5;stale[1].fixed.rate=1.01;
  assert.throws(()=>graphRoute(set,tracks,{seedEntries:stale,prefixLength:3}),/изменились/);
});

test('shared artist limit includes collaborators and both cycle boundaries',()=>{
  const {set,tracks}=fixture(12,{ring:true});
  for(let i=0;i<12;i++)set.graph.artists['t'+i]=['lead'+(i%3),'guest'+(i%4)];
  for(let seed=1;seed<=80;seed++)check(graphRoute(set,tracks,{random:random(seed)}),set);
});

test('pinning only a node does not incorrectly preserve its old outgoing timing',()=>{
  const {set,tracks}=fixture(8),base=fixedSetEntries(set,tracks),seed=[base.at(-1),...base.slice(0,-1)];
  for(let s=1;s<=20;s++){
    const route=graphRoute(set,tracks,{seedEntries:seed,prefixLength:1,random:random(s)});
    assert.equal(route.entries[0].id,seed[0].id);check(route,set);
  }
});

test('a few songs may be omitted only through existing shortcut edges, with bounded work',()=>{
  const {set,tracks}=fixture(8);
  set.graph.artists.t6=['A'];set.graph.artists.t7=['A'];set.graph.artists.t0=['A'];
  const result=graphRoute(set,tracks,{random:()=>0,attempts:20});
  assert(result.coverage>=7);check(result,set);
  for(const id of Object.keys(set.graph.artists))set.graph.artists[id]=['same'];
  let calls=0;
  assert.throws(()=>graphRoute(set,tracks,{random:()=>{calls++;return .4;}}),/Не удалось собрать/);
  assert(calls<=8001,'impossible credit constraint cannot loop indefinitely');
});

test('stale source identity, manual bounds, graph version and malformed edge refuse playback',()=>{
  for(const patch of [
    (set,tracks)=>{tracks.get('t0').md5='different';},
    (set,tracks)=>{tracks.get('t0').rangeStart=30;},
    set=>{set.graph.baseRevision='c'.repeat(20);},
    set=>{set.graph.edges.t0[0].to='missing';},
    set=>{set.graph.edges.t0[0].transitionSeconds=60;},
    set=>{set.graph.edges.t0[0].transitionKind='whatever';},
    set=>{set.graph.artists.t0=[];},
  ]){
    const {set,tracks}=fixture();patch(set,tracks);
    assert.throws(()=>graphRoute(set,tracks),/изменились/);
  }
});

test('a 531-song sparse-neighborhood graph is routed without dropping the library',()=>{
  const {set,tracks}=fixture(531,{ring:true}),cycles=new Set();
  for(let seed=1;seed<=10;seed++){
    const result=graphRoute(set,tracks,{random:random(seed)});check(result,set);
    assert.equal(result.coverage,531);cycles.add(canonical(result.trackIds));
  }
  assert.equal(cycles.size,10);
});

test('one-song graphs have no shuffle control and invalid random/attempt budgets fail fast',()=>{
  const {set,tracks}=fixture();
  assert.throws(()=>graphRoute(set,tracks,{random:()=>1}),/Не удалось перемешать/);
  assert.throws(()=>graphRoute(set,tracks,{attempts:4001}),/изменились/);
  assert(!graphReady({...set,tracks:set.tracks.slice(0,1)}));
});
