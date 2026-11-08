import test from 'node:test';
import assert from 'node:assert/strict';
import {playbackSettings,fadeGain} from '../miniapp/playback.mjs';

test('ordinary playback is the default and always keeps original gain',()=>{
  assert.deepEqual(playbackSettings(null),{audition:false,mode:'normal',length:'normal',seconds:3,remixDurationSeconds:12,remixMaxVisits:3,remixLookahead:10});
  for(const position of [0,1,59,60])assert.equal(fadeGain(position,0,60,0),1);
});
test('smooth mode fades in and out inside the selected fragment',()=>{
  const values=[30,31,33,50,57,59,60].map(position=>fadeGain(position,30,60,3));
  assert.deepEqual(values,[0,1/3,1,1,1,1/3,0]);
});
test('short fragments keep a valid envelope and every seek recomputes gain',()=>{
  assert.equal(fadeGain(10.5,10,11,8),1);assert.equal(fadeGain(10.75,10,11,8),.5);
  for(const position of [-5,0,5,10,30,100]){const gain=fadeGain(position,10,30,3);assert(gain>=0&&gain<=1);}
  assert.equal(fadeGain(25,10,30,3),1);assert.equal(fadeGain(29,10,30,3),1/3);
});
test('stored settings are validated and duration remains bounded',()=>{
  assert.deepEqual(playbackSettings({mode:'smooth',seconds:30}),{audition:false,mode:'smooth',length:'normal',seconds:8,remixDurationSeconds:12,remixMaxVisits:3,remixLookahead:10});
  assert.deepEqual(playbackSettings({mode:'undefined',seconds:null}),{audition:false,mode:'normal',length:'normal',seconds:3,remixDurationSeconds:12,remixMaxVisits:3,remixLookahead:10});
  assert.equal(playbackSettings({mode:'smooth',seconds:.1}).seconds,1);
});

test('remix duration is bounded, finite and independent of smooth transition',()=>{
  for(const [value,expected] of [[10,10],[30,30],[60,60],[120,120],[1,10],[999,120],[NaN,12],[Infinity,12],['30',12],[null,12]]){
    const result=playbackSettings({mode:'remix',remixDurationSeconds:value,seconds:4});
    assert.equal(result.remixDurationSeconds,expected);assert.equal(result.seconds,4);
  }
});


test('remix visits and lookahead are independently bounded and survive serialization',()=>{
 for(const [field,max,defaultValue]of [['remixMaxVisits',10,3],['remixLookahead',50,10]]){
  for(const [value,expected]of [[0,1],[1,1],[4.6,5],[999,max],[NaN,defaultValue],[Infinity,defaultValue],['4',defaultValue]]){
   const p=playbackSettings({mode:'remix',[field]:value,remixDurationSeconds:60});assert.equal(p[field],expected);assert.equal(p.remixDurationSeconds,60);
   assert.deepEqual(playbackSettings(JSON.parse(JSON.stringify(p))),p);
  }
 }
});
