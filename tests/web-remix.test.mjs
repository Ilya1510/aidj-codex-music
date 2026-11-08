import test from 'node:test';
import assert from 'node:assert/strict';
import {Queue} from '../miniapp/player.mjs';
import {planRemix} from '../miniapp/dj.mjs';
import {arrangeWebRemix,finishWebRemix,forecastWebRemix} from '../miniapp/web-remix.mjs';
const preferences={remixDurationSeconds:10,remixMaxVisits:3,remixLookahead:10};
function fixture(count=8){
  const tracks=new Map(Array.from({length:count},(_,i)=>{const id=String(i+1).padStart(24,'0'),bpm=i===0||i===2?128:160;return [id,{id,duration:180,djV4:{remix:[20,60,100].map(start=>({start,end:start+10,bpm,fade:.3,mix:3.2,entryVerified:true,wordGuarded:true,energy:.5}))}}];}));
  const queue=new Queue();queue.start([...tracks.keys()]);queue.entries=planRemix(queue.entries,tracks,preferences);return {queue,tracks};
}
test('web remix honours visit cap, unique excerpts and tempo selection',()=>{
  for(const cap of [1,3]){
    const {queue,tracks}=fixture(),p={...preferences,remixMaxVisits:cap};arrangeWebRemix(queue,tracks,p);
    assert.equal(queue.entries[1].trackId,[...tracks.keys()][2]);
    const played=new Map();let n=0;
    do{const e=queue.current,heard=played.get(e.trackId)||new Set(),key=e.dj.start+':'+e.dj.end;assert.ok(!heard.has(key));heard.add(key);played.set(e.trackId,heard);assert.ok(heard.size<=cap);n++;}while(n<100&&finishWebRemix(queue,tracks,p));
    assert.equal(n,8*cap);assert.equal(queue.currentId,null);
  }
});
test('web forecast is read-only and matches actual next transition; manual choice overrides lookahead',()=>{
  const {queue,tracks}=fixture(14);arrangeWebRemix(queue,tracks,preferences);
  const before=JSON.stringify(queue.snapshot()),forecast=forecastWebRemix(queue,tracks,preferences);
  assert.equal(JSON.stringify(queue.snapshot()),before);assert.equal(forecast.segments.length,10);
  finishWebRemix(queue,tracks,preferences);assert.equal(queue.current.id,forecast.segments[0].entryId);assert.equal(queue.current.dj.start,forecast.segments[0].start);
  const current=JSON.stringify(queue.current.dj),last=queue.entries.at(-1);queue.manualNextId=last.id;
  arrangeWebRemix(queue,tracks,{...preferences,remixLookahead:1});assert.equal(queue.entries[1].id,last.id);assert.equal(JSON.stringify(queue.current.dj),current);
  const skipped=queue.current.id;finishWebRemix(queue,tracks,preferences,true);assert.ok(!queue.entries.some(e=>e.id===skipped));
});
