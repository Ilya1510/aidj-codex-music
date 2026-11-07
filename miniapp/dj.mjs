// Queue-only cues: originals, manual excerpts and playlist order stay independent.
export const modeName=value=>({normal:'Обычное',smooth:'Плавное',dj:'DJ'})[value?.mode]||'Обычное';
export function djCue(track,length='normal'){
  if(!track||Number(track.rangeStart)>0||Number(track.rangeEnd)<Number(track.duration)-.1)return null;
  const target={short:80,normal:110,long:140}[length]||110;
  const cues=(track.dj?.cues||[]).filter(c=>[c.start,c.end,c.bpm,c.fade].every(Number.isFinite)&&c.start>=0&&c.end<=track.duration+.1&&c.end-c.start>=30&&c.bpm>=60&&c.bpm<=210&&c.fade>=.3&&c.fade<=8);
  return cues.sort((a,b)=>Math.abs(a.end-a.start-target)-Math.abs(b.end-b.start-target))[0]||null;
}
export function planDj(entries,tracks,length,{reorder=false,pin=null}={}){
  const result=entries.map(e=>({...e,dj:null}));
  const tempo=e=>djCue(tracks.get(e.trackId),length)?.bpm||0;
  // Only a newly shuffled queue may be ordered by neighbours. Never touch playlist membership/order.
  if(reorder)for(let i=1;i<result.length;i++){
    const bpm=tempo(result[i-1]);if(!bpm)continue;
    let best=i,score=Infinity;
    for(let j=i;j<result.length;j++){const b=tempo(result[j]),s=b?Math.abs(Math.log(b/bpm)):Infinity;if(s<score){score=s;best=j;}}
    [result[i],result[best]]=[result[best],result[i]];
  }
  let group=[],low=0,high=Infinity;
  const finish=()=>{if(!group.length)return;const mean=group.reduce((n,e)=>n+e.dj.bpm,0)/group.length,target=Math.max(low,Math.min(high,mean));for(const e of group)e.dj.rate=target/e.dj.bpm;group=[];low=0;high=Infinity;};
  for(const entry of result){
    const cue=djCue(tracks.get(entry.trackId),length);
    if(!cue||entry.djFull){finish();continue;}
    entry.dj={...cue,rate:1};
    const lo=cue.bpm*.97,hi=cue.bpm*1.03;
    if(Math.max(low,lo)>Math.min(high,hi))finish();
    low=Math.max(low,lo);high=Math.min(high,hi);
    if(entry.id===pin?.id&&pin.dj?.rate>=.97&&pin.dj.rate<=1.03){const fixed=cue.bpm*pin.dj.rate;low=high=fixed;}
    group.push(entry);
  }
  finish();return result;
}
export function playbackTrack(track,entry,enabled){
  if(!track||!enabled||entry?.djFull||!entry?.dj)return track;
  // A manual excerpt saved while the queue is running has priority too.
  if(Number(track.rangeStart)>0||Number(track.rangeEnd)<track.duration-.1)return track;
  return {...track,rangeStart:entry.dj.start,rangeEnd:entry.dj.end};
}
