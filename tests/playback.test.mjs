import test from 'node:test';
import assert from 'node:assert/strict';
import {playbackSettings,fadeGain} from '../miniapp/playback.mjs';

test('ordinary playback is the default and always keeps original gain',()=>{
  assert.deepEqual(playbackSettings(null),{mode:'normal',seconds:3});
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
  assert.deepEqual(playbackSettings({mode:'smooth',seconds:30}),{mode:'smooth',seconds:8});
  assert.deepEqual(playbackSettings({mode:'undefined',seconds:null}),{mode:'normal',seconds:3});
  assert.equal(playbackSettings({mode:'smooth',seconds:.1}).seconds,1);
});
