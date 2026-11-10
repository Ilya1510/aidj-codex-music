import {fixedCue,endsAtSourceEnd,fixedGain} from './dj.mjs';
const clamp=n=>Math.max(0,Math.min(1,n));
// Position-based envelopes survive pause and promotion without restarting a cue.
export function fixedBlendGains(left,leftEntry,leftTime,leftDuration,right,rightEntry,rightTime,rightDuration,progress){
  const cue=fixedCue(left,leftEntry),next=fixedCue(right,rightEntry);
  if(!cue||!next)return null;
  const end=endsAtSourceEnd(left,leftEntry)&&Number.isFinite(leftDuration)?leftDuration:cue.end;
  const intro=cue.fadeIn>0?clamp((leftTime-cue.start)/cue.fadeIn):1;
  const tail=cue.fadeOut>0?clamp((end-leftTime)/cue.fadeOut):1,p=clamp(progress);
  return {out:.7*intro*Math.min(tail,Math.cos(p*Math.PI/2)),
    incoming:fixedGain(right,rightEntry,rightTime,rightDuration)*Math.sin(p*Math.PI/2)};
}
export class FixedBlend {
  constructor({snapshot,resolve,create=()=>new Audio(),promote,volume=(track,gain)=>gain,revoke=url=>URL.revokeObjectURL(url)}){
    Object.assign(this,{snapshot,resolve,create,onPromote:promote,volume,revoke});this.serial=0;this.state=null;
  }
  context(){
    const s=this.snapshot();if(!s?.enabled||!s.entry||!s.next)return null;
    const cue=fixedCue(s.track,s.entry),next=fixedCue(s.nextTrack,s.next);
    if(!cue||!next||cue.transitionKind!=='crossfade'||cue.transitionSeconds<=0)return null;
    return {...s,cue,nextCue:next,key:JSON.stringify([s.entry.id,s.next.id,s.track.md5,s.nextTrack.md5,cue,next])};
  }
  cancel(){
    this.serial++;const state=this.state;this.state=null;
    if(state){state.deck.volume=0;state.deck.pause();state.deck.removeAttribute('src');state.deck.load();if(state.url)this.revoke(state.url);}
  }
  pause(){const s=this.state;if(s){s.deck.volume=0;s.deck.pause();}}
  async prepare(context){
    const serial=++this.serial,deck=this.create();deck.volume=0;deck.preload='auto';deck.preservesPitch=true;
    const state={key:context.key,deck,url:null,ready:false,mixing:false,starting:false,serial};this.state=state;
    const valid=()=>this.state===state&&this.serial===serial&&this.context()?.key===state.key;
    deck.addEventListener('loadedmetadata',()=>{if(!valid())return;deck.playbackRate=context.nextCue.rate;deck.currentTime=context.nextCue.start;if(context.nextCue.start===0)state.ready=true;});
    deck.addEventListener('seeked',()=>{if(valid())state.ready=true;});
    deck.addEventListener('error',()=>{if(this.state===state){this.failed=state.key;this.cancel();}});
    try{
      const source=await this.resolve(context.nextTrack);
      if(!valid()){if(source.url)this.revoke(source.url);return;}
      state.url=source.url||null;deck.src=source.src;deck.load();
    }catch{if(this.state===state){this.failed=state.key;this.cancel();}}
  }
  start(state){
    if(state.starting||!state.deck.paused)return;
    state.starting=true;state.deck.volume=0;
    Promise.resolve(state.deck.play()).then(()=>{
      state.starting=false;
      if(state.promoted)return;
      if(this.state!==state||this.context()?.key!==state.key||this.snapshot().audio.paused){state.deck.volume=0;state.deck.pause();}
    }).catch(()=>{state.starting=false;if(this.state===state){this.failed=state.key;this.cancel();}});
  }
  tick(){
    const context=this.context();
    if(!context){if(this.state)this.cancel();return;}
    if(this.state&&this.state.key!==context.key)this.cancel();
    const audio=context.audio;
    if(audio.paused){this.pause();return;}
    if(audio.seeking){if(this.state)this.cancel();return;}
    if(!this.state){if(this.failed!==context.key)this.prepare(context);return;}
    const state=this.state;if(!state.ready)return;
    const end=endsAtSourceEnd(context.track,context.entry)&&Number.isFinite(audio.duration)?audio.duration:context.cue.end;
    const remaining=end-audio.currentTime;
    if(!state.mixing&&remaining>0&&remaining<=context.cue.transitionSeconds*audio.playbackRate){
      state.mixing=true;state.start=audio.currentTime;state.span=Math.max(.001,remaining);
    }
    if(state.mixing){this.start(state);this.apply();}
  }
  apply(){
    const context=this.context(),state=this.state;
    if(!context||!state||state.key!==context.key||!state.mixing)return false;
    if(context.audio.paused){this.pause();return false;}
    const progress=clamp((context.audio.currentTime-state.start)/state.span);
    const gains=fixedBlendGains(context.track,context.entry,context.audio.currentTime,context.audio.duration,
      context.nextTrack,context.next,state.deck.currentTime,state.deck.duration,progress);
    if(!gains)return false;
    context.audio.volume=this.volume(context.track,gains.out);
    state.deck.volume=state.deck.paused?0:this.volume(context.nextTrack,gains.incoming);
    return true;
  }
  promote(){
    const context=this.context(),state=this.state;
    if(!context||!state||state.key!==context.key||!state.ready||!state.mixing||state.deck.paused)return false;
    state.promoted=true;this.state=null;this.serial++;
    this.onPromote(state.deck,state.url,context.next);
    return true;
  }
}
