const sorts=new Set(['default','likes','dislikes','score','votes']);
const states=new Set(['all','rated','unrated','mine-missing']);
export const voteKinds=['like','dislike','neutral','unrated'];
export function listSettings(value){
  const raw=value?.minScore??(value?.minLikes>0?value.minLikes:null);
  const minimum=raw===null||raw===undefined||raw===''?null:Number(raw);
  const people=new Map();
  for(const rule of Array.isArray(value?.people)?value.people:[]){
    if(!rule||typeof rule.id!=='string'||!/^[a-zA-Z0-9_-]{1,40}$/.test(rule.id)||!Array.isArray(rule.votes))continue;
    const votes=voteKinds.filter(kind=>rule.votes.includes(kind));
    if(votes.length)people.set(rule.id,{id:rule.id,votes});
  }
  const priorityOrder=Array.isArray(value?.priorityOrder)?[...new Set(value.priorityOrder.filter(id=>typeof id==='string'&&/^[a-zA-Z0-9_-]{1,40}$/.test(id)))]:null;
  const priorityOutcomes=Array.isArray(value?.priorityOutcomes)?['like','dislike','unrated'].filter(v=>value.priorityOutcomes.includes(v)):['like'];
  return {ratingMode:value?.ratingMode==='priority'?'priority':'people',priorityOrder,priorityOutcomes,people:[...people.values()],peopleMode:value?.peopleMode==='any'?'any':'all',sort:sorts.has(value?.sort)?value.sort:'default',state:states.has(value?.state)?value.state:'all',
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
export function personVote(track,id,review={},pending={}){
  const person=review.voters?.find(p=>p.id===id);
  if(!person)return undefined;
  if(person.isMine)return rating(track,pending[track.md5]).mine;
  // A cached catalog from an older app has no complete per-person matrix.
  // Missing data must never be mistaken for an actual absence of a vote.
  if(!review.votes||typeof review.votes!=='object')return undefined;
  return review.votes[track.md5]?.[id]??null;
}
export function matchesPeople(track,options,review={},pending={}){
  if(!options.people.length)return true;
  const matches=rule=>{
    const value=personVote(track,rule.id,review,pending);
    if(value===undefined)return false;
    const kind=value===1?'like':value===-1?'dislike':value===0?'neutral':value===null?'unrated':null;
    return rule.votes.includes(kind);
  };
  return options.peopleMode==='any'?options.people.some(matches):options.people.every(matches);
}
export function priorityRating(track,order,review={},pending={}){
  for(const id of order||[]){
    const value=personVote(track,id,review,pending);
    // An unavailable higher-priority vote cannot safely be skipped.
    if(value===undefined)return {value:null,personId:null,known:false};
    if(value===1||value===-1)return {value,personId:id,known:true};
  }
  return {value:null,personId:null,known:true};
}
export function movePerson(order,from,to){
  const result=[...order];
  if(!Number.isInteger(from)||!Number.isInteger(to)||from<0||to<0||from>=result.length||to>=result.length)return result;
  const [id]=result.splice(from,1);result.splice(to,0,id);return result;
}
function matchesRating(track,r,options,pending,review){
  if(options.ratingMode==='priority'){
    const decision=priorityRating(track,options.priorityOrder,review,pending);
    return decision.known&&options.priorityOutcomes.includes(decision.value===1?'like':decision.value===-1?'dislike':'unrated');
  }
  return matchesPeople(track,options,review,pending)&&(!options.noDislikes||r.dislikes===0)&&
    (options.minScore===null||r.likes-r.dislikes>=options.minScore)&&
    (options.state==='all'||options.state==='rated'&&r.total>0||options.state==='unrated'&&r.total===0||options.state==='mine-missing'&&r.mine===null);
}
export function selectRated(list,settings={},pending={},review={}){
  const options=listSettings(settings);
  return list.map((track,index)=>({track,index,r:rating(track,pending[track.md5])})).filter(({track,r})=>matchesRating(track,r,options,pending,review)
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
    const priority=r.proposals>0&&r.total===0?0:1;
    return {track,priority,key:-Math.log(Math.max(Number.MIN_VALUE,random()))/weight};
  }).sort((a,b)=>a.priority-b.priority||a.key-b.key).map(x=>x.track);
}
