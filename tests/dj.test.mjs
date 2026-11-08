import test from 'node:test';
import assert from 'node:assert/strict';
import {Queue} from '../miniapp/player.mjs';
import {planDj,djCue,playbackTrack,djProgress,modeName,remixCues,planRemix,packRemixEntries,remixAnchors,remixAcousticAnchors,remixDurationSeconds,timedCue} from '../miniapp/dj.mjs';
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
  assert.deepEqual(playbackSettings({mode:'dj',length:'long'}),{audition:false,mode:'dj',length:'long',seconds:3,remixDurationSeconds:12,remixMaxVisits:3,remixLookahead:10});
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
test('old APKs retain a clearly marked full-source fallback when timed clips are unsupported',()=>{
  const track={id:'a',duration:240,djV4:{remix:[{start:40,end:52,bpm:128,fade:1,entryVerified:true}]}};
  const [entry]=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:120,allowTimed:false});assert.equal(entry.dj,null);assert.equal(entry.remix,false);assert.equal(entry.remixNeedsReview,true);assert.equal(entry.remixFallback,'full-source');assert.equal(playbackTrack(track,entry,true),track);
  const [manual]=planRemix([{id:'e',trackId:'a'}],new Map([['a',{...track,rangeStart:40,rangeEnd:100}]]),{remixDurationSeconds:120,allowTimed:false});assert.equal(manual.remixFallback,'full-source');assert.equal(playbackTrack({...track,rangeStart:40,rangeEnd:100},manual,true).rangeEnd,100);
});

test('regular DJ can pair primary graph boundaries when no static cue was preselected',()=>{
  const anchors={version:1,wordGuarded:true,bpm:128,grid:.12,entries:[[0,.1],[60,.8],[180,1]],exits:[[45,.3,.1],[140,.3,.8],[290,.4,1]]};
  const track={id:'a',duration:330,djV4:{cues:[],remix:[],anchors}};
  const cue=djCue(track);assert.equal(cue.start,180);assert.equal(cue.end,290);assert.equal(cue.end-cue.start,110);assert.equal(cue.wordGuarded,true);assert.equal(cue.entryVerified,true);assert.equal(cue.mix,3.2);
  assert.equal(djCue(track,'short').end-djCue(track,'short').start,80);
  const [entry]=planDj([{id:'e',trackId:'a'}],new Map([['a',track]]),'normal');assert.equal(entry.dj.start,180);assert.equal(entry.dj.end,290);assert.equal(playbackTrack(track,entry,true).rangeStart,180);
  assert.deepEqual(djProgress(['a'],new Map([['a',track]])),{ready:1,processed:0,pending:0,working:0,failed:0,total:1});
});

test('regular graph fallback keeps acoustic provenance and obeys the safer fade bound',()=>{
  const acousticAnchors={version:1,quality:'acoustic',wordGuarded:false,needsReview:true,bpm:0,entries:[[35,1]],exits:[[145,.7,1]]};
  const track={id:'a',duration:240,djV4:{cues:[],acousticAnchors}};
  const cue=djCue(track);assert.equal(cue.start,35);assert.equal(cue.end,145);assert.equal(cue.entryVerified,false);assert.equal(cue.wordGuarded,false);assert.equal(cue.acoustic,true);assert.equal(cue.quality,'acoustic');assert.equal(cue.needsReview,true);assert.equal(cue.fade,.7);assert.equal(cue.mix,3.2);
  assert.equal(playbackTrack(track,{dj:cue},true).rangeStart,35);
  assert.equal(djCue({...track,djV4:{acousticAnchors:{...acousticAnchors,exits:[[145,.49,1]]}}}),null);
  assert.equal(djCue({...track,djV4:{acousticAnchors:{...acousticAnchors,needsReview:false}}}),null);
});

test('regular graph fallback prefers whole-word evidence and never borrows a remix-only fade',()=>{
  const anchors={version:1,wordGuarded:true,bpm:120,entries:[[40,.8]],exits:[[150,.16,1],[155,.3,.8]]};
  const acousticAnchors={version:1,quality:'acoustic',wordGuarded:false,needsReview:true,bpm:120,entries:[[30,1]],exits:[[140,.7,1]]};
  const track={duration:240,djV4:{cues:[],anchors,acousticAnchors}};
  const cue=djCue(track);assert.equal(cue.start,40);assert.equal(cue.end,155);assert.equal(cue.wordGuarded,true);assert.equal(cue.acoustic,undefined);assert.equal(cue.fade,.3);
  const onlyNarrow={...track,djV4:{anchors:{...anchors,exits:[[150,.16,1]]}}};assert.equal(djCue(onlyNarrow),null);
  assert.equal(djCue({...track,rangeStart:40,rangeEnd:100}),null);
  assert.equal(djCue({...track,rangeEnd:90}),null);
  assert.equal(playbackTrack(track,{dj:cue,djFull:true},true),track);
  assert.equal(planDj([{id:'e',trackId:'a',djFull:true}],new Map([['a',{...track,id:'a'}]]),'normal')[0].dj,null);
});

test('regular valid static cues remain unchanged and malformed graph boundaries cannot reach playback',()=>{
  const cue={start:0,end:80,bpm:128,fade:1.8};
  const anchors={version:1,wordGuarded:true,bpm:128,entries:[[30,1]],exits:[[140,.3,1]]};
  const track={duration:240,djV4:{cues:[cue],anchors}};
  assert.deepEqual(djCue(track),cue);
  for(const change of [{entries:[[NaN,1]]},{entries:[[-1,1]]},{exits:[[250,.3,1]]},{exits:[[140,Infinity,1]]},{exits:[[140,.249,1]]},{exits:[[50,.3,1]]},{bpm:NaN}])assert.equal(djCue({...track,djV4:{cues:[],anchors:{...anchors,...change}}}),null);
  assert.equal(djCue({...track,duration:NaN,djV4:{cues:[],anchors}}),null);
  // A graph containing only rejected/removed ends cannot recover that end from
  // static or another unfiltered source: it has no usable regular cue.
  assert.equal(djCue({...track,djV4:{cues:[],anchors:{...anchors,exits:[]}}}),null);
});


test('unmatched duration uses an honest short timed remix rather than a full source',()=>{
  const track={id:'a',duration:112.744475,djV4:{anchors:{version:1,wordGuarded:true,bpm:114.9425,entries:[[0,.0889],[17.373,.9645],[21.033,.9654]],exits:[[21.633,.6,.7361]]}}};
  const [entry]=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:12});
  assert.equal(entry.dj.end-entry.dj.start,12);assert([0,17.373,21.033].includes(entry.dj.start));assert(timedCue(entry.dj));assert.equal(entry.dj.sourceDuration,track.duration);assert.equal(entry.remixFallback,'timed');assert.equal(entry.remixNeedsReview,true);
  assert.equal(entry.dj.entryVerified,false);assert.equal(entry.dj.wordGuarded,false);assert.equal(entry.dj.quality,'timed');assert.equal(entry.dj.acoustic,undefined);assert.equal(entry.dj.rate,1);assert.equal(entry.dj.mix,3.2);
  assert.equal(playbackTrack(track,entry,true).rangeStart,entry.dj.start);
  const [old]=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]),{remixDurationSeconds:12,allowTimed:false});assert.equal(old.dj,null);assert.equal(old.remixFallback,'full-source');
  const [downgraded]=planRemix([entry],new Map([['a',track]]),{pin:entry,remixDurationSeconds:12,allowTimed:false});assert.equal(downgraded.dj,null);
});

test('word/acoustic matches always precede a timed fallback and short originals stay bounded',()=>{
  const word={version:1,wordGuarded:true,bpm:128,entries:[[40,1]],exits:[[52,.2,1]]},track={id:'a',duration:112,djV4:{anchors:word}};
  const [matched]=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]));assert.equal(matched.dj.wordGuarded,true);assert.equal(matched.dj.timedFallback,undefined);
  const sound={...word,wordGuarded:false,quality:'acoustic',needsReview:true,exits:[[52,.7,1]]};const [acoustic]=planRemix([{id:'e',trackId:'a'}],new Map([['a',{...track,djV4:{acousticAnchors:sound}}]]));assert.equal(acoustic.dj.quality,'acoustic');assert.equal(acoustic.dj.timedFallback,undefined);
  const short={id:'a',duration:57};const [bounded]=planRemix([{id:'e',trackId:'a'}],new Map([['a',short]]),{remixDurationSeconds:120});assert(timedCue(bounded.dj));assert.equal(bounded.dj.start,0);assert.equal(bounded.dj.end,57);
  const [manual]=planRemix([{id:'e',trackId:'a'}],new Map([['a',{...track,rangeStart:40,rangeEnd:90}]]));assert(manual.dj);assert(manual.dj.start>=40&&manual.dj.end<=90);assert.equal(manual.remix,true);
  const [full]=planRemix([{id:'e',trackId:'a',djFull:true}],new Map([['a',track]]));assert.equal(full.dj,null);assert.equal(full.remixFallback,null);
});

test('timed cue validation rejects false confidence, invalid numbers and normal DJ use',()=>{
  const track={id:'a',duration:240};const [entry]=planRemix([{id:'e',trackId:'a'}],new Map([['a',track]]));const c=entry.dj;assert(timedCue(c));
  for(const change of [{timedFallback:false},{timedFallback:'true'},{needsReview:false},{wordGuarded:true},{entryVerified:true},{quality:'acoustic'},{acoustic:true},{start:-1},{end:240},{targetSeconds:9},{targetSeconds:121},{sourceDuration:NaN},{fade:.79},{fade:1.21},{mix:3.19},{mix:8.1},{bpm:50},{bpm:Infinity},{start:'0'}])assert.equal(timedCue({...c,...change}),false);
  assert.equal(playbackTrack(track,{...entry,remix:false},true),track);assert.equal(playbackTrack({...track,duration:241},entry,true).rangeStart,undefined);
  const regular={...track,djV4:{cues:[{...c,start:0,end:110,targetSeconds:110}]}};assert.equal(djCue(regular),null);
});

test('timed revisits prefer genuinely different published starts and retain native pins',()=>{
  const track={id:'a',duration:112,djV4:{anchors:{version:1,wordGuarded:true,bpm:120,entries:[[0,.2],[20,1],[60,.9]],exits:[]}}},tracks=new Map([['a',track]]);
  const [first]=planRemix([{id:'e',trackId:'a'}],tracks);assert(timedCue(first.dj));const heard=Math.round(first.dj.start*1000)+':'+Math.round(first.dj.end*1000);
  const [second]=planRemix([{...first,remixVisits:1,remixUsed:[heard]}],tracks);assert(timedCue(second.dj));assert.notEqual(second.dj.start,first.dj.start);
  const [pin]=planRemix([first],tracks,{pin:first,remixDurationSeconds:30});assert.deepEqual(pin.dj,first.dj);
  const noChoice={id:'a',duration:112};const [only]=planRemix([{id:'e',trackId:'a'}],new Map([['a',noChoice]]));const [done]=planRemix([{...only,remixVisits:1,remixUsed:['0:12000']}],new Map([['a',noChoice]]));assert.equal(done.dj,null);
});

test('remix selects original-coordinate boundaries inside the user excerpt and retains full override',()=>{
 const anchors={version:1,wordGuarded:true,bpm:128,entries:[[0,.4],[30,.8],[80,.9],[120,1]],exits:[[12,.3,.4],[42,.3,.8],[92,.3,.9],[132,.3,1]]};
 const track={id:'manual',duration:173.244,rangeStart:25,rangeEnd:114.832,djV4:{anchors}};
 const choices=remixCues(track,12);assert.equal(choices.length,2);assert(choices.every(c=>c.start>=25&&c.end<=114.832));
 assert.equal(djCue(track),null);
 const [entry]=planRemix([{id:'e',trackId:track.id}],new Map([[track.id,track]]));assert(entry.remix);assert(entry.dj.start>=25&&entry.dj.end<=114.832);
 const playing=playbackTrack(track,entry,true);assert.equal(playing.rangeStart,entry.dj.start);assert.equal(playing.rangeEnd,entry.dj.end);
 assert.equal(playbackTrack(track,{...entry,djFull:true},true),track);
 assert.equal(playbackTrack(track,{...entry,remix:false},true),track);
 const stale={...entry,dj:{...entry.dj,start:120,end:132}};
 const [replanned]=planRemix([stale],new Map([[track.id,track]]),{pin:stale});assert(replanned.dj.start>=25&&replanned.dj.end<=114.832);
});
test('timed remix stays in a manual excerpt even when the target exceeds its length',()=>{
 const track={id:'manual',duration:173.244,rangeStart:25,rangeEnd:114.832};
 const [short]=planRemix([{id:'e',trackId:track.id}],new Map([[track.id,track]]));assert(timedCue(short.dj));assert.equal(short.dj.start,25);assert.equal(short.dj.end,37);
 const [long]=planRemix([{id:'e',trackId:track.id}],new Map([[track.id,track]]),{remixDurationSeconds:120});assert(timedCue(long.dj));assert.equal(long.dj.start,25);assert.equal(long.dj.end,114.832);assert.equal(long.dj.sourceDuration,173.244);
 for(const patch of [{sourceRangeStart:undefined},{sourceRangeEnd:undefined},{sourceRangeEnd:180},{sourceRangeStart:'25'},{sourceRangeEnd:24}])assert(!timedCue({...long.dj,...patch}));
 const changed={...track,rangeStart:30};assert.equal(playbackTrack(changed,short,true),changed);
 const tiny={...track,rangeStart:25,rangeEnd:30};const [e]=planRemix([{id:'e',trackId:track.id}],new Map([[track.id,tiny]]));assert(!e.dj);assert.equal(playbackTrack(tiny,e,true),tiny);
});

test('timed pins are recomputed when manual scope changes even if the old cue still lies inside',()=>{
 const before={id:'s',duration:180,rangeStart:25,rangeEnd:114.832};
 const [pin]=planRemix([{id:'e',trackId:'s'}],new Map([['s',before]]));
 const after={...before,rangeStart:20,rangeEnd:100};
 const [updated]=planRemix([pin],new Map([['s',after]]),{pin});
 assert(timedCue(updated.dj));assert.equal(updated.dj.sourceRangeStart,20);assert.equal(updated.dj.sourceRangeEnd,100);
 const invalid={...pin,dj:{...pin.dj,wordGuarded:true}};
 const [recovered]=planRemix([invalid],new Map([['s',before]]),{pin:invalid});assert(timedCue(recovered.dj));
});


test('per-song visit limits are independent and keep an already playing cue pinned',()=>{
 const track={id:'a',duration:240,djV4:{anchors:{version:1,wordGuarded:true,bpm:128,entries:[[0,.4],[40,.8],[80,.9],[120,1]],exits:[[12,.3,.4],[52,.3,.8],[92,.3,.9],[132,.3,1]]}}};
 const tracks=new Map([['a',track]]),entry={id:'e',trackId:'a',remixVisits:3,remixUsed:['0:12000','40000:52000','80000:92000']};
 assert.equal(planRemix([entry],tracks,{remixMaxVisits:3})[0].dj,null);
 const raised=planRemix([entry],tracks,{remixMaxVisits:5})[0];assert(raised.dj);assert.equal(raised.dj.start,120);
 assert.equal(planRemix([entry],tracks,{remixMaxVisits:1})[0].dj,null);
 const pinned=planRemix([raised],tracks,{remixMaxVisits:1,pin:raised})[0];assert.equal(pinned.dj.start,120);
 const [a,b]=planRemix([entry,{id:'b',trackId:'a',remixVisits:0}],tracks,{remixMaxVisits:3});assert.equal(a.dj,null);assert(b.dj);
});
