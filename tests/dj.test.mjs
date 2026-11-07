import test from 'node:test';
import assert from 'node:assert/strict';
import {Queue} from '../miniapp/player.mjs';
import {planDj,djCue,playbackTrack} from '../miniapp/dj.mjs';
import {playbackSettings} from '../miniapp/playback.mjs';
const song=(id,bpm)=>({id,duration:240,dj:{cues:[{start:20,end:130,bpm,fade:1.8},{start:20,end:100,bpm,fade:2}]}});

test('DJ neighbours share a reachable tempo and every rate stays within 3%',()=>{
  const tracks=new Map([song('a',123),song('b',126),song('c',130),song('d',155)].map(t=>[t.id,t]));
  const entries=['a','b','c','d'].map(trackId=>({id:trackId,trackId}));
  const plan=planDj(entries,tracks,'normal');
  assert.equal(plan[0].dj.end,130);assert.equal(plan[3].dj.rate,1);
  for(const e of plan)assert(e.dj.rate>=.97&&e.dj.rate<=1.03);
  assert(Math.abs(plan[0].dj.bpm*plan[0].dj.rate-plan[2].dj.bpm*plan[2].dj.rate)<1e-8);
  assert.deepEqual(entries.map(e=>e.dj),[undefined,undefined,undefined,undefined]);
});
test('manual excerpts, full-song override and unknown songs retain original bounds',()=>{
  const t=song('a',128),entry={id:'entry',trackId:'a',dj:djCue(t)};
  assert.equal(playbackTrack(t,entry,true).rangeEnd,130);
  assert.equal(playbackTrack(t,{...entry,djFull:true},true),t);
  assert.equal(playbackTrack(t,entry,false),t);
  const manual={...t,rangeStart:35,rangeEnd:90};assert.equal(djCue(manual),null);assert.equal(playbackTrack(manual,entry,true),manual);
  assert.equal(djCue({duration:200}),null);
  assert.equal(djCue({...t,dj:{cues:[{start:20,end:900,bpm:128,fade:1}]}}),null);
  assert.equal(djCue(t,'short').end,100);
});
test('only shuffle may choose compatible neighbours and queue identity survives restore',()=>{
  const tracks=new Map([song('a',128),song('b',165),song('c',126)].map(t=>[t.id,t]));
  const q=new Queue();q.start(['a','b','c']);q.playlistId=7;
  assert.deepEqual(planDj(q.entries,tracks,'normal').map(e=>e.trackId),['a','b','c']);
  q.entries=planDj(q.entries,tracks,'normal',{reorder:true});assert.deepEqual(q.entries.map(e=>e.trackId),['a','c','b']);
  q.current.djFull=true;const restore=new Queue(JSON.parse(JSON.stringify(q.snapshot())));
  assert.equal(restore.playlistId,7);assert.equal(restore.current.djFull,true);restore.remove(restore.entries[1].id);assert.equal(restore.entries.length,2);
  assert.deepEqual(playbackSettings({mode:'dj',length:'long'}),{mode:'dj',length:'long',seconds:3});
});

test('inserting an upcoming song keeps the current tempo fixed',()=>{
  const tracks=new Map([song('a',123),song('b',126),song('c',128)].map(t=>[t.id,t]));
  const first=planDj([{id:'a',trackId:'a'},{id:'b',trackId:'b'}],tracks,'normal');
  const next=planDj([...first,{id:'c',trackId:'c'}],tracks,'normal',{pin:first[0]});
  assert.equal(next[0].dj.rate,first[0].dj.rate);
  for(const e of next)assert(Math.abs(e.dj.bpm*e.dj.rate-first[0].dj.bpm*first[0].dj.rate)<1e-8);
});
