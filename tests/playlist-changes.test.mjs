import test from 'node:test';
import assert from 'node:assert/strict';
import {PlaylistChanges} from '../miniapp/playlist-changes.mjs';

const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const copy=x=>structuredClone(x);
const catalog=(ids=['a','b'],revision='r1')=>({tracks:['a','b','c'].map(id=>({id,md5:id,title:id})),playlists:[{id:1,trackIds:ids,revision}]});
const http=status=>Object.assign(new Error('Rejected'),{status});
function setup(t,options={}) {
  const state={saved:[],cached:null,display:null,errors:[],calls:[]};
  const changes=new PlaylistChanges({save:async v=>{state.saved=copy(v);},cache:async v=>{state.cached=copy(v);},
    changed:v=>{state.display=copy(v);},failed:(e,item)=>state.errors.push({e,item}),
    send:async body=>{state.calls.push(copy(body));return catalog();},refresh:async()=>catalog(),...options});
  changes.restore(catalog());t.after(()=>clearTimeout(changes.timer));return {changes,state};
}
const ids=c=>c.project().playlists[0].trackIds;
const enqueue=(c,action,trackId)=>c.enqueue(action,{playlistId:1,trackId});

test('combined playlists follow offline source changes with shared metadata and MD5 deduplication',async t=>{
  const {changes:c}=setup(t,{canSend:()=>false});
  const base=catalog();base.tracks[0].rating={likes:4};base.tracks[0].djV4={anchors:{version:1}};
  base.tracks.push({id:'alias-a',md5:'a',title:'Alias'});
  base.playlists.push({id:2,trackIds:['alias-a','c'],revision:'s1'},
    {id:900001001,combined:true,sourcePlaylistIds:[1,2],trackIds:[],revision:'mix1'});
  c.restore(base);
  const combined=()=>c.project().playlists.find(p=>p.combined).trackIds;
  assert.deepEqual(combined(),['a','b','c']);
  assert.strictEqual(c.project().tracks[0],base.tracks[0]);
  await enqueue(c,'remove','a');assert.deepEqual(combined(),['b','alias-a','c']);
  await c.enqueue('remove',{playlistId:2,trackId:'alias-a'});assert.deepEqual(combined(),['b','c']);
  await enqueue(c,'add','a');assert.deepEqual(combined(),['a','b','c']);
  assert.deepEqual(base.playlists[0].trackIds,['a','b']);
  await assert.rejects(c.enqueue('remove',{playlistId:900001001,trackId:'a'}),/исходных плейлистов/);
});

test('add/remove update the UI before a slow Disk response; requests stay serial',async t=>{
  const first=deferred(),calls=[];
  const {changes:c}=setup(t,{send:async body=>{calls.push(body);return calls.length===1?first.promise:catalog(['c','b'],'r3');}});
  await enqueue(c,'remove','a');await enqueue(c,'add','c');
  assert.deepEqual(ids(c),['c','b']);assert.equal(calls.length,1);
  first.resolve(catalog(['b'],'r2'));await c.flush();
  assert.deepEqual(calls.map(x=>[x.action,x.revision]),[['remove','r1'],['add','r2']]);
  assert.deepEqual(ids(c),['c','b']);assert.deepEqual(c.pending,[]);
});

test('remove then immediately re-add keeps the latest intent visible during both requests',async t=>{
  const one=deferred(),two=deferred();let n=0;
  const {changes:c}=setup(t,{send:()=>++n===1?one.promise:two.promise});
  await enqueue(c,'remove','a');await enqueue(c,'add','a');
  assert.deepEqual(ids(c),['a','b']);
  one.resolve(catalog(['b'],'r2'));
  while(n<2)await new Promise(r=>setImmediate(r));
  assert.deepEqual(ids(c),['a','b']);
  two.resolve(catalog(['a','b'],'r3'));await c.flush();assert.deepEqual(c.pending,[]);
});

test('a background catalog refresh cannot resurrect a pending removal',async t=>{
  const {changes:c}=setup(t,{canSend:()=>false});await enqueue(c,'remove','a');
  await c.accept(catalog(['a','b','c'],'external'));
  assert.deepEqual(ids(c),['b','c']);
});

test('offline intents survive restart and use the current revision when first sent',async t=>{
  const {changes:c,state}=setup(t,{canSend:()=>false});await enqueue(c,'add','c');await enqueue(c,'remove','a');
  const calls=[];
  const {changes:resumed}=setup(t,{send:async body=>{calls.push(body);return calls.length===1?catalog(['c','a','b'],'r8'):catalog(['c','b'],'r9');}});
  resumed.restore(catalog(['a','b'],'r7'),state.saved);
  assert.deepEqual(ids(resumed),['c','b']);await resumed.flush();
  assert.deepEqual(calls.map(x=>x.revision),['r7','r8']);assert.equal(resumed.pending.length,0);
});

test('a lost response replays the same durable opId after restart, without repeating its effect',async t=>{
  const operations=new Set();let effects=0,calls=0;
  const send=async body=>{
    calls++;if(!operations.has(body.opId)){operations.add(body.opId);effects++;}
    if(calls===1)throw new Error('Response lost after commit');
    return catalog(['b'],'r2');
  };
  const {changes:c,state}=setup(t,{send});await enqueue(c,'remove','a');await c.flush();
  const op=state.saved[0];assert.equal(op.revision,'r1');assert.deepEqual(ids(c),['b']);
  const {changes:resumed}=setup(t,{send});resumed.restore(catalog(['a','b'],'r1'),state.saved);await resumed.flush();
  assert.equal(effects,1);assert.equal(calls,2);assert.equal(resumed.pending.length,0);assert.ok(operations.has(op.opId));
});

test('a confirmed rejection rolls back only that change and preserves later intentions',async t=>{
  const gate=deferred();let n=0;const failures=[];
  const {changes:c}=setup(t,{send:async()=>{if(++n===1){await gate.promise;throw http(400);}return catalog(['c','a','b'],'r3');},
    failed:(error,item)=>failures.push(item)});
  await enqueue(c,'remove','a');await enqueue(c,'add','c');gate.resolve();await c.flush();
  assert.deepEqual(ids(c),['c','a','b']);assert.deepEqual(failures.map(x=>x.action),['remove']);assert.equal(c.pending.length,0);
});

test('503 and expired authentication retain pending intents rather than rolling back',async t=>{
  for(const status of [503,401,403]) {
    const {changes:c,state}=setup(t,{send:async()=>{throw http(status);}});
    await enqueue(c,'remove','a');await c.flush();
    assert.deepEqual(ids(c),['b']);assert.equal(state.saved.length,1);assert.equal(state.errors.length,0);
  }
});

test('revision conflicts rebase the same membership operation and retain external edits',async t=>{
  const calls=[];
  const {changes:c}=setup(t,{refresh:async()=>catalog(['c','a','b'],'other-device'),send:async body=>{
    calls.push(body);if(calls.length===1)throw http(409);return catalog(['c','b'],'r3');
  }});
  await enqueue(c,'remove','a');await c.flush();assert.deepEqual(ids(c),['c','b']);
  await c.flush();assert.equal(calls[0].opId,calls[1].opId);assert.equal(calls[1].revision,'other-device');
  assert.deepEqual(ids(c),['c','b']);
});

test('an older catalog response cannot undo a confirmed operation',async t=>{
  const {changes:c}=setup(t,{send:async()=>catalog(['b'],'r2')});const generation=c.generation;
  await enqueue(c,'remove','a');await c.flush();
  assert.equal(await c.accept(catalog(),generation),false);assert.deepEqual(ids(c),['b']);
});

test('failed local persistence never starts a request or claims success',async t=>{
  let requests=0;const {changes:c}=setup(t,{save:async()=>{throw new Error('Storage full');},send:async()=>{requests++;}});
  await assert.rejects(enqueue(c,'remove','a'),/Storage full/);
  assert.equal(requests,0);assert.equal(c.pending.length,0);assert.deepEqual(ids(c),['a','b']);
});

test('a failed acknowledgement write retries the same operation, including after a catalog write',async t=>{
  const calls=[];let fail=true;
  const {changes:c,state}=setup(t,{save:async pending=>{
    if(!pending.length&&fail){fail=false;throw new Error('Interrupted acknowledgement');}state.saved=copy(pending);
  },send:async body=>{calls.push(body);return catalog(['b'],'r2');}});
  await enqueue(c,'remove','a');await c.flush();assert.equal(c.pending.length,1);
  assert.equal(state.cached.playlists[0].revision,'r2');await c.flush();
  assert.equal(calls.length,2);assert.equal(calls[0].opId,calls[1].opId);assert.equal(c.pending.length,0);
});

test('MD5 aliases do not duplicate adds and removal targets the playlist canonical track',async t=>{
  const calls=[];const {changes:c}=setup(t,{send:async body=>{calls.push(body);return catalog(['b'],'r2');}});
  const base=catalog();base.tracks.push({id:'alias-a',md5:'a'});c.restore(base);
  await c.accept(base);c.canSend=()=>false;await enqueue(c,'add','alias-a');
  assert.deepEqual(ids(c),['a','b']);await enqueue(c,'remove','alias-a');assert.deepEqual(ids(c),['b']);
  await c.flush();
  const {changes:removal}=setup(t,{send:async body=>{calls.push(body);return catalog(['b'],'r2');}});
  removal.restore(base);await enqueue(removal,'remove','alias-a');await removal.flush();assert.equal(calls[0].trackId,'a');
});

test('a double tap queues one operation rather than two removals',async t=>{
  const {changes:c}=setup(t,{canSend:()=>false});
  await Promise.all([enqueue(c,'remove','a'),enqueue(c,'remove','a')]);assert.equal(c.pending.length,1);
});

test('metadata writes are serialized when changes are made in quick succession',async t=>{
  const gate=deferred(),snapshots=[];let n=0;
  const {changes:c}=setup(t,{canSend:()=>false,save:async value=>{if(++n===1)await gate.promise;snapshots.push(copy(value));}});
  const a=enqueue(c,'remove','a'),b=enqueue(c,'add','c');gate.resolve();await Promise.all([a,b]);
  assert.deepEqual(snapshots.map(s=>s.map(x=>x.action)),[['remove'],['remove','add']]);assert.deepEqual(ids(c),['c','b']);
});
