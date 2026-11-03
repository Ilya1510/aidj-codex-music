const sorts=new Set(['default','likes','dislikes','score','votes']);
const states=new Set(['all','rated','unrated','mine-missing']);
export function listSettings(value){
  const raw=value?.minScore??(value?.minLikes>0?value.minLikes:null);
  const minimum=raw===null||raw===undefined||raw===''?null:Number(raw);
  return {sort:sorts.has(value?.sort)?value.sort:'default',state:states.has(value?.state)?value.state:'all',
    noDislikes:value?.noDislikes===true,minScore:Number.isFinite(minimum)?Math.min(10000,Math.max(-10000,Math.floor(minimum))):null};
}
export function rating(track,pending=null){
  const source=track?.rating||{},value={likes:0,dislikes:0,neutral:0,total:0,mine:null,proposals:0,...source};
  if(pending){
    const field=v=>v===1?'likes':v===-1?'dislikes':v===0?'neutral':null;
    const before=field(value.mine),after=field(pending.value);
    if(before){value[before]=Math.max(0,value[before]-1);value.total=Math.max(0,value.total-1);}
    if(after){value[after]++;value.total++;}
    value.mine=pending.value;value.pending=true;
  }
  return value;
}
export function selectRated(list,settings={},pending={}){
  const options=listSettings(settings);
  return list.map((track,index)=>({track,index,r:rating(track,pending[track.md5])})).filter(({r})=>
    (!options.noDislikes||r.dislikes===0)&&(options.minScore===null||r.likes-r.dislikes>=options.minScore)&&
    (options.state==='all'||options.state==='rated'&&r.total>0||options.state==='unrated'&&r.total===0||options.state==='mine-missing'&&r.mine===null)
  ).sort((a,b)=>{
    if(options.sort==='default')return a.index-b.index;
    const score=r=>options.sort==='score'?r.likes-r.dislikes:options.sort==='votes'?r.total:r[options.sort];
    return score(b.r)-score(a.r)||b.r.total-a.r.total||a.index-b.index;
  }).map(item=>item.track);
}

// Weighted random order without repetitions; one dislike gives a fixed 25% bonus
// over an unrated song. Positive-only songs yield to songs needing more input.
export function reviewOrder(list,pending={},random=Math.random){
  return list.map(track=>{const r=rating(track,pending[track.md5]);
    const weight=r.dislikes>0?1.25:1/(1+r.total);
    return {track,key:-Math.log(Math.max(Number.MIN_VALUE,random()))/weight};
  }).sort((a,b)=>a.key-b.key).map(x=>x.track);
}
