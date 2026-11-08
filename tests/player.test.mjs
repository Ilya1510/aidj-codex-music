import test from 'node:test';
import assert from 'node:assert/strict';
import {Queue} from '../miniapp/player.mjs';
import {Downloads} from '../miniapp/storage.mjs';

test('shuffle preserves current track and includes each upcoming song once',()=>{
  const q=new Queue();q.start(['a','b','c','d']);const current=q.currentId;
  q.toggleShuffle(()=>0);
  assert.equal(q.currentId,current);assert.deepEqual(new Set(q.entries.map(x=>x.trackId)),new Set(['a','b','c','d']));
  assert.notDeepEqual(q.entries.map(x=>x.trackId),['a','b','c','d']);
  q.toggleShuffle();assert.deepEqual(q.entries.map(x=>x.trackId),['a','b','c','d']);
});
test('queue removal changes only the queue and next entries can be reordered',()=>{
  const playlist=['a','b','c'];const q=new Queue();q.start(playlist);
  q.remove(q.entries[1].id);assert.deepEqual(playlist,['a','b','c']);
  assert.deepEqual(q.entries.map(x=>x.trackId),['a','c']);
  q.append('b',true);assert.deepEqual(q.entries.map(x=>x.trackId),['a','b','c']);
  q.move(q.entries[2].id,-1);assert.deepEqual(q.entries.map(x=>x.trackId),['a','c','b']);
  q.remove(q.currentId);assert.equal(q.current.trackId,'c');
});
test('repeat modes, queue persistence, and clearing upcoming songs',()=>{
  const q=new Queue();q.start(['a','b']);q.repeat='one';assert.equal(q.next(true).trackId,'a');
  assert.equal(q.next(false).trackId,'b');q.repeat='all';assert.equal(q.next(true).trackId,'a');
  const restored=new Queue(JSON.parse(JSON.stringify(q.snapshot())));assert.equal(restored.current.trackId,'a');
  restored.clearUpcoming();assert.equal(restored.entries.length,1);restored.repeat='off';assert.equal(restored.next(),null);
});
test('downloads deduplicate existing and in-flight files across playlists',async()=>{
  const saved=new Set(['already']);let requests=0;
  const d=new Downloads({existing:md5=>saved.has(md5),fetchTrack:async t=>{requests++;return {ok:true,blob:async()=>new Blob(['hello'])};},store:async(t,b)=>saved.add(t.md5)});
  await d.one({md5:'already',bytes:5});assert.equal(requests,0);
  const track={md5:'new',bytes:5};await Promise.all([d.one(track),d.one(track)]);
  assert.equal(requests,1);await d.one(track);assert.equal(requests,1);
});
test('partial files never enter offline storage and completed downloads are skipped on resume',async()=>{
  const saved=new Set();let tries=0;
  const d=new Downloads({existing:m=>saved.has(m),fetchTrack:async t=>({ok:true,blob:async()=>new Blob([t.md5==='bad'&&tries++===0?'x':'hello'])}),store:async(t,b)=>saved.add(t.md5)});
  d.add([{md5:'good',bytes:5},{md5:'bad',bytes:5}]);
  await new Promise(resolve=>setTimeout(resolve,20));
  assert(saved.has('good'));assert(!saved.has('bad'));assert(d.paused);
  d.toggle();await new Promise(resolve=>setTimeout(resolve,20));assert(saved.has('bad'));assert.equal(d.completed,2);
});

test('upcoming drag moves duplicate occurrences by entry ID, preserves current and pins the next song',()=>{
  const q=new Queue();q.start(['played','current','same','other','same'],1);
  const [played,current,a,b,c]=q.entries;
  assert(q.moveUpcoming(c.id,a.id));assert.deepEqual(q.entries.map(e=>e.id),[played.id,current.id,c.id,a.id,b.id]);
  assert.equal(q.currentId,current.id);assert.equal(q.manualNextId,c.id);
  const restored=new Queue(JSON.parse(JSON.stringify(q.snapshot())));assert.equal(restored.manualNextId,c.id);
  assert.equal(q.moveUpcoming(current.id,null),false);assert.equal(q.moveUpcoming(played.id,null),false);
  assert.equal(q.moveUpcoming(a.id,played.id),false);assert.equal(q.moveUpcoming(a.id,'deleted-entry'),false);
  assert(q.moveUpcoming(c.id,null));assert.deepEqual(q.entries.map(e=>e.id),[played.id,current.id,a.id,b.id,c.id]);
  assert.equal(q.manualNextId,a.id);q.next();assert.equal(q.currentId,a.id);assert.equal(q.manualNextId,null);
  q.clearUpcoming();assert.equal(q.currentId,a.id);
});
