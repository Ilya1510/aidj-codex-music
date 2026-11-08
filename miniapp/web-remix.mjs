import {planRemix} from './dj.mjs';
import {Queue} from './player.mjs';
import {playbackTrack} from './dj.mjs';
import {segment} from './segments.mjs';

const signature=c=>Math.round(c.start*1000)+':'+Math.round(c.end*1000);
export function arrangeWebRemix(queue,tracks,preferences){
  const at=queue.index;if(at<0)return;
  const current=queue.current,cap=preferences.remixMaxVisits,depth=preferences.remixLookahead;
  const rest=queue.entries.slice(at+1).filter(e=>e.djFull||(e.remixVisits||0)<cap);
  const options=planRemix(rest.slice(0,depth),tracks,{remixDurationSeconds:preferences.remixDurationSeconds,remixMaxVisits:cap});
  const left=current.dj,bpm=(left?.bpm||0)*(left?.rate||1);
  let chosen=options[0],cost=Infinity;
  for(const [i,e] of options.entries()){
    const target=e.dj?.bpm||0;
    const score=i*.2+(e.dj?0:3)+(bpm&&target?Math.min(2,Math.abs(Math.log(target/bpm))*5):.4)+(e.trackId===current.trackId?3:0);
    if(score<cost){chosen=e;cost=score;}
  }
  const manual=rest.find(e=>e.id===queue.manualNextId);
  if(manual)chosen=planRemix([manual],tracks,{pin:manual,remixDurationSeconds:preferences.remixDurationSeconds,remixMaxVisits:cap})[0];
  if(chosen?.dj){const rate=chosen.dj.timedFallback||!chosen.dj.bpm||!bpm?1:Math.max(.97,Math.min(1.03,bpm/chosen.dj.bpm));chosen={...chosen,dj:{...chosen.dj,rate}};}
  queue.entries=[current,...(chosen?[chosen,...rest.filter(e=>e.id!==chosen.id)]:rest)];
}

export function finishWebRemix(queue,tracks,preferences,skipped=false){
  const current=queue.current;if(!current)return false;
  const values=queue.entries.slice(queue.index+1);
  if(!skipped&&current.remix&&!current.djFull){
    const used=[...new Set([...(current.remixUsed||[]),...(current.dj?[signature(current.dj)]:[])])],visits=(current.remixVisits||0)+1;
    if(visits<preferences.remixMaxVisits){
      const {dj,remix,...old}=current;
      const candidate=planRemix([{...old,remixUsed:used,remixVisits:visits}],tracks,{remixDurationSeconds:preferences.remixDurationSeconds,remixMaxVisits:preferences.remixMaxVisits})[0];
      if(candidate?.remix)values.splice(Math.min(3,values.length),0,candidate);
    }
  }
  queue.entries=values;queue.currentId=values[0]?.id||null;
  if(queue.manualNextId===queue.currentId)queue.manualNextId=null;
  if(queue.currentId)arrangeWebRemix(queue,tracks,preferences);
  return !!queue.currentId;
}

export function forecastWebRemix(queue,tracks,preferences){
  const copy=new Queue({...queue.snapshot(),entries:queue.entries.map(e=>({...e,dj:e.dj?{...e.dj}:undefined,remixUsed:e.remixUsed?[...e.remixUsed]:undefined}))}),segments=[];
  for(let i=0;i<10&&finishWebRemix(copy,tracks,preferences);i++){
    const e=copy.current,t=tracks.get(e.trackId);if(!t)break;const part=segment(playbackTrack(t,e,true));
    segments.push({position:i+1,entryId:e.id,trackId:e.trackId,start:part.start,end:part.end});
  }
  return {currentId:queue.currentId,segments};
}
