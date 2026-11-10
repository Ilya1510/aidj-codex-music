import test from 'node:test';
import assert from 'node:assert/strict';
import {FixedBlend,fixedBlendGains} from '../miniapp/fixed-blend.mjs';
const source={md5:'a',duration:20},second={md5:'b',duration:20};
const cue={start:1,end:10,fadeIn:3,fadeOut:.1,transitionSeconds:3,transitionKind:'crossfade',rate:1};
const left={id:'a',fixed:{...cue}},right={id:'b',fixed:{...cue}};
class Deck extends EventTarget{
  constructor(){super();this.paused=true;this.volume=1;this.currentTime=1;this.duration=20;this.playbackRate=1;this.seeking=false;this.plays=0;}
  play(){this.paused=false;this.plays++;return Promise.resolve();}
  pause(){this.paused=true;}
  load(){}
  removeAttribute(){this.src='';}
}
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function setup(resolve=async()=>({src:'qa'}),options={}){
 const audio=new Deck();audio.paused=false;audio.currentTime=5;
 const state={enabled:true,audio,track:source,entry:left,nextTrack:second,next:right};
 const decks=[],promotions=[];const blend=new FixedBlend({snapshot:()=>state,resolve,create:()=>{const deck=new Deck();decks.push(deck);return deck;},promote:(...args)=>promotions.push(args),revoke:()=>{},...options});
 return {audio,state,decks,promotions,blend};
}
test('first second is quiet; outgoing keeps authored tail; promotion envelope is continuous',()=>{
 const start=fixedBlendGains(source,left,7,20,second,right,1,20,0);
 assert.equal(start.incoming,0);assert.equal(start.out,.7);
 const one=fixedBlendGains(source,left,8,20,second,right,2,20,1/3);
 assert.ok(one.incoming<.12);assert.ok(one.out>.6);
 const tail=fixedBlendGains(source,left,9.99,20,second,right,3.99,20,.99);
 assert.ok(tail.out<.012);
 const end=fixedBlendGains(source,left,10,20,second,right,4,20,1);
 assert.equal(end.out,0);assert.ok(Math.abs(end.incoming-.7)<1e-12);
});
test('preload stays silent, overlaps once, and promotes actual progressed deck without reload',async()=>{
 const {audio,blend,decks,promotions}=setup();blend.tick();await turn();const deck=decks[0];
 assert.equal(deck.volume,0);assert.equal(deck.plays,0);
 deck.dispatchEvent(new Event('loadedmetadata'));deck.dispatchEvent(new Event('seeked'));
 audio.currentTime=7;blend.tick();await turn();assert.equal(deck.plays,1);
 audio.currentTime=8;deck.currentTime=2;blend.tick();assert.ok(deck.volume>0&&deck.volume<.12);
 audio.currentTime=10;deck.currentTime=4;blend.apply();assert.equal(blend.promote(),true);
 assert.equal(promotions.length,1);assert.equal(promotions[0][0],deck);assert.equal(deck.currentTime,4);assert.equal(deck.plays,1);
 assert.equal(blend.promote(),false);
});
test('pause mutes both-deck controller immediately; idle ticks never start it; resume preserves position',async()=>{
 const {audio,blend,decks}=setup();blend.tick();await turn();const deck=decks[0];deck.dispatchEvent(new Event('loadedmetadata'));deck.dispatchEvent(new Event('seeked'));
 audio.currentTime=7;blend.tick();await turn();deck.currentTime=2;
 audio.pause();blend.pause();assert.equal(deck.paused,true);assert.equal(deck.volume,0);
 for(let i=0;i<10;i++)blend.tick();assert.equal(deck.plays,1);
 audio.paused=false;blend.tick();await turn();assert.equal(deck.plays,2);assert.equal(deck.currentTime,2);
});
test('stale source/range/preload completion cannot start another song or leak URL',async()=>{
 let finish;const {audio,state,blend,decks}=setup(()=>new Promise(r=>finish=r));const revoked=[];blend.revoke=url=>revoked.push(url);
 blend.tick();state.next={...right,fixed:{...cue,start:2}};blend.cancel();finish({src:'blob:x',url:'blob:x'});await turn();
 assert.equal(decks[0].plays,0);assert.equal(decks[0].volume,0);assert.deepEqual(revoked,['blob:x']);
 state.next=right;blend.tick();state.nextTrack={...second,md5:'changed'};blend.tick();assert.equal(decks[1].paused,true);
 audio.paused=true;blend.tick();assert.equal(decks.at(-1).plays,0);
});
test('full file outgoing uses actual decoded end before overlap, never the shorter metadata',async()=>{
 const {audio,state,blend,decks}=setup();state.track={...source,duration:10};state.entry={...left,fixed:{...cue,end:10}};audio.duration=10.1;
 blend.tick();await turn();decks[0].dispatchEvent(new Event('loadedmetadata'));decks[0].dispatchEvent(new Event('seeked'));
 audio.currentTime=7.05;blend.tick();assert.equal(decks[0].plays,0);
 audio.currentTime=7.11;blend.tick();await turn();assert.equal(decks[0].plays,1);
});
test('late play promise belongs to promoted main deck and cannot silence it',async()=>{
 const {audio,blend,decks,promotions}=setup();blend.tick();await turn();const deck=decks[0];
 deck.dispatchEvent(new Event('loadedmetadata'));deck.dispatchEvent(new Event('seeked'));
 let finish;deck.play=()=>{deck.paused=false;return new Promise(resolve=>finish=resolve);};
 audio.currentTime=7;blend.tick();audio.currentTime=10;deck.currentTime=4;
 assert.equal(blend.promote(),true);assert.equal(promotions.length,1);
 deck.volume=.7;finish();await turn();assert.equal(deck.paused,false);assert.equal(deck.volume,.7);
});
test('near-deadline timer starts the prepared deck without waiting for another50ms UI poll',async()=>{
 const timers=[];const cancelled=[];
 const {audio,blend,decks}=setup(undefined,{schedule:(fn,delay)=>{timers.push({fn,delay});return timers.length;},unschedule:id=>cancelled.push(id)});
 blend.tick();await turn();const deck=decks[0];deck.dispatchEvent(new Event('loadedmetadata'));deck.dispatchEvent(new Event('seeked'));
 audio.playbackRate=1.03;audio.currentTime=6.88;blend.tick();
 assert.equal(deck.plays,0);assert.equal(timers.length,1);assert.equal(timers[0].delay,32);
 audio.currentTime=6.912;timers[0].fn();await turn();
 assert.equal(deck.plays,1);assert.equal(deck.currentTime,1);assert.equal(deck.preservesPitch,true);
 assert.ok(blend.state.mixing);blend.cancel();
});
test('queued start timer cannot override pause or start from a stale source; stalled clocks do not busy retry',async()=>{
 const timers=[],cancelled=[];
 const {audio,state,blend,decks}=setup(undefined,{schedule:(fn,delay)=>{timers.push({fn,delay});return timers.length;},unschedule:id=>cancelled.push(id)});
 blend.tick();await turn();const deck=decks[0];deck.dispatchEvent(new Event('loadedmetadata'));deck.dispatchEvent(new Event('seeked'));
 audio.currentTime=6.97;blend.tick();assert.equal(timers.length,1);
 timers[0].fn();assert.equal(timers.length,1);assert.equal(deck.plays,0);
 blend.tick();assert.equal(timers.length,2);audio.pause();blend.pause();assert.ok(cancelled.includes(2));
 audio.currentTime=7.01;timers[1].fn();await turn();assert.equal(deck.plays,0);assert.equal(deck.volume,0);
 audio.paused=false;audio.currentTime=6.97;blend.tick();assert.equal(timers.length,3);
 state.nextTrack={...second,md5:'changed'};audio.currentTime=7.02;timers[2].fn();await turn();
 assert.equal(deck.plays,0);blend.cancel();
});
