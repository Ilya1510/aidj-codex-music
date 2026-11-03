import test from 'node:test';
import assert from 'node:assert/strict';
import {segment,moveSegment,changeBoundary,parseTime,formatTime} from '../miniapp/segments.mjs';
test('dragging the selected window preserves length at both song edges',()=>{
  const part=segment({duration:200,rangeStart:30,rangeEnd:90});
  assert.deepEqual(moveSegment(part,-100),{start:0,end:60,total:200});
  assert.deepEqual(moveSegment(part,1000),{start:140,end:200,total:200});
});
test('handles cannot cross, become negative, or exceed the song',()=>{
  const part=segment({duration:200,rangeStart:30,rangeEnd:90});
  assert.equal(changeBoundary(part,'start',120).start,89.5);
  assert.equal(changeBoundary(part,'end',0).end,30.5);
  assert.equal(changeBoundary(part,'start',-10).start,0);
  assert.equal(changeBoundary(part,'end',300).end,200);
});
test('manual timestamps agree with draggable bounds and reject malformed time',()=>{
  assert.equal(parseTime('1:30'),90);assert.equal(parseTime('0:01:30.5'),90.5);
  assert.equal(formatTime(90.5),'1:30.5');assert.equal(formatTime(59.98),'1:00');
  for(const value of ['1:90','false','-1:00','NaN','0:00:60'])assert.throws(()=>parseTime(value));
});
