export const native = globalThis.AndroidMusic || null;
let sequence=0;
const waiting=new Map();
if(native)globalThis.__aidjNativeReply=(id,result)=>{
  const task=waiting.get(String(id));if(!task)return;waiting.delete(String(id));clearTimeout(task.timer);
  if(result.player)globalThis.__aidjNativeState?.({player:result.player,queueConflict:result.queueAccepted===false,requestedUiRevision:result.requestedUiRevision});
  if(result.error)task.reject(new Error(result.error));else task.resolve(result);
};
export function command(type,extra={}){
  if(!native)return Promise.reject(new Error('Android-приложение недоступно.'));
  return new Promise((resolve,reject)=>{
    const id=String(++sequence),timer=setTimeout(()=>{waiting.delete(id);reject(new Error('Сервер не ответил. Попробуй ещё раз.'));},90000);
    waiting.set(id,{resolve,reject,timer});native.request(id,JSON.stringify({type,...extra}));
  });
}
export async function nativeFetch(path,body){const result=await command('api',{path:'/api/'+path,body:body??null});return new Response(JSON.stringify(result.body),{status:result.status,headers:{'Content-Type':'application/json'}});}
export class NativeAudio extends EventTarget {
  constructor(){super();this.paused=true;this.duration=0;this.position=0;this.src='native';this.playbackRate=1;this.queueFactory=()=>({});}
  get currentTime(){return this.position;}
  set currentTime(value){this.position=Number(value)||0;command('seek',{time:this.position}).catch(()=>{});}
  async play(){await command('play',{queue:this.queueFactory()});}
  pause(){command('pause').catch(()=>{});}
  removeAttribute(){}
  load(){}
  accept(state){const before=this.paused;this.paused=state.paused;this.position=state.time||0;this.duration=state.duration||0;
    if(before!==this.paused)this.dispatchEvent(new Event(this.paused?'pause':'play'));
    this.dispatchEvent(new Event('timeupdate'));
  }
}
export class NativeDownloads {
  constructor(changed){this.changed=changed;this.active=null;this.pending=[];this.paused=false;this.completed=0;this.total=0;this.error=null;this.phase='';this.percent=0;this.bytesTotal=0;this.bytesDownloaded=0;this.failedCount=0;this.needsRetry=false;this.pauseReason='';this.controlling=false;this.snapshotKey=this.stateKey();}
  stateKey(){
    // Track snapshots may include large DJ/loudness graphs. Compare the download
    // identity and display metadata without walking those graphs on every poll.
    const track=t=>t?[t.id,t.md5,t.title,t.artist,t.bytes]:null;
    return JSON.stringify([track(this.active),this.pending.map(track),this.paused,this.completed,this.total,this.phase,this.percent,this.bytesTotal,this.bytesDownloaded,this.failedCount,this.needsRetry,this.pauseReason,this.controlling,this.error?.name,this.error?.message]);
  }
  notify(){this.snapshotKey=this.stateKey();this.changed();}
  add(tracks){command('download',{tracks}).catch(error=>{this.error=error;this.notify();});}
  toggle(){return this.setPaused(!(this.paused||this.needsRetry));}
  setPaused(paused){
    const modern=typeof native.version==='function'&&native.version()>=16;
    this.controlling=true;this.notify();
    return command(modern?'downloadPause':'downloadToggle',modern?{paused}:{}).catch(error=>{this.error=error;}).finally(()=>{this.controlling=false;this.notify();});
  }
  cancel(){command('downloadCancel').catch(()=>{});}
  accept(state){
    const message=state.error?String(state.error):null,error=this.error;
    Object.assign(this,{failedCount:0,needsRetry:false},state);
    this.error=message?(error?.name==='Error'&&error.message===message?error:new Error(message)):null;
    const key=this.stateKey();if(key===this.snapshotKey)return;this.snapshotKey=key;this.changed();
  }
}
