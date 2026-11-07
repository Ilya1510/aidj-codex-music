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
