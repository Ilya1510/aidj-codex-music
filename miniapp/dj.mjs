// Queue-only cues: originals, manual excerpts and playlist order stay independent.
export const modeName=value=>({normal:'Обычное',smooth:'Плавное',dj:'DJ',remix:'DJ: ремикс'})[value?.mode]||'Обычное';
const acoustic=c=>c?.acoustic===true&&c.needsReview===true&&c.quality==='acoustic';
const guarded=c=>c.entryVerified===true&&c.wordGuarded===true&&Number.isFinite(c.mix)&&c.mix>=1.5&&c.mix<=8;
const minimumFade=(c,remix=false)=>guarded(c)?(remix?.15:.25):.5;
export function djCue(track,length='normal'){
  if(!track||Number(track.rangeStart)>0||Number(track.rangeEnd)<Number(track.duration)-.1)return null;
  const target={short:80,normal:110,long:140}[length]||110;
  const cues=((track.djV4||track.djV3||track.djV2||track.dj)?.cues||[]).filter(c=>[c.start,c.end,c.bpm,c.fade].every(Number.isFinite)&&c.start>=0&&c.end<=track.duration+.1&&c.end-c.start>=30&&(c.bpm===0||c.bpm>=60&&c.bpm<=210)&&c.fade>=minimumFade(c)&&c.fade<=8);
  const cue=cues.sort((a,b)=>Math.abs(a.end-(a.entryVerified||acoustic(a)?a.start:0)-target)-Math.abs(b.end-(b.entryVerified||acoustic(b)?b.start:0)-target))[0];
  if(cue)return {...cue,start:cue.entryVerified||acoustic(cue)?cue.start:0};
  if(!Number.isFinite(track.duration)||track.duration<30)return null;
  // A reusable graph can contain valid long passages even when the original
  // static pool had no preselected match. Pair its exact observed boundaries.
  for(const provisional of [false,true]){
    const candidates=anchorCandidates(track,target,provisional,{minimum:30,maximum:track.duration,minimumFade:provisional?.5:.25});
    const cost=c=>Math.abs(c.end-c.start-target)/target+.35*(1-c.energy)+(c.start===0?.1:0)+.12*Math.max(0,1.2-c.fade);
    const best=candidates.sort((a,b)=>cost(a)-cost(b))[0];
    if(best)return best;
  }
  return null;
}
export function planDj(entries,tracks,length,{reorder=false,pin=null}={}){
  const result=entries.map(e=>({...e,dj:null,remix:false}));
  const tempo=e=>djCue(tracks.get(e.trackId),length)?.bpm||0;
  // Only a newly shuffled queue may be ordered by neighbours. Never touch playlist membership/order.
  if(reorder)for(let i=1;i<result.length;i++){
    const bpm=tempo(result[i-1]);if(!bpm)continue;
    let best=i,score=Infinity;
    for(let j=i;j<Math.min(result.length,i+10);j++){const b=tempo(result[j]),s=b?Math.abs(Math.log(b/bpm)):Infinity;if(s<score){score=s;best=j;}}
    [result[i],result[best]]=[result[best],result[i]];
  }
  let group=[],low=0,high=Infinity;
  const finish=()=>{if(!group.length)return;const mean=group.reduce((n,e)=>n+e.dj.bpm,0)/group.length,target=Math.max(low,Math.min(high,mean));for(const e of group)e.dj.rate=target/e.dj.bpm;group=[];low=0;high=Infinity;};
  for(const entry of result){
    const cue=djCue(tracks.get(entry.trackId),length);
    if(!cue||entry.djFull){finish();continue;}
    entry.dj={...cue,rate:1};
    if(!cue.bpm){finish();continue;}
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
  return {...track,rangeStart:entry.dj.entryVerified||acoustic(entry.dj)?entry.dj.start:0,rangeEnd:entry.dj.end};
}

export function djProgress(ids,tracks){
  const counts={ready:0,processed:0,pending:0,working:0,failed:0,total:ids.length};
  for(const id of ids){const t=tracks.get(id);if(djCue(t))counts.ready++;
    if(t?.djStatus==='complete')counts.processed++;
    else if(t?.djStatus==='working')counts.working++;
    else if(t?.djStatus==='failed')counts.failed++;
    else if(t?.djStatus==='pending')counts.pending++;
  }
  return counts;
}

export function remixDurationSeconds(value=12){return typeof value==='number'&&Number.isFinite(value)?Math.max(10,Math.min(120,value)):12;}
function remixGraph(track,isAcoustic=false){
  if(!track||Number(track.rangeStart)>0||Number(track.rangeEnd)<Number(track.duration)-.1)return null;
  const a=track.djV4?.[isAcoustic?'acousticAnchors':'anchors'];
  if(!a||a.version!==1||(isAcoustic?a.wordGuarded!==false||a.needsReview!==true||a.quality!=='acoustic':a.wordGuarded!==true)||!Number.isFinite(a.bpm)||a.bpm!==0&&(a.bpm<60||a.bpm>210)||!Array.isArray(a.entries)||!Array.isArray(a.exits))return null;
  const scale=a.packed===true?1000:1,energy=n=>Number.isFinite(n)?Math.max(0,Math.min(1,n/scale)):.5;
  const entryRows=a.flat===true?Array.from({length:Math.min(256,Math.floor(a.entries.length/2))},(_,i)=>a.entries.slice(i*2,i*2+2)):a.entries;
  const exitRows=a.flat===true?Array.from({length:Math.min(256,Math.floor(a.exits.length/3))},(_,i)=>a.exits.slice(i*3,i*3+3)):a.exits;
  const entries=entryRows.slice(0,256).filter(x=>Array.isArray(x)&&Number.isFinite(x[0])&&x[0]>=0&&x[0]/scale<=track.duration-8).map(x=>[x[0]/scale,energy(x[1])]).sort((x,y)=>x[0]-y[0]);
  const exits=exitRows.slice(0,256).filter(x=>Array.isArray(x)&&Number.isFinite(x[0])&&x[0]>=0&&x[0]/scale<=track.duration+.1&&Number.isFinite(x[1])&&x[1]/scale>=(isAcoustic?.5:.15)&&x[1]/scale<=2).map(x=>[x[0]/scale,x[1]/scale,energy(x[2])]).sort((x,y)=>x[0]-y[0]);
  if(!entries.length||!exits.length)return null;
  return {version:1,wordGuarded:!isAcoustic,...(isAcoustic?{quality:'acoustic',needsReview:true}:{}),bpm:a.bpm,...(a.bpm>0&&Number.isFinite(a.grid)&&a.grid>=0&&a.grid<60/a.bpm?{grid:a.grid}:{}),entries,exits};
}
export const remixAnchors=track=>remixGraph(track,false);
export const remixAcousticAnchors=track=>remixGraph(track,true);
function anchorCandidates(track,desired,isAcoustic=false,{minimum=8,maximum=123,minimumFade=isAcoustic?.5:.15}={}){
  const graph=remixGraph(track,isAcoustic);if(!graph)return [];
  const a={...graph,exits:graph.exits.filter(row=>row[1]>=minimumFade)};
  const result=[],seen=new Set();
  for(const [start,entryEnergy] of a.entries){
    let lo=0,hi=a.exits.length;while(lo<hi){const mid=(lo+hi)>>1;if(a.exits[mid][0]<start+desired)lo=mid+1;else hi=mid;}
    for(let i=Math.max(0,lo-1);i<Math.min(a.exits.length,lo+1);i++){
      const [end,fade,exitEnergy]=a.exits[i],span=end-start,key=Math.round(start*1000)+':'+Math.round(end*1000);if(span<minimum||span>maximum||seen.has(key))continue;
      seen.add(key);result.push({start,end,bpm:a.bpm,fade,mix:3.2,energy:(entryEnergy+exitEnergy)/2,entryVerified:!isAcoustic,wordGuarded:!isAcoustic,...(isAcoustic?{acoustic:true,quality:'acoustic',needsReview:true}:{}),...(a.grid!=null?{grid:a.grid}:{}),rate:1});
    }
  }
  return result;
}
export function remixCues(track,desired=12){
  if(!track||Number(track.rangeStart)>0||Number(track.rangeEnd)<Number(track.duration)-.1)return [];
  desired=remixDurationSeconds(desired);
  const dynamic=[...anchorCandidates(track,desired),...anchorCandidates(track,desired,true)].filter(c=>Math.abs(c.end-c.start-desired)<=3);if(dynamic.length)return dynamic;
  return ((track.djV4||track.djV3)?.remix||[]).filter(c=>c&&[c.start,c.end,c.bpm,c.fade].every(Number.isFinite)&&c.start>=0&&c.end<=track.duration+.1&&c.end-c.start>=8&&c.end-c.start<=30&&Math.abs(c.end-c.start-desired)<=3&&(c.start===0||c.entryVerified===true||acoustic(c))&&(c.bpm===0||c.bpm>=60&&c.bpm<=210)&&c.fade>=minimumFade(c,true)&&c.fade<=8).slice(0,32).map(c=>({...c,rate:1}));
}
const cueSignature=c=>Math.round(c.start*1000)+':'+Math.round(c.end*1000);
function heardOverlap(cue,used){
  let overlap=0;
  for(const key of used){const [lo,hi]=key.split(':').map(n=>Number(n)/1000);if(![lo,hi].every(Number.isFinite)||hi<=lo)continue;overlap=Math.max(overlap,Math.max(0,Math.min(cue.end,hi)-Math.max(cue.start,lo))/Math.min(cue.end-cue.start,hi-lo));}
  return overlap;
}
export function planRemix(entries,tracks,{pin=null,nextPin=null,remixDurationSeconds:desired=12}={}){
  desired=remixDurationSeconds(desired);
  return entries.map(entry=>{
    const track=tracks.get(entry.trackId),cues=remixCues(track,desired),anchors=remixAnchors(track),acousticAnchors=remixAcousticAnchors(track),graph=anchors||acousticAnchors;
    const keep=entry.id===pin?.id?pin:entry.id===nextPin?.id?nextPin:null;
    // Native owns the moving queue and its next prepared deck. Refresh only the
    // available choices, retaining both selected snippets and completed visits.
    if(keep)return {...entry,...keep,remixCues:graph?[]:cues,remixAnchors:anchors,remixAcousticAnchors:acousticAnchors};
    const used=new Set(Array.isArray(entry.remixUsed)?entry.remixUsed:[]);
    let remaining=cues.filter(c=>!used.has(cueSignature(c)));
    if(!used.size&&entry.remixVisits)remaining=remaining.slice(entry.remixVisits);
    const wordFit=remaining.filter(c=>!acoustic(c));if(wordFit.length)remaining=wordFit;
    const nearby=remaining.filter(c=>Math.abs(c.end-c.start-desired)<=3);if(nearby.length)remaining=nearby;
    const score=c=>heardOverlap(c,used)*8+Math.max(0,Math.abs(c.end-c.start-desired)-3)/3+(1-(Number.isFinite(c.energy)?Math.max(0,Math.min(1,c.energy)):.5))*.4;
    const cue=(entry.remixVisits||0)>=3?null:remaining.sort((a,b)=>score(a)-score(b))[0]||null;
    const explicit=entry.djFull||Number(track?.rangeStart)>0||Number(track?.rangeEnd)<Number(track?.duration)-.1;
    return {...entry,remixAnchors:anchors,remixAcousticAnchors:acousticAnchors,remixCues:graph?[]:cues,remix:!entry.djFull&&!!cue,remixNeedsReview:!explicit&&(!cue||acoustic(cue)),remixFallback:!explicit&&!cue?'full-source':null,dj:entry.djFull?null:cue};
  });
}

// Keep a full-song candidate pool small enough for the native bridge and its
// persistent queue, including hundreds of tracks. Current .dj stays readable.
function packGraph(graph){
  if(!graph||graph.packed===true||!Array.isArray(graph.entries)||!Array.isArray(graph.exits))return graph;
  return {...graph,packed:true,flat:true,entries:graph.entries.slice(0,256).flatMap(([time,energy])=>[Math.round(time*1000),Math.round(energy*1000)]),exits:graph.exits.slice(0,256).flatMap(([time,fade,energy])=>[Math.round(time*1000),Math.round(fade*1000),Math.round(energy*1000)])};
}
export function packRemixEntries(entries){
  return entries.map(entry=>{
    const {remixCues,remixPool,...rest}=entry;
    const pool=entry.remixAnchors||entry.remixAcousticAnchors?[]:Array.isArray(remixCues)?remixCues.slice(0,32).map(c=>[c.start,c.end,c.bpm,c.fade,c.mix??3.2,c.grid??null,c.energy??.5,c.wordGuarded===true?1:0,c.entryVerified===true?1:0,...(acoustic(c)?[1,1]:[])]):Array.isArray(remixPool)?remixPool.slice(0,32):[];
    const result={...rest,...(entry.remixAnchors?{remixAnchors:packGraph(entry.remixAnchors)}:{}),...(entry.remixAcousticAnchors?{remixAcousticAnchors:packGraph(entry.remixAcousticAnchors)}:{})};
    return pool.length?{...result,remixPool:pool}:result;
  });
}
