import test from 'node:test';
import assert from 'node:assert/strict';
import {Queue} from '../miniapp/player.mjs';
import {planDj,djCue,playbackTrack,djProgress,modeName,remixCues,planRemix,packRemixEntries,remixAnchors,remixAcousticAnchors,remixDurationSeconds} from '../miniapp/dj.mjs';
import {playbackSettings} from '../miniapp/playback.mjs';
const song=(id,bpm)=>({id,duration:240,dj:{cues:[{start:0,end:110,bpm,fade:1.8},{start:0,end:80,bpm,fade:2}]}});

test('DJ neighbours share a reachable tempo and every rate stays within 3%',()=>{
  const tracks=new Map([song('a',123),song('b',126),song('c',130),song('d',155)].map(t=>[t.id,t]));
  const entries=['a','b','c','d'].map(trackId=>({id:trackId,trackId}));
  const plan=planDj(entries,tracks,'normal');
  assert.equal(plan[0].dj.end,110);assert.equal(plan[3].dj.rate,1);
  for(const e of plan)assert(e.dj.rate>=.97&&e.dj.rate<=1.03);
  assert(Math.abs(plan[0].dj.bpm*plan[0].dj.rate-plan[2].dj.bpm*plan[2].dj.rate)<1e-8);
  assert.deepEqual(entries.map(e=>e.dj),[undefined,undefined,undefined,undefined]);
});
test('manual excerpts, full-song override and unknown songs retain original bounds',()=>{
  const t=song('a',128),entry={id:'entry',trackId:'a',dj:djCue(t)};
  assert.equal(playbackTrack(t,entry,true).rangeEnd,110);
  assert.equal(playbackTrack(t,{...entry,djFull:true},true),t);
  assert.equal(playbackTrack(t,entry,false),t);
  const manual={...t,rangeStart:35,rangeEnd:90};assert.equal(djCue(manual),null);assert.equal(playbackTrack(manual,entry,true),manual);
  assert.equal(djCue({duration:200}),null);
  assert.equal(djCue({...t,dj:{cues:[{start:20,end:900,bpm:128,fade:1}]}}),null);
  assert.equal(djCue(t,'short').end,80);
});
test('only shuffle may choose compatible neighbours and queue identity survives restore',()=>{
  const tracks=new Map([song('a',128),song('b',165),song('c',126)].map(t=>[t.id,t]));
  const q=new Queue();q.start(['a','b','c']);q.playlistId=7;
  assert.deepEqual(planDj(q.entries,tracks,'normal').map(e=>e.trackId),['a','b','c']);
  q.entries=planDj(q.entries,tracks,'normal',{reorder:true});assert.deepEqual(q.entries.map(e=>e.trackId),['a','c','b']);
  q.current.djFull=true;const restore=new Queue(JSON.parse(JSON.stringify(q.snapshot())));
  assert.equal(restore.playlistId,7);assert.equal(restore.current.djFull,true);restore.remove(restore.entries[1].id);assert.equal(restore.entries.length,2);
  assert.deepEqual(playbackSettings({mode:'dj',length:'long'}),{audition:false,mode:'dj',length:'long',seconds:3,remixDurationSeconds:12});
});

test('inserting an upcoming song keeps the current tempo fixed',()=>{
  const tracks=new Map([song('a',123),song('b',126),song('c',128)].map(t=>[t.id,t]));
  const first=planDj([{id:'a',trackId:'a'},{id:'b',trackId:'b'}],tracks,'normal');
  const next=planDj([...first,{id:'c',trackId:'c'}],tracks,'normal',{pin:first[0]});
  assert.equal(next[0].dj.rate,first[0].dj.rate);
  for(const e of next)assert(Math.abs(e.dj.bpm*e.dj.rate-first[0].dj.bpm*first[0].dj.rate)<1e-8);
});

test('automatic entries preserve initial words and unknown tempo still gets a safe fragment',()=>{
  const track={id:'a',duration:240,djV2:{cues:[{start:17.3,end:136.5,bpm:0,fade:.5,mix:3.2}]},djStatus:'complete'};
  const cue=djCue(track);assert.equal(cue.start,0);assert.equal(cue.bpm,0);
  const q=planDj([{id:'a',trackId:'a'}],new Map([['a',track]]),'normal');assert.equal(q[0].dj.rate,1);
  assert.equal(playbackTrack(track,{dj:{...cue,start:17.3}},true).rangeStart,0);
  assert.deepEqual(djProgress(['a','b'],new Map([['a',track],['b',{djStatus:'working'}]])),{ready:1,processed:1,pending:0,working:1,failed:0,total:2});
});


test('verified interior entries play the complete selected section and old unsafe starts remain zero',()=>{
  const cue={start:52.4,end:161.7,bpm:128,fade:1.3,mix:3.2,entryVerified:true};
  const track={id:'a',duration:240,djV3:{cues:[cue]},djV2:{cues:[{start:0,end:80,bpm:128,fade:2}]}};
  assert.equal(djCue(track).start,52.4);
  assert.equal(playbackTrack(track,{dj:djCue(track)},true).rangeStart,52.4);
  assert.equal(playbackTrack(track,{dj:{...cue,entryVerified:false}},true).rangeStart,0);
  const manual={...track,rangeStart:30,rangeEnd:60};
  assert.equal(playbackTrack(manual,{dj:cue},true),manual);
});

test('regular DJ length compares excerpt duration rather than absolute end',()=>{
  const track={duration:300,djV3:{cues:[{start:90,end:200,bpm:120,fade:1,entryVerified:true},{start:0,end:100,bpm:120,fade:1}]}};
  assert.equal(djCue(track).end-djCue(track).start,110);
});

test('remix is a distinct persisted mode and audition does not coerce arbitrary values',()=>{
  assert.equal(modeName({mode:'remix'}),'DJ: ремикс');
  assert.equal(playbackSettings({mode:'remix'}).mode,'remix');
  assert.equal(playbackSettings({mode:'dj',audition:true}).audition,true);
  for(const audition of ['true',1,null])assert.equal(playbackSettings({audition}).audition,false);
});


test('remix snippets require bounded verified entries and leave manual excerpts untouched',()=>{
  const good={start:42,end:54,bpm:128,fade:1,entryVerified:true};
  const track={id:'a',duration:240,djV3:{remix:[good,{...good,entryVerified:false},{...good,end:45},{...good,end:100},{...good,start:235,end:250},{...good,bpm:NaN}]}};
  assert.deepEqual(remixCues(track),[{...good,rate:1}]);
  assert.deepEqual(remixCues({...track,rangeStart:10,rangeEnd:30}),[]);
  assert.deepEqual(remixCues({duration:240}),[]);
});

test('catalog refresh retains native remix deck positions, rates, visits and queue order',()=>{
  const cues=[{start:40,end:52,bpm:128,fade:1,entryVerified:true},{start:90,end:104,bpm:128,fade:1,entryVerified:true}];
  const tracks=new Map(['a','b','c'].map(id=>[id,{id,duration:240,djV3:{remix:cues}}]));
  const entries=['a','c','b'].map(id=>({id,trackId:id}));
  const pin={...entries[0],dj:{...cues[1],rate:1.02},remix:true,remixVisits:1};
  const nextPin={...entries[1],dj:{...cues[0],rate:.99},remix:true,remixVisits:2};
  const planned=planRemix(entries,tracks,{pin,nextPin});
  assert.deepEqual(planned.map(e=>e.trackId),['a','c','b']);
  assert.deepEqual(planned[0].dj,pin.dj);assert.equal(planned[0].remixVisits,1);
  assert.deepEqual(planned[1].dj,nextPin.dj);assert.equal(planned[1].remixVisits,2);
  assert.equal(planned[2].remixCues.length,2);assert.equal(entries[2].dj,undefined);
  assert.equal(playbackTrack(tracks.get('a'),planned[0],true).rangeStart,90);
});

test('DJ shuffle chooses compatible neighbours inside the next ten rather than searching the whole playlist',()=>{
  const sequence=[song('first',128),...Array.from({length:10},(_,i)=>song('near'+i,160+i)),song('far',128)];
  const tracks=new Map(sequence.map(track=>[track.id,track]));
  const entries=sequence.map(track=>({id:track.id,trackId:track.id}));
  const planned=planDj(entries,tracks,'normal',{reorder:true});
  assert.notEqual(planned[1].trackId,'far');
  assert.deepEqual(new Set(planned.map(entry=>entry.id)),new Set(entries.map(entry=>entry.id)));
});

test('songs never requested for preparation are not counted as pending work',()=>{
  const tracks=new Map([['a',{duration:200}],['b',{duration:200,djStatus:'pending'}]]);
  assert.deepEqual(djProgress(['a','b'],tracks),{ready:0,processed:0,pending:1,working:0,failed:0,total:2});
});

test('remix revisits cannot replay a used excerpt after a catalogue reorders its choices',()=>{
  const a={start:40,end:52,bpm:128,fade:1,mix:3.2,entryVerified:true},b={...a,start:70,end:82};
  const track={id:'a',duration:240,djV3:{remix:[a,b]}};
  const entry={id:'entry',trackId:'a',remixVisits:1,remixUsed:['40000:52000']};
  for(const clips of [[a,b],[b,a]]){
    track.djV3.remix=clips;const [next]=planRemix([entry],new Map([['a',track]]));
    assert.equal(next.dj.start,70);
  }
  const [exhausted]=planRemix([{...entry,remixVisits:3}],new Map([['a',track]]));
  assert.equal(exhausted.dj,null);
});


test('v4 word guards permit a short final fade while keeping a long incoming overlap',()=>{
  const regular={start:35.52,end:125.56,bpm:110,fade:.42,mix:3.2,entryVerified:true,wordGuarded:true};
  const short={...regular,end:50.3,fade:.16};
  const track={duration:240,djV4:{cues:[regular],remix:[short]},djV3:{cues:[],remix:[]}};
  assert.equal(djCue(track).fade,.42);assert.equal(remixCues(track)[0].fade,.16);
  for(const change of [{wordGuarded:false},{entryVerified:false},{mix:.16}]){
    assert.equal(djCue({...track,djV4:{cues:[{...regular,...change}]}}),null);
    assert.deepEqual(remixCues({...track,djV4:{remix:[{...short,...change}]}}),[]);
  }
});

test('all32 remix choices fit the persistent native queue for543songs',()=>{
  const cues=Array.from({length:32},(_,k)=>({start:6*k,end:6*k+12,bpm:128,fade:.16,mix:3.2,grid:.12,energy:.8,entryVerified:true,wordGuarded:true}));
  const track={duration:240,djV4:{remix:cues}};assert.equal(remixCues(track).length,32);
  const entries=Array.from({length:543},(_,i)=>({id:String(i),trackId:'a'.repeat(24),remixCues:cues}));
  const packed=packRemixEntries(entries);assert(JSON.stringify(packed).length<1.5*1024**2);
  assert.equal(packed[0].remixPool.length,32);assert.equal(packed[0].remixCues,undefined);
  assert.deepEqual(packed[0].remixPool[0],[0,12,128,.16,3.2,.12,.8,1,1]);
  assert.deepEqual(packRemixEntries(packed),packed);assert.equal(entries[0].remixCues.length,32);
});


test('desired duration uses whole-phrase anchors including late song sections',()=>{
  const anchors={version:1,wordGuarded:true,bpm:128,grid:.12,entries:[[0,.2],[80,.8],[180,1],[280,.9]],exits:[[12,.16,.2],[124,.3,.9],[220,.2,1],[284,.16,.9],[400,.3,.9]]};
  const track={id:'a',duration:420,djV4:{anchors}};
  const short=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:12})[0];assert.equal(short.dj.end-short.dj.start,12);
  const medium=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:43})[0];assert(Math.abs(medium.dj.end-medium.dj.start-43)<=3);assert(anchors.entries.some(([n])=>n===medium.dj.start));assert(anchors.exits.some(([n])=>n===medium.dj.end));
  const long=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:120})[0];assert.equal(long.dj.end-long.dj.start,120);assert(long.dj.start>=280);assert.equal(long.dj.mix,3.2);assert.equal(long.dj.wordGuarded,true);
  assert.deepEqual(long.remixCues,[]);assert.equal(packRemixEntries([long])[0].remixAnchors.packed,true);assert.equal(packRemixEntries([long])[0].remixAnchors.entries[2],80000);
  assert.equal(remixDurationSeconds(),12);assert.equal(remixDurationSeconds(NaN),12);assert.equal(remixDurationSeconds(4),10);assert.equal(remixDurationSeconds(300),120);
});
test('duration settings keep current and prepared next decks and reject unguarded anchors',()=>{
  const anchors={version:1,wordGuarded:true,bpm:120,entries:[[0,.2],[30,.8],[100,.8]],exits:[[12,.3,.8],[90,.3,.8],[160,.3,.8]]},track={id:'a',duration:240,djV4:{anchors}};
  const entries=[{id:'e1',trackId:'a'},{id:'e2',trackId:'a'},{id:'e3',trackId:'a'}],original=planRemix(entries,new Map([['a',track]])),[pin,nextPin]=original;
  const changed=planRemix(original,new Map([['a',track]]),{pin,nextPin,remixDurationSeconds:60});assert.deepEqual(changed[0].dj,pin.dj);assert.deepEqual(changed[1].dj,nextPin.dj);assert.equal(changed[2].dj.end-changed[2].dj.start,60);
  assert.equal(remixAnchors({...track,djV4:{anchors:{...anchors,wordGuarded:false}}}),null);
  const legacy={...track,djV4:{remix:[{start:40,end:52,bpm:120,fade:1,entryVerified:true}]}};assert.deepEqual(remixCues(legacy,120),[]);
});
test('full-song anchor queues stay below the8MB private transport limit without duplicate pools',()=>{
  const anchors={version:1,wordGuarded:true,bpm:128.1234,grid:.123456,entries:Array.from({length:256},(_,i)=>[Number((i*2.2+.1234).toFixed(4)),.9876]),exits:Array.from({length:256},(_,i)=>[Number((i*2.2+10.1234).toFixed(4)),1.9876,.9876])};
  const entries=Array.from({length:543},(_,i)=>({id:'e'+i,trackId:'a'.repeat(24),remixAnchors:anchors,remixAcousticAnchors:{...anchors,quality:'acoustic',wordGuarded:false,needsReview:true},remixCues:Array.from({length:32},()=>({start:0,end:12,bpm:128,fade:.2,mix:3.2,wordGuarded:true,entryVerified:true}))}));
  const packed=packRemixEntries(entries);assert(JSON.stringify(packed).length<8*1024**2);assert.equal(packed[0].remixPool,undefined);assert.equal(packed[0].remixAnchors.packed,true);assert.equal(packed[0].remixAnchors.flat,true);assert.equal(packed[0].remixAcousticAnchors.packed,true);assert.deepEqual(packRemixEntries(packed),packed);
});


test('acoustic fallback is audible at its measured entry but never claims recognised words',()=>{
  const a={version:1,quality:'acoustic',wordGuarded:false,needsReview:true,bpm:128,entries:[[40,1]],exits:[[100,.7,1]]},track={id:'a',duration:240,djV4:{acousticAnchors:a}};
  const [entry]=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:60});
  assert.equal(entry.dj.start,40);assert.equal(entry.dj.end,100);assert.equal(entry.dj.acoustic,true);assert.equal(entry.dj.needsReview,true);assert.equal(entry.dj.entryVerified,false);assert.equal(entry.dj.wordGuarded,false);assert.equal(entry.remixNeedsReview,true);assert.equal(playbackTrack(track,entry,true).rangeStart,40);
  const graph={...a,wordGuarded:true,entries:[[30,.2]],exits:[[90,.3,.2]]};const [verified]=planRemix([{id:'e',trackId:'a'}],new Map([['a',{...track,djV4:{anchors:graph,acousticAnchors:a}}]]),{remixDurationSeconds:60});assert.equal(verified.dj.entryVerified,true);assert.equal(verified.dj.acoustic,undefined);
  assert.equal(remixAcousticAnchors({...track,djV4:{acousticAnchors:{...a,needsReview:false}}}),null);
});
test('missing requested duration retains a clearly marked full-source review fallback',()=>{
  const track={id:'a',duration:240,djV4:{remix:[{start:40,end:52,bpm:128,fade:1,entryVerified:true}]}};
  const [entry]=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:120});assert.equal(entry.dj,null);assert.equal(entry.remix,false);assert.equal(entry.remixNeedsReview,true);assert.equal(entry.remixFallback,'full-source');assert.equal(playbackTrack(track,entry,true),track);
  const [manual]=planRemix([{id:'e',trackId:'a'}],new Map([['a',{...track,rangeStart:40,rangeEnd:100}]]),{remixDurationSeconds:120});assert.equal(manual.remixFallback,null);assert.equal(manual.remixNeedsReview,false);
});
