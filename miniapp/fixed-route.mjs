import {fixedCue} from './dj.mjs';

const stale=()=>new Error('Переходы DJ 1 изменились. Обнови библиотеку и запусти новый маршрут.');
const finite=n=>typeof n==='number'&&Number.isFinite(n);
const rotate=(values,at)=>[...values.slice(at),...values.slice(0,at)];
const reverse=(values,first,last)=>{while(first<last){[values[first],values[last]]=[values[last],values[first]];first++;last--;}};
const sourceFields=['start','end','fadeIn','fadeOut','rate'];

export function graphReady(set){
  const graph=set?.graph;
  return !!(set?.tracks?.length>1&&graph?.version===1&&graph.baseRevision===set.revision&&/^[a-f0-9]{20}$/.test(graph.revision)&&graph.edges&&graph.artists);
}

function compile(set,tracks){
  if(!graphReady(set)||!Array.isArray(set.tracks)||!set.tracks.length||set.tracks.length>2000)throw stale();
  const graph=set.graph,rows=new Map(),adjacency=new Map(),artists=new Map();
  if(!Number.isInteger(graph.degree)||graph.degree<1||graph.degree>10)throw stale();
  for(const row of set.tracks){
    if(!row||typeof row.trackId!=='string'||rows.has(row.trackId))throw stale();
    const cue={sourceMd5:row.md5,...Object.fromEntries(sourceFields.map(k=>[k,row[k]])),transitionSeconds:row.transitionSeconds,transitionKind:row.transitionKind};
    if(!fixedCue(tracks.get(row.trackId),{fixed:cue}))throw stale();
    const credits=graph.artists[row.trackId];
    if(!Array.isArray(credits)||!credits.length||credits.length>50||credits.some(k=>typeof k!=='string'||!k||k.length>256))throw stale();
    rows.set(row.trackId,row);artists.set(row.trackId,new Set(credits));
  }
  for(const [id,row] of rows){
    const edges=graph.edges[id];
    if(!Array.isArray(edges)||!edges.length||edges.length>graph.degree)throw stale();
    const destinations=new Map();
    for(const edge of edges){
      if(!edge||!rows.has(edge.to)||edge.to===id&&rows.size>1||destinations.has(edge.to)||typeof edge.beatMatched!=='boolean'||!finite(edge.score)||edge.score<0)throw stale();
      const clean=edge.transitionKind==='clean-cut'&&edge.transitionSeconds===0;
      const blend=edge.transitionKind==='crossfade'&&finite(edge.transitionSeconds)&&edge.transitionSeconds>=4.75&&edge.transitionSeconds<=5.25;
      if(!clean&&!blend)throw stale();
      if(!fixedCue(tracks.get(id),{fixed:{sourceMd5:row.md5,...Object.fromEntries(sourceFields.map(k=>[k,row[k]])),transitionSeconds:edge.transitionSeconds,transitionKind:edge.transitionKind}}))throw stale();
      destinations.set(edge.to,edge);
    }
    adjacency.set(id,destinations);
  }
  return {graph,rows,adjacency,artists};
}

function badAt(ids,at,artists){
  if(ids.length<3)return 0;
  const n=ids.length,a=artists.get(ids[(at+n)%n]),b=artists.get(ids[(at+1+n)%n]),c=artists.get(ids[(at+2+n)%n]);
  return [...a].some(key=>b.has(key)&&c.has(key))?1:0;
}
function badCount(ids,artists){return ids.reduce((sum,_,i)=>sum+badAt(ids,i,artists),0);}
function cycleValid(ids,adjacency){return ids.length>0&&ids.every((id,i)=>adjacency.get(id)?.has(ids[(i+1)%ids.length]));}
function sourceMatches(entry,row){
  return entry?.trackId===row.trackId&&entry.fixed?.sourceMd5===row.md5&&sourceFields.every(k=>entry.fixed[k]===row[k]);
}
function edgeMatches(entry,edge){return entry.fixed.transitionKind===edge.transitionKind&&entry.fixed.transitionSeconds===edge.transitionSeconds;}

/** Choose a cycle only from published edges; never fabricate a fallback jump.
 *
 * Reversals preserve coverage and the existing opening/pinned prefix. Checking
 * the internal reverse edges matters for a directed graph. Only the boundary
 * artist triples change; internal reversed triples have the same common credit.
 */
export function graphRoute(set,tracks,{random=Math.random,startId=null,seedEntries=null,prefixLength=0,attempts=null}={}){
  const {graph,rows,adjacency,artists}=compile(set,tracks),all=[...rows.keys()],total=all.length;
  if(typeof random!=='function'||!Number.isInteger(prefixLength)||prefixLength<0)throw stale();
  const draw=()=>{const n=random();if(!finite(n)||n<0||n>=1)throw new Error('Не удалось перемешать маршрут.');return n;};
  let ids=seedEntries?seedEntries.map(e=>e.trackId):all.slice();
  if(!ids.length||new Set(ids).size!==ids.length||ids.some(id=>!rows.has(id))||prefixLength>ids.length||!cycleValid(ids,adjacency))throw stale();
  const preservedCount=Math.min(2,prefixLength===ids.length?prefixLength:Math.max(0,prefixLength-1));
  if(seedEntries){
    for(const entry of seedEntries)if(!sourceMatches(entry,rows.get(entry.trackId))||!fixedCue(tracks.get(entry.trackId),entry))throw stale();
    for(let i=0;i<preservedCount;i++)if(!edgeMatches(seedEntries[i],adjacency.get(ids[i]).get(ids[(i+1)%ids.length])))throw stale();
  }else if(prefixLength)throw stale();
  const limit=attempts??Math.min(4000,Math.max(200,total*6));
  if(!Number.isInteger(limit)||limit<0||limit>4000)throw stale();
  const first=Math.max(1,prefixLength);let bad=badCount(ids,artists),changes=0;
  if(ids.length-first>=2)for(let attempt=0;attempt<limit;attempt++){
    const i=first+Math.floor(draw()*(ids.length-first-1));
    const width=2+Math.floor(draw()*Math.min(19,ids.length-i-1)),j=i+width-1,n=ids.length;
    if(!adjacency.get(ids[i-1]).has(ids[j])||!adjacency.get(ids[i]).has(ids[(j+1)%n]))continue;
    let allowed=true;
    for(let k=i;k<j;k++)if(!adjacency.get(ids[k+1]).has(ids[k])){allowed=false;break;}
    if(!allowed)continue;
    const affected=[...new Set([i-2,i-1,j-1,j].map(at=>(at+n)%n))];
    const before=affected.reduce((sum,at)=>sum+badAt(ids,at,artists),0);
    reverse(ids,i,j);
    const after=affected.reduce((sum,at)=>sum+badAt(ids,at,artists),0);
    if(after>before){reverse(ids,i,j);continue;}
    bad+=after-before;changes++;
  }
  // Rare credit conflicts may need a small omission. A shortcut is accepted
  // only when that exact directed edge exists; coverage loss is capped at5%.
  const maxOmitted=Math.max(1,Math.floor(total*.05));let omitted=total-ids.length;
  if(omitted>maxOmitted)throw stale();
  while(bad&&ids.length>3&&omitted<maxOmitted){
    let removed=false;
    for(let at=Math.max(1,prefixLength);at<ids.length;at++){
      if(!adjacency.get(ids[at-1]).has(ids[(at+1)%ids.length]))continue;
      const candidate=ids.filter((_,i)=>i!==at),nextBad=badCount(candidate,artists);
      if(nextBad>=bad)continue;
      ids=candidate;bad=nextBad;omitted++;removed=true;break;
    }
    if(!removed)break;
  }
  if(bad||!cycleValid(ids,adjacency))throw new Error('Не удалось собрать маршрут с подходящими переходами. Попробуй ещё раз.');
  if(!seedEntries){
    const at=startId==null?Math.floor(draw()*ids.length):ids.indexOf(startId);
    if(at<0)throw stale();ids=rotate(ids,at);
  }else if(startId!=null&&ids[0]!==startId)throw stale();
  const entries=ids.map((id,i)=>{
    const row=rows.get(id),edge=adjacency.get(id).get(ids[(i+1)%ids.length]);
    if(seedEntries&&i<preservedCount)return seedEntries[i];
    const existing=seedEntries&&i<prefixLength?seedEntries[i]:null;
    return {id:existing?.id||'graph-'+set.id+'-'+crypto.randomUUID(),trackId:id,ordinal:existing?.ordinal??i,
      fixed:{sourceMd5:row.md5,...Object.fromEntries(sourceFields.map(k=>[k,row[k]])),transitionSeconds:edge.transitionSeconds,transitionKind:edge.transitionKind}};
  });
  if(entries.some(e=>!fixedCue(tracks.get(e.trackId),e)))throw stale();
  return {entries,trackIds:ids,total,coverage:entries.length,changes,graphRevision:graph.revision};
}
