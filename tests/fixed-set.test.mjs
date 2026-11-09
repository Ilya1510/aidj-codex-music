import test from 'node:test';
import assert from 'node:assert/strict';
import {fixedCue,fixedSetEntries,playbackTrack} from '../miniapp/dj.mjs';
import {Queue} from '../miniapp/player.mjs';
const tracks=new Map([['a',{id:'a',duration:240,rangeStart:20,rangeEnd:200}],['b',{id:'b',duration:180}]]);
const cue={start:40,end:100,fadeIn:.2,fadeOut:.4,transitionSeconds:0,transitionKind:'clean-cut',rate:1};
const set={id:'dj1',name:'DJ 1',revision:'v1',playlistId:1,tracks:[{trackId:'b',...cue},{trackId:'a',...cue}]};
test('fixed set keeps published order and explicit bounds through queue persistence without dynamic cues',()=>{
  const entries=fixedSetEntries(set,tracks),q=new Queue({playlistId:1,fixedSet:{id:set.id,name:set.name,revision:set.revision},entries,currentId:entries[0].id});
  assert.deepEqual(entries.map(e=>e.trackId),['b','a']);
  const restored=new Queue(JSON.parse(JSON.stringify(q.snapshot())));
  assert.deepEqual(restored.fixedSet,{id:'dj1',name:'DJ 1',revision:'v1'});
  assert.deepEqual(restored.entries,q.entries);
  assert.equal(playbackTrack(tracks.get('a'),entries[1],true).rangeStart,40);
  assert.equal(playbackTrack(tracks.get('a'),entries[1],true).rangeEnd,100);
  assert.equal(playbackTrack(tracks.get('a'),entries[1],false).rangeStart,20);
  restored.start(['a']);assert.equal(restored.fixedSet,null);
});
test('fixed plans fail validation instead of substituting full songs or ignoring manual limits',()=>{
  for(const patch of [{start:10},{end:201},{rate:2},{fadeIn:9},{transitionSeconds:1},{end:NaN},{fadeOut:-1},{transitionKind:'unknown'}])assert.equal(fixedCue(tracks.get('a'),{fixed:{...cue,...patch}}),null);
  assert.throws(()=>fixedSetEntries({...set,tracks:[{trackId:'missing',...cue}]},tracks));
  assert.throws(()=>fixedSetEntries({...set,tracks:[]},tracks));
  assert.equal(fixedCue(tracks.get('a'),{fixed:{...cue,transitionSeconds:1,transitionKind:'crossfade'}}).transitionSeconds,1);
});
