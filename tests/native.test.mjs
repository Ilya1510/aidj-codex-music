import test from 'node:test';
import assert from 'node:assert/strict';
const calls=[];
globalThis.AndroidMusic={request(id,body){const value=JSON.parse(body);calls.push(value);queueMicrotask(()=>globalThis.__aidjNativeReply(id,{ok:true}));}};
const {NativeAudio,NativeDownloads}=await import('../miniapp/native.mjs');
test('native transport sends the persistent queue to Android and follows Android playback state',async()=>{
  const audio=new NativeAudio();const snapshot={entries:[{id:'one',trackId:'song'}],currentId:'one',tracks:{song:{title:'Song'}},uiRevision:123};audio.queueFactory=()=>snapshot;
  await audio.play();assert.deepEqual(calls.at(-1),{type:'play',queue:snapshot});
  let plays=0;audio.addEventListener('play',()=>plays++);audio.accept({paused:false,time:12,duration:180});
  assert.equal(audio.paused,false);assert.equal(audio.currentTime,12);assert.equal(plays,1);
  audio.currentTime=42;await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(calls.at(-1),{type:'seek',time:42});
  audio.pause();await new Promise(resolve=>setTimeout(resolve,0));assert.equal(calls.at(-1).type,'pause');
  audio.accept({paused:true,time:42,duration:180});assert.equal(audio.paused,true);
});
test('native downloads are delegated to Android and restored progress does not start new browser downloads',async()=>{
  let updates=0;const downloads=new NativeDownloads(()=>updates++);const tracks=[{id:'song',md5:'hash',bytes:100}];
  downloads.add(tracks);await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(calls.at(-1),{type:'download',tracks});
  const before=calls.length;downloads.accept({active:tracks[0],pending:[],completed:4,total:5,paused:false,error:null});
  assert.equal(downloads.active.id,'song');assert.equal(downloads.completed,4);assert.equal(calls.length,before);assert.equal(updates,1);
  downloads.accept({active:null,pending:[],completed:5,total:5,paused:false,error:null});assert.equal(downloads.completed,5);
});

test('background batch progress and automatic network waiting never restart downloads',async()=>{
 const downloads=new NativeDownloads(()=>{}),track={id:'song',md5:'hash',bytes:100};
 const before=calls.length;
 downloads.accept({active:track,pending:[],completed:4,total:10,paused:false,error:null,phase:'Скачиваю',percent:45,bytesTotal:1000,bytesDownloaded:450});
 assert.equal(downloads.percent,45);assert.equal(downloads.bytesTotal,1000);
 downloads.accept({active:track,pending:[],completed:4,total:10,paused:false,error:null,phase:'Жду подключения к сети',percent:45,bytesTotal:1000});
 assert.equal(downloads.paused,false);assert.equal(downloads.error,null);assert.equal(downloads.phase,'Жду подключения к сети');
 downloads.accept({active:track,pending:[],completed:4,total:10,paused:false,error:null,phase:'Скачиваю',percent:46,bytesTotal:1000});
 assert.equal(downloads.percent,46);assert.equal(calls.length,before);
});

test('Resume sends an explicit desired state even when snapshots change during the request',async()=>{
 globalThis.AndroidMusic.version=()=>16;
 const downloads=new NativeDownloads(()=>{});downloads.accept({paused:true,error:null});
 const first=downloads.setPaused(false);
 downloads.accept({paused:false,error:null});
 const second=downloads.setPaused(false);await Promise.all([first,second]);
 assert.deepEqual(calls.slice(-2),[{type:'downloadPause',paused:false},{type:'downloadPause',paused:false}]);
 assert.equal(downloads.controlling,false);
 await downloads.setPaused(true);assert.deepEqual(calls.at(-1),{type:'downloadPause',paused:true});
});

test('identical native download polls do not rebuild UI but real progress and queue changes notify',()=>{
 let updates=0;const downloads=new NativeDownloads(()=>updates++);
 const track={id:'song',md5:'hash',title:'Song',artist:'Artist',bytes:100,djV4:{anchors:Array(256).fill([1,2])}};
 const state={active:track,pending:[{id:'next',md5:'next-hash',title:'Next',bytes:50}],completed:1,total:3,paused:false,error:null,phase:'Скачиваю',percent:25,bytesTotal:200,bytesDownloaded:50};
 downloads.accept(state);assert.equal(updates,1);
 downloads.accept(structuredClone(state));assert.equal(updates,1,'newly parsed objects with identical values are unchanged');
 const metadata=structuredClone(state);metadata.active.djV4.anchors.push([3,4]);downloads.accept(metadata);assert.equal(updates,1,'unused DJ analysis does not rebuild download controls');
 const progress={...state,percent:26,bytesDownloaded:52};downloads.accept(progress);assert.equal(updates,2);assert.equal(downloads.bytesDownloaded,52);
 const phase={...progress,phase:'Жду подключения к сети'};downloads.accept(phase);assert.equal(updates,3);
 const pending={...phase,pending:[{id:'replacement',md5:'replacement-hash',title:'Replacement',bytes:50}]};downloads.accept(pending);assert.equal(updates,4,'same-size pending queue with different songs is a change');
 const renamed={...pending,active:{...track,title:'Renamed'}};downloads.accept(renamed);assert.equal(updates,5);
 const paused={...renamed,paused:true,pauseReason:'manual'};downloads.accept(paused);assert.equal(updates,6);
 downloads.accept(structuredClone(paused));assert.equal(updates,6);
});

test('download error equality preserves Error identity and older snapshots reset failure defaults',()=>{
 let updates=0;const downloads=new NativeDownloads(()=>updates++);
 downloads.accept({active:null,pending:[],error:null});assert.equal(updates,0,'initial idle poll has no visible change');
 downloads.accept({failedCount:2,needsRetry:true,error:'Не удалось скачать'});const error=downloads.error;assert.equal(updates,1);
 downloads.accept({failedCount:2,needsRetry:true,error:'Не удалось скачать'});assert.equal(updates,1);assert.strictEqual(downloads.error,error);
 downloads.accept({error:'Нет подключения'});assert.equal(updates,2);assert.equal(downloads.error.message,'Нет подключения');assert.equal(downloads.failedCount,0);assert.equal(downloads.needsRetry,false);
 downloads.accept({});assert.equal(updates,3);assert.equal(downloads.error,null,'an omitted native error clears the prior error');
 downloads.accept({error:null});assert.equal(updates,3);
});

test('native snapshots still clear a local command error and preserve control updates',async()=>{
 let updates=0;const downloads=new NativeDownloads(()=>updates++);
 downloads.error=new Error('Local request failed');downloads.notify();assert.equal(updates,1);
 downloads.accept({error:null});assert.equal(updates,2);assert.equal(downloads.error,null);
 await downloads.setPaused(true);assert.equal(updates,4,'request start and finish still update disabled controls');
 downloads.accept({error:null});assert.equal(updates,4,'a poll after the request does not redraw unchanged controls');
});
