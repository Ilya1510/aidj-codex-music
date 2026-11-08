import {modeName,djCue,planDj,playbackTrack,djProgress,remixCues as computeRemixCues,remixAnchors as computeRemixAnchors,remixAcousticAnchors as computeRemixAcousticAnchors,planRemix,packRemixEntries,timedCue} from './dj.mjs';
import {Queue} from './player.mjs';
import {sortablePeople,sortableQueue} from './reorder.mjs';
import {PlaylistChanges} from './playlist-changes.mjs';
import * as storage from './storage.mjs';
import {native,command,nativeFetch,NativeAudio,NativeDownloads} from './native.mjs';
import {PersonalPlaylists} from './personal-playlists.mjs';
import {arrangeWebRemix,finishWebRemix,forecastWebRemix} from './web-remix.mjs';
import {LocalTransfer} from './transfer.mjs';
import {segment,moveSegment,changeBoundary,parseTime,formatTime} from './segments.mjs';
import {playbackSettings as normalisePlayback,fadeGain} from './playback.mjs';
import {rating as reviewRating,selectRated,listSettings,reviewOrder,voteKinds,priorityRating,movePerson} from './ratings.mjs';

const $ = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const paths = {
  grip:'<path d="M8 5h1m6 0h1M8 12h1m6 0h1M8 19h1m6 0h1"/>',
  play:'<path class="filled" d="M8 4.8v14.4L19 12Z"/>', pause:'<path class="filled" d="M6 5h4v14H6zm8 0h4v14h-4z"/>',
  next:'<path class="filled" d="M5 5v14l11-7Z"/><path d="M19 5v14"/>', previous:'<path class="filled" d="M19 5v14L8 12Z"/><path d="M5 5v14"/>',
  shuffle:'<path d="m17 3 4 4-4 4M3 17l4-5 4-5h10M3 7h4l4 5 4 5h6m-4-4 4 4-4 4"/>',
  repeat:'<path d="m17 2 4 4-4 4M3 10V8a2 2 0 0 1 2-2h16M7 22l-4-4 4-4m14 0v2a2 2 0 0 1-2 2H3"/>',
  queue:'<path d="M4 5h16M4 11h16M4 17h9"/><path class="filled" d="m17 14 5 4-5 4Z"/>',
  music:'<path d="M9 18V5l12-2v13M9 8l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>', check:'<path d="m5 12 4 4L19 6"/><circle cx="12" cy="12" r="10"/>',
  more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  plus:'<path d="M12 4v16M4 12h16"/>', close:'<path d="m6 6 12 12M6 18 18 6"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>', back:'<path d="m14 5-7 7 7 7"/>',
  down:'<path d="m5 9 7 7 7-7"/>', up:'<path d="m5 15 7-7 7 7"/>', trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  top:'<path d="m5 12 7-7 7 7m-14 7 7-7 7 7"/>',
  edit:'<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14Z"/>', settings:'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="var(--bg)"/><circle cx="16" cy="12" r="2" fill="var(--bg)"/><circle cx="8" cy="18" r="2" fill="var(--bg)"/>',
  external:'<path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/>', folder:'<path d="M3 7V5h7l2 3h9v12H3Z"/>',
};
function icon(name) { const node=document.createElementNS('http://www.w3.org/2000/svg','svg'); node.setAttribute('viewBox','0 0 24 24'); node.setAttribute('aria-hidden','true'); node.innerHTML=paths[name]||paths.music; return node; }
const visibleChildren=children=>children.flat(Infinity).filter(child=>child!=null&&typeof child!=='boolean');
function el(tag, attrs={}, ...children) {
  const node=document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key==='class') node.className=value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2),value);
    else if (value!==false && value!=null) node.setAttribute(key,value===true?'':value);
  }
  for (const child of visibleChildren(children)) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
function button(label, name, fn, cls='icon-button',attrs={}) {
  return el('button',{class:cls,'aria-label':label,title:label,...attrs,onclick:()=>Promise.resolve().then(fn).catch(showError)},name&&icon(name),cls.includes('icon-button')?null:label);
}
const counted=(n,forms)=>`${n} ${forms[n%10===1&&n%100!==11?0:n%10>=2&&n%10<=4&&!(n%100>=12&&n%100<=14)?1:2]}`;
const votesCount=n=>counted(n,['оценка','оценки','оценок']);
const count=n=>`${n} ${n%10===1&&n%100!==11?'песня':n%10>=2&&n%10<=4&&!(n%100>=12&&n%100<=14)?'песни':'песен'}`;
const time=n=>`${Math.floor((Number(n)||0)/60)}:${String(Math.floor((Number(n)||0)%60)).padStart(2,'0')}`;
const bytes=n=>n>=1024**3?(n/1024**3).toFixed(1)+' ГБ':n>=1024**2?(n/1024**2).toFixed(0)+' МБ':Math.round(n/1024)+' КБ';
const duration=n=>n>=3600?`${Math.floor(n/3600)} ч ${Math.round(n%3600/60)} мин`:`${Math.round(n/60)} мин`;
const normalize=s=>s.toLocaleLowerCase('ru').replaceAll('ё','е');
let data={tracks:[],playlists:[]}, tracks=new Map(), saved=new Map(), queue=new Queue();
let appAccess=null,personalPlaylists=new PersonalPlaylists();
const canEdit=()=>appAccess?.role==='admin';
const personalKey=()=> 'personal-playlists-'+(Number.isSafeInteger(appAccess?.userId)&&appAccess.userId>0?appAccess.userId:'owner');
let catalogReady=false,catalogError=null;
let queueDragging=false;
let queueForecast=null;
let view='home', selectedPlaylist=null, query='', shown=70, online=native?true:navigator.onLine, loggedIn=false, editing=null, installPrompt=null;
let generation=0, objectURL=null, loadedEntry=null, restoreEntry=null, toastTimer, lastPersist=0, restoredTime=0;
let nativeRevision=0,nativePlaybackEpoch=0,lastNativeQueue='',lastNativeSnapshot='',nativeReady=false,lastFiles='';
const remixInfoCache=new WeakMap();
function remixInfo(track){
  if(!track)return null;
  let value=remixInfoCache.get(track);
  if(!value||value.duration!==track.duration||value.start!==track.rangeStart||value.end!==track.rangeEnd||value.v4!==track.djV4||value.v3!==track.djV3){
    value={duration:track.duration,start:track.rangeStart,end:track.rangeEnd,v4:track.djV4,v3:track.djV3,cues:new Map(),anchors:computeRemixAnchors(track),acoustic:computeRemixAcousticAnchors(track)};remixInfoCache.set(track,value);
  }
  return value;
}
const remixAnchors=track=>remixInfo(track)?.anchors||null,remixAcousticAnchors=track=>remixInfo(track)?.acoustic||null;
function remixCues(track,desired=12){const info=remixInfo(track);if(!info)return [];if(!info.cues.has(desired)){if(info.cues.size>=8)info.cues.delete(info.cues.keys().next().value);info.cues.set(desired,computeRemixCues(track,desired));}return info.cues.get(desired);}
const discovery={query:'',job:null,recent:[],busy:false,error:null,listenJob:null};
let discoveryTimer=null,discoveryReading=false,discoveryRevision=0;
let libraryRevision='',libraryRefreshing=false;
let rangeOverrides={},rangeSyncing=false,rangeAdvancing=false;
let playbackPreferences=normalisePlayback(null),playlistPlayback={},latestRelease=null,lastUpdateCheck=0;
const clientVersion=native?.version?.()||36;
const djSupported=!native||clientVersion>=17;
const preferencesFor=id=>normalisePlayback(id!=null?playlistPlayback[id]||(id===900000021?{...playbackPreferences,mode:'remix'}:playbackPreferences):playbackPreferences);
const activePreferences=()=>preferencesFor(queue.playlistId);
const djMode=p=>djSupported&&['dj','remix'].includes(p.mode);
const remixMode=p=>djSupported&&clientVersion>=19&&p.mode==='remix';
const hasDj=track=>remixMode(preferencesFor(view==='playlist'?selectedPlaylist:queue.playlistId))?remixCues(track,preferencesFor(view==='playlist'?selectedPlaylist:queue.playlistId).remixDurationSeconds).length>0:!!djCue(track);
const progressFor=(ids,preferences=activePreferences())=>{const stats=djProgress(ids,tracks);if(remixMode(preferences))stats.ready=ids.filter(id=>remixCues(tracks.get(id),preferences.remixDurationSeconds).length).length;return stats;};
const playingTrack=()=>playbackTrack(tracks.get(queue.current?.trackId),queue.current,djMode(activePreferences()));
let renderedLocation=null,serverUnavailable=false,nativeConnected=null,djOnlyViews={},lastDjPriority='';
let listOptions={},voteOverrides={},reviewPeople={},voteSyncing=false,activeReviewTrack=null,reviewTab='all',reviewing=false;
const ratingOf=track=>reviewRating(track,voteOverrides[track.md5]);
const withLocalRange=track=>rangeOverrides[track.id]?.pending?{...track,rangeStart:rangeOverrides[track.id].start,rangeEnd:rangeOverrides[track.id].end}:track;
async function syncLibrary(revision=null){
  if(libraryRefreshing||document.hidden||editing||!loggedIn)return;
  libraryRefreshing=true;
  try{await flushRanges();await flushVotes();playlistChanges.flush();const next=revision||(await api('library-version')).revision;if(next!==libraryRevision){const generation=playlistChanges.generation;if(await applyData(await api('catalog'),generation))libraryRevision=next;}}
  catch{}finally{libraryRefreshing=false;}
}
const audio=native?new NativeAudio():$('audio'), main=$('main');
function nativeTrack(t){if(!t)return t;const {id,md5,title,artist,duration,bytes,format,rangeStart,rangeEnd,loudness}=t;return {id,md5,title,artist,duration,bytes,format,rangeStart,rangeEnd,loudness};}
function playerSnapshot(light=false){
  const snapshot=queue.snapshot();
  if(light&&native&&native.version()>=21)return {...snapshot,entries:snapshot.entries.map(entry=>{const {remixAnchors,remixAcousticAnchors,remixPool,remixCues,...rest}=entry;return rest;})};
  return native&&native.version()>=20?{...snapshot,entries:packRemixEntries(snapshot.entries)}:snapshot;
}
function restoreRemixMetadata(entries){
  const previous=new Map(queue.entries.map(entry=>[entry.id,entry]));
  return entries.map(entry=>{
    const old=previous.get(entry.id),restored={...entry};
    for(const field of ['remixAnchors','remixAcousticAnchors','remixPool','remixCues'])if(!restored[field]&&old?.trackId===entry.trackId&&old[field])restored[field]=old[field];
    if(remixMode(activePreferences())&&!restored.remixAnchors&&!restored.remixPool&&!restored.remixCues){
      const track=tracks.get(entry.trackId);
      if(track?.djV4?.anchors)restored.remixAnchors=track.djV4.anchors;
      if(track?.djV4?.acousticAnchors)restored.remixAcousticAnchors=track.djV4.acousticAnchors;
      if(!restored.remixAnchors&&!restored.remixAcousticAnchors)restored.remixCues=remixCues(track,activePreferences().remixDurationSeconds);
    }
    return restored;
  });
}
function nativeQueue(){const preferences=activePreferences();return {...playerSnapshot(),uiRevision:nativeRevision,...(native.version()>=33?{queueProtocol:1,playbackEpoch:nativePlaybackEpoch}:{}),djEnabled:djMode(preferences),djRemix:remixMode(preferences),remixDurationSeconds:preferences.remixDurationSeconds,remixMaxVisits:preferences.remixMaxVisits,remixLookahead:preferences.remixLookahead,djAudition:!!preferences.audition&&preferences.mode==='dj'&&djSupported&&native.version()>=19,transitionMs:preferences.mode==='smooth'?preferences.seconds*1000:0,tracks:Object.fromEntries([...new Set(queue.entries.map(x=>x.trackId))].map(id=>[id,nativeTrack(tracks.get(id))])),time:loadedEntry===queue.currentId?(audio.currentTime||0):0};}
if(native)audio.queueFactory=nativeQueue;
async function refreshNativeMetadata(){
  const preferences=activePreferences(),prepared=packRemixEntries(planRemix(queue.entries,tracks,{pin:queue.current,nextPin:queue.entries[queue.index+1],remixDurationSeconds:preferences.remixDurationSeconds,remixMaxVisits:preferences.remixMaxVisits,allowTimed:true}));
  const entries=prepared.map(({trackId,remixAnchors,remixAcousticAnchors,remixPool,remixCues})=>({trackId,remixAnchors,remixAcousticAnchors,remixPool,remixCues}));
  await command('metadata',{metadata:{entries,tracks:Object.fromEntries([...new Set(queue.entries.map(e=>e.trackId))].map(id=>[id,nativeTrack(tracks.get(id))]))}});
}
let metadataRefreshTimer=null;
function scheduleNativeMetadata(){
  clearTimeout(metadataRefreshTimer);
  metadataRefreshTimer=setTimeout(()=>{if(scrollBusy||listTouching){scheduleNativeMetadata();return;}refreshNativeMetadata().catch(showError);},200);
}

function toast(message) { clearTimeout(toastTimer); $('toast').textContent=message; $('toast').hidden=false; toastTimer=setTimeout(()=>$('toast').hidden=true,4200); }
function showError(error) { toast(error?.name==='QuotaExceededError'?'На устройстве не хватает места. Удали ненужные скачанные песни.':error?.message||'Не получилось. Попробуй ещё раз.'); }
async function api(path, body) {
  let response;
  try { response=native?await nativeFetch(path,body):await fetch('/api/'+path,body===undefined?{cache:'no-store',headers:{'X-AIDJ-Version':'36'}}:{method:'POST',headers:{'Content-Type':'application/json','X-AIDJ-App':'1','X-AIDJ-Version':'36'},body:JSON.stringify(body)}); }
  catch { serverUnavailable=true;if(!native)online=navigator.onLine;connection();throw new Error('Не удалось связаться с сервером. Скачанные песни доступны без сети.'); }
  const result=await response.json();
  serverUnavailable=false;online=true;connection();
  if (!response.ok) { if (response.status===401||response.status===403){loggedIn=false;connection();} const error=new Error(result.error||'Не удалось выполнить запрос.');error.status=response.status;throw error; }
  if(['admin','listener'].includes(result.access?.role)){appAccess=result.access;storage.putMeta('access',appAccess).catch(()=>{});}
  online=true; loggedIn=true; connection(); return result;
}
function connection() {
  const text=!online?'Офлайн':serverUnavailable?'Нет связи с сервером':playlistChanges.pending.length?(loggedIn?'Сохраняю…':'Подключи приложение'):'';
  $('connection').textContent=text;$('connection').classList.toggle('offline',serverUnavailable||!online);
}
function displayCatalog(value) {
  if(!appAccess)appAccess=value.access||{role:'admin'}; // Legacy owner-only cached catalogs.
  const before=queue.current&&tracks.get(queue.current.trackId);
  const queuedLevels=new Map(queue.entries.map(entry=>[entry.trackId,JSON.stringify(tracks.get(entry.trackId)?.loudness)]));
  data={...value,tracks:value.tracks.map(withLocalRange),playlists:value.playlists.filter(p=>!p.djReview)};
  data.playlists.push(...personalPlaylists.items);
  catalogReady=true;catalogError=null;
  if(canEdit()&&native&&native.version()>=21){
    const reviewIds=data.tracks.filter(track=>track.djV4&&(track.djV4.needsReview||track.djV4.reviewRecommended)).map(track=>track.id);
    if(reviewIds.length)data.playlists.push({id:900000021,name:'DJ — проверить переходы',trackIds:reviewIds,djReview:true});
  }
  tracks=new Map(data.tracks.map(x=>[x.id,x]));
  const levelsChanged=native&&nativeReady&&[...queuedLevels].some(([id,level])=>level!==JSON.stringify(tracks.get(id)?.loudness));
  if(native&&native.version()>=33&&nativeReady){scheduleNativeMetadata();}
  else if(djMode(activePreferences())){const current=queue.current;if(current?.dj?.start>0&&!current.dj.entryVerified&&!(current.dj.acoustic===true&&current.dj.needsReview===true&&current.dj.quality==='acoustic')&&!(current.remix&&timedCue(current.dj)))current.dj={...current.dj,start:0};queue.entries=remixMode(activePreferences())?planRemix(queue.entries,tracks,{pin:current,nextPin:queue.entries[queue.index+1],remixDurationSeconds:activePreferences().remixDurationSeconds,remixMaxVisits:activePreferences().remixMaxVisits,allowTimed:clientVersion>=25}):planDj(queue.entries,tracks,activePreferences().length).map(e=>e.id===current?.id?current:e);if(nativeReady)persist();}
  render(true);refreshRatingBars();connection();
  const summary=$('dj-ready-summary');if(summary){const p=data.playlists.find(p=>String(p.id)===summary.dataset.playlist),ids=p?.trackIds||queue.entries.map(e=>e.trackId),stats=progressFor(ids,preferencesFor(p?.id??queue.playlistId));summary.textContent=`Подготовлены DJ-фрагменты: ${stats.ready} из ${stats.total}.`;}
  if($('dj-now-label'))$('dj-now-label').textContent=djCurrentCaption();if($('dj-global-work'))$('dj-global-work').textContent=djWorkText();
  if(activeReviewTrack&&$('sheet').open){const track=tracks.get(activeReviewTrack);if(track)paintReviewDetails(track);}
  const after=queue.current&&tracks.get(queue.current.trackId);
  if(after&&!(native&&native.version()>=33)&&(levelsChanged||before&&(after.rangeStart!==before.rangeStart||after.rangeEnd!==before.rangeEnd)))updatePlayerRange().catch(showError);
}
const playlistChanges=new PlaylistChanges({
  save:pending=>storage.putMeta('playlist-changes',pending),
  cache:value=>storage.putMeta('catalog',value),
  send:body=>{if(!canEdit())throw new Error('Общие плейлисты доступны только для прослушивания.');return api('edit',body);},refresh:()=>api('catalog'),changed:displayCatalog,
  canSend:()=>loggedIn&&navigator.onLine,
  failed:(error,item)=>{
    const title=tracks.get(item.trackId)?.title||'Песня';
    toast((item.action==='remove'?'Не удалось убрать «':'Не удалось добавить «')+title+'»: '+error.message);
  },
});
async function applyData(value,generation=playlistChanges.generation) {return playlistChanges.accept(value,generation);}
async function cacheCatalog(){await playlistChanges.update(()=>storage.putMeta('catalog',playlistChanges.base));}
async function updatePlayerRange(){
  const track=queue.current&&tracks.get(queue.current.trackId);if(!track)return;
  if(native&&nativeReady){nativeRevision=Math.max(Date.now(),nativeRevision+1);await command('queue',{queue:nativeQueue()});}
  else if(!native){const part=segment(track);if(audio.currentTime<part.start||audio.currentTime>=part.end)audio.currentTime=part.start;}
}
async function flushRanges(){
  if(rangeSyncing||!loggedIn||!canEdit())return;rangeSyncing=true;
  try{for(const [id,value] of Object.entries(rangeOverrides))if(value.pending){await api('range',{trackId:id,start:value.start,end:value.end});if(rangeOverrides[id]===value)rangeOverrides[id]={...value,pending:false};await storage.putMeta('playback-ranges',rangeOverrides);}}
  catch{}finally{rangeSyncing=false;}
}
async function savePlaybackRange(track,part){
  if(!canEdit())throw new Error('Общую разметку может менять только владелец.');
  const latest=tracks.get(track.id);if(!latest)return;
  latest.rangeStart=part.start;latest.rangeEnd=part.end;rangeOverrides[track.id]={start:part.start,end:part.end,pending:true};
  const original=playlistChanges.base.tracks.find(t=>t.id===track.id);if(original){original.rangeStart=part.start;original.rangeEnd=part.end;}
  await storage.putMeta('playback-ranges',rangeOverrides);await cacheCatalog();await updatePlayerRange();
  if($('range-label'))$('range-label').textContent='Фрагмент · '+formatTime(part.start)+'–'+formatTime(part.end);
  flushRanges();
}
async function refresh(silent=false) {
  const generation=playlistChanges.generation;
  try { await applyData(await api('catalog'),generation);playlistChanges.flush();if(!silent)toast('Библиотека обновлена'); }
  catch(error) { if(!catalogReady){catalogError=error;render();}else if(!silent)showError(error); }
}
async function edit(action,extra={}) {
  if(!canEdit()||Number(extra.playlistId)<0){
    personalPlaylists.mutate(action,extra,new Set(tracks.keys()));
    await storage.putMeta(personalKey(),personalPlaylists.items);displayCatalog(playlistChanges.project());return;
  }
  if(action==='add'||action==='remove')return playlistChanges.enqueue(action,extra);
  await playlistChanges.flush();
  if(playlistChanges.pending.length)throw new Error('Изменения ещё сохраняются. Попробуй чуть позже.');
  const p=data.playlists.find(x=>x.id===extra.playlistId);
  const generation=playlistChanges.generation;
  try { await applyData(await api('edit',{action,opId:crypto.randomUUID(),revision:p?.revision,...extra}),generation); }
  catch(error) { if (online) await refresh(true); throw error; }
}
function navigate(next,playlist=null) { view=next;selectedPlaylist=playlist;query='';editing=null;shown=70;render();window.scrollTo(0,0);if(next==='discover')refreshDiscovery().catch(showError); }
function cover(title, variant=1, cls='') {
  const art=el('div',{class:`cover cover-${Math.abs(variant)%4+1} ${cls}`},el('span',{class:'cover-label'},'AI DJ / COLLECTION'),el('span',{class:'cover-letter'},title.trim().slice(0,1).toUpperCase()||'♪'));
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 300 300');
  svg.innerHTML='<circle cx="150" cy="150" r="112"/><circle cx="150" cy="150" r="74"/><circle cx="150" cy="150" r="27"/><path d="M0 90h300M0 210h300M90 0v300M210 0v300M0 0l300 300M0 300 300 0"/>';
  art.prepend(svg);return art;
}
function searchBox() {
  const input=el('input',{id:'search',type:'search',autocapitalize:'none',autocorrect:'off',autocomplete:'off',spellcheck:'false',enterkeyhint:'search',placeholder:selectedPlaylist?'Найти в плейлисте':'Песня или исполнитель',value:query,'aria-label':'Поиск музыки',oninput:e=>{query=e.target.value;shown=70;render();}});
  const clear=button('Очистить поиск','close',()=>{query='';render();$('search')?.focus();});clear.hidden=!query;
  return el('label',{class:'search'},icon('search'),input,clear);
}
function empty(title,description,action=null) {return el('div',{class:'empty'},icon('music'),el('h2',{},title),el('p',{},description),action);}
function art(track) { const variant=1+(track.artist.codePointAt(0)||0)%4;return el('span',{class:'track-art art-'+variant},track.artist.slice(0,1).toUpperCase()||'♪'); }
function isSaved(track) {return saved.has(track.md5);}
function filtered(ids) {
  const list=ids.map(id=>tracks.get(id)).filter(Boolean).filter(t=>!query||normalize(query).trim().split(/\s+/).every(word=>normalize(t.title+' '+t.artist).includes(word)));
  return editing?list:selectRated(djOnlyViews[optionsKey()]?list.filter(hasDj):list,optionsForView(),voteOverrides,data.review||{});
}
function available(ids) {return (native?nativeConnected!==false:online)?ids:ids.filter(id=>tracks.has(id)&&isSaved(tracks.get(id)));}
async function startPlaylist(ids,start=0,shuffle=false) {
  if(!native)audio.pause();
  queueForecast=null;
  reviewing=false;
  const wanted=ids[start], usable=available(ids);
  if(native&&!shuffle&&wanted&&!usable.includes(wanted))usable.unshift(wanted);
  if(!usable.length)throw new Error('Сначала скачай песни на устройство.');
  if(!native&&!online&&!usable.includes(wanted)&&!shuffle)throw new Error('Эта песня ещё не скачана.');
  queue.start(usable,Math.max(0,usable.indexOf(wanted)),shuffle);queue.playlistId=view==='playlist'?selectedPlaylist:null;
  if(djMode(activePreferences())){queue.entries=remixMode(activePreferences())?planRemix(queue.entries,tracks,{remixDurationSeconds:activePreferences().remixDurationSeconds,remixMaxVisits:activePreferences().remixMaxVisits,allowTimed:clientVersion>=25}):planDj(queue.entries,tracks,activePreferences().length,{reorder:shuffle});queue.currentId=shuffle?queue.entries[0]?.id:queue.currentId;lastDjPriority='';}
  if(!native)await persist(); await loadCurrent(true,true); render();
}
function entryCaption(track,entry,position=null){
  const interval=part=>time(Math.round(part.start))+'–'+time(Math.round(part.end));
  if(remixMode(activePreferences())){
    const scheduled=queueForecast?.segments.filter(s=>s.entryId===entry.id)||[];
    const labels=scheduled.map(s=>s.position+' · '+interval(s));
    if(position==null)labels.unshift(interval(segment(playbackTrack(track,entry,true))));
    return labels.length?labels:['—'];
  }
  const part=entry.dj&&djMode(activePreferences())&&!entry.djFull?segment(playbackTrack(track,entry,true)):
    position==null||entry.djFull||!djMode(activePreferences())||entry.remixFallback==='full-source'?segment(track):null;
  return [(position!=null?position+' · ':'')+(part?interval(part):'—')];
}
function readQueueForecast(player){
  const plan=player?.remixPreview;
  if(!remixMode(activePreferences())||!plan||plan.currentId!==queue.currentId||plan.uiRevision!==nativeRevision||!Array.isArray(plan.segments)||plan.segments.length>10)return null;
  if(native.version()>=33&&plan.playbackEpoch!==nativePlaybackEpoch)return null;
  const entries=new Map(queue.entries.map(e=>[e.id,e]));
  for(const [index,s] of plan.segments.entries()){
    const e=entries.get(s?.entryId),track=e&&tracks.get(e.trackId);
    if(!s||s.position!==index+1||!track||s.trackId!==e.trackId||!Number.isFinite(s.start)||!Number.isFinite(s.end)||s.end<=s.start)return null;
    const part=segment(track);if(s.start<part.start-.001||s.end>part.end+.001)return null;
  }
  return plan;
}
function songRow(track,index,ids,context={}) {
  const playing=queue.current?.trackId===track.id;
  const preferences=preferencesFor(view==='playlist'?selectedPlaylist:queue.playlistId);
  const graphs=!context.entry&&remixMode(preferences)?[remixAnchors(track),remixAcousticAnchors(track)].filter(Boolean):[];
  const remixLabel=graphs.length?`Ремикс · входы: ${new Set(graphs.flatMap(g=>g.entries.map(row=>Math.round(row[0]*1000)))).size} · выходы: ${new Set(graphs.flatMap(g=>g.exits.map(row=>Math.round(row[0]*1000)))).size}`:track.djStatus==='complete'?'Ремикс · примерный переход':null;
  const lead=context.number?el('span',{class:'song-number'},playing?'♪':index+1):art(track);
  const row=el('div',{class:'song-row'+(playing?' playing':'')},
    el('button',{class:'song-main','aria-label':track.artist+' — '+track.title,onclick:()=>{
      if(context.entry){queueForecast=null;queue.select(context.entry.id);loadCurrent(true,true).catch(showError);render();}
      else startPlaylist(ids,index).catch(showError);
    }},lead,el('span',{class:'song-text'},el('strong',{},track.title),el('span',{},track.artist),!context.entry&&ratingBadges(track),!context.entry&&remixMode(preferences)?remixLabel&&el('span',{class:'dj-ready'},remixLabel):!context.entry&&hasDj(track)?el('span',{class:'dj-ready','aria-label':'DJ-фрагмент готов'},'DJ · '+time(djCue(track).end-djCue(track).start)):null,!context.entry&&track.djStatus==='working'?el('span',{class:'dj-working'},'DJ · готовится'):null)),
    el('span',{class:'duration'},time(track.duration)));
  if(context.entry){row.classList.add('queue-row');row.append(el('span',{class:'queue-status'},...entryCaption(track,context.entry,context.queuePosition).map(label=>el('span',{class:'queue-step'},label))));}
  if(editing&&context.playlist) {
    for(const [direction,delta] of [['up',-1],['down',1]]) row.append(button(delta<0?'Выше':'Ниже',direction,()=>{
      const at=editing.ids.indexOf(track.id),to=at+delta;
      if(to>=0&&to<editing.ids.length){[editing.ids[at],editing.ids[to]]=[editing.ids[to],editing.ids[at]];render();}
    },'icon-button small'));
  } else if(context.entry) {
    row.append(button('Убрать из очереди','close',()=>removeQueue(context.entry.id),'icon-button small'));
  } else {
    row.append(button(isSaved(track)?'Скачано на устройство':'Скачать',isSaved(track)?'check':'download',()=>isSaved(track)?songMenu(track,context):download([track]),'icon-button small'+(isSaved(track)?' saved-icon':'')));
    row.append(button('Действия с песней','more',()=>songMenu(track,context),'icon-button more'));
  }
  return row;
}
function trackList(list,context={}) {
  if(!list.length&&djOnlyViews[optionsKey()])return empty('Нет подходящих DJ-фрагментов','Подготовка идёт на сервере. Также проверь поиск и фильтры оценок.');
  if(!list.length)return empty(query?'Ничего не найдено':'Нет подходящих песен',query?'Попробуй другое название.':'Измени фильтры или добавь песни в плейлист.');
  const ids=list.map(t=>t.id),container=el('div',{}),rows=el('div',{class:'song-list'});
  for(const [index,track] of list.slice(0,shown).entries())rows.append(songRow(track,index,ids,context));
  container.append(rows);
  if(list.length>shown)container.append(button(`Показать ещё · осталось ${list.length-shown}`,null,()=>{shown+=100;render();},'button full'));
  return container;
}
function heading(title,sub,extra=null) { return el('div',{class:'page-heading'},el('div',{},el('span',{class:'eyebrow'},'МОЯ КОЛЛЕКЦИЯ'),el('h1',{},title),sub&&el('p',{class:'subtitle'},sub)),extra); }
function home() {
  const fragment=document.createDocumentFragment();
  fragment.append(heading('Моя музыка',`${count(data.tracks.length)} · с Яндекс Диска`,button('Создать плейлист','plus',()=>newPlaylist())));
  fragment.append(searchBox());
  if(canEdit())fragment.append(button('Найти новую песню','search',()=>{discovery.query=query;navigate('discover');},'button discover-entry'));
  if(canEdit())fragment.append(button('Оценки и предложения',null,()=>navigate('reviews'),'button reviews-entry'));
  if(!query) {
    const grid=el('div',{class:'playlist-grid'});
    for(const p of data.playlists)grid.append(el('button',{class:'playlist-card',onclick:()=>navigate('playlist',p.id)},cover(p.name,p.id-1),el('strong',{},p.name),el('span',{class:'subtitle'},(p.personal?'Личный · ':p.combined?'Объединение · ':'')+count(p.trackIds.length))));
    grid.append(el('button',{class:'playlist-add',onclick:()=>newPlaylist()},icon('plus'),el('span',{},'Новый плейлист')));fragment.append(grid);
    fragment.append(el('div',{class:'section-heading'},el('h2',{},'Все треки'),el('span',{},count(data.tracks.length))));
  }
  fragment.append(listTools(data.tracks.map(x=>x.id)),trackList(filtered(data.tracks.map(x=>x.id))));return fragment;
}
function playlistPage() {
  const p=data.playlists.find(x=>x.id===selectedPlaylist);
  if(!p){selectedPlaylist=null;return home();}
  const ids=editing?.ids||p.trackIds, visible=filtered(ids).map(t=>t.id),total=ids.reduce((sum,id)=>sum+(tracks.get(id)?.duration||0),0);
  const fragment=document.createDocumentFragment();fragment.append(button('Плейлисты','back',()=>navigate('home'),'back'));
  const actions=el('div',{class:'featured-actions'},button('Слушать','play',()=>startPlaylist(visible),'button primary'),button('Шафл','shuffle',()=>startPlaylist(visible,0,true),'button'),button('Скачать','download',()=>downloadPlaylist(p),'icon-button'),button('Действия с плейлистом','more',()=>playlistMenu(p),'icon-button'));
  fragment.append(el('section',{class:'featured'},cover(p.name,p.id-1),el('div',{class:'featured-info'},el('span',{class:'eyebrow'},'ПЛЕЙЛИСТ'),el('h1',{},p.name),el('div',{class:'subtitle'},`${count(ids.length)} · ${duration(total)}`)),actions));
  if(p.combined)fragment.append(el('p',{class:'subtitle combined-sources'},'Источники: '+(p.sourcePlaylistIds||[]).map(id=>data.playlists.find(s=>s.id===id)?.name||'Недоступный плейлист').join(' + ')+'. Список обновляется автоматически.'));
  if(p.djReview)fragment.append(el('p',{class:'subtitle'},'Сложные песни после дополнительного анализа. «По звуку, проверить» — границы менее уверенные. Открой «Выбрать фрагмент», чтобы проверить и поправить конкретную песню.'));
  if(editing) {
    fragment.append(el('div',{class:'toolbar'},el('span',{class:'subtitle'},'Порядок песен'),button('Отмена',null,()=>{editing=null;render();},'button'),button('Сохранить',null,async()=>{
      const draft=editing; await edit('reorder',{playlistId:p.id,trackIds:draft.ids,revision:draft.revision});editing=null;render();toast('Порядок сохранён');
    },'button primary')));
    fragment.append(el('p',{class:'edit-hint'},'Перемещай песни стрелками, затем сохрани порядок.'));
  } else fragment.append(button('Воспроизведение · '+modeName(preferencesFor(p.id)),'music',()=>playbackOptions(p.id),'button full playback-mode'),searchBox(),listTools(ids));
  fragment.append(trackList(filtered(ids),{playlist:p,number:true}));return fragment;
}
function savedPage() {
  const list=filtered(data.tracks.filter(isSaved).map(t=>t.id));
  const total=[...saved.values()].reduce((sum,x)=>sum+x.bytes,0);
  const fragment=document.createDocumentFragment();fragment.append(heading('На устройстве',`${count(saved.size)} · ${bytes(total)}`));
  if(saved.size){fragment.append(el('div',{class:'toolbar'},button('Слушать','play',()=>startPlaylist(list.map(t=>t.id)),'button primary'),button('Шафл','shuffle',()=>startPlaylist(list.map(t=>t.id),0,true),'button')),searchBox(),listTools(data.tracks.filter(isSaved).map(t=>t.id)),trackList(list));}
  else fragment.append(empty('Музыка без сети','Нажми на значок загрузки рядом с песней или скачай плейлист целиком.',button('К плейлистам','folder',()=>navigate('home'),'button primary')));
  return fragment;
}
function queuePage() {
  const fragment=document.createDocumentFragment();fragment.append(heading('Очередь',count(queue.entries.length)));
  if(!queue.entries.length){fragment.append(empty('Что послушаем?','Выбери песню или включи плейлист.',button('К музыке','music',()=>navigate('home'),'button primary')));return fragment;}
  fragment.append(el('p',{class:'dj-work-status'},djWorkText()),el('div',{class:'toolbar'},button('Шафл','shuffle',()=>{queueForecast=null;queue.toggleShuffle();render();persist();},'button'+(queue.shuffle?' primary':'')),!remixMode(activePreferences())&&button(queue.repeat==='one'?'Повтор песни':queue.repeat==='all'?'Повтор очереди':'Без повтора','repeat',()=>{cycleRepeat();render();},'button')));
  const current=queue.current;
  if(current&&tracks.has(current.trackId))fragment.append(el('section',{class:'queue-current'},el('span',{class:'eyebrow'},'СЕЙЧАС ИГРАЕТ'),songRow(tracks.get(current.trackId),0,[],{entry:current})));
  const rest=queue.entries.slice(Math.max(0,queue.index+1));
  const positions=new Map(rest.map((entry,index)=>[entry.id,index+1]));
  const words=normalize(query).trim().split(/\s+/).filter(Boolean),matches=rest.filter(entry=>{const track=tracks.get(entry.trackId);return track&&words.every(word=>normalize(track.title+' '+track.artist).includes(word));});
  fragment.append(el('div',{class:'queue-tools'},el('h2',{},'Далее · '+rest.length),button('Очистить',null,()=>{queueForecast=null;queue.clearUpcoming();render();persist();},'text-button')));
  const search=searchBox();search.querySelector('input').setAttribute('placeholder','Найти в очереди');search.querySelector('input').setAttribute('aria-label','Поиск в очереди');fragment.append(search);
  if(words.length)fragment.append(el('p',{class:'subtitle',role:'status'},`Найдено: ${matches.length} из ${rest.length}`));
  const rows=el('div',{class:'song-list queue-sortable'});
  for(const entry of matches.slice(0,shown)){
    const track=tracks.get(entry.trackId);if(!track)continue;
    const row=songRow(track,0,[],{entry,queuePosition:positions.get(entry.id)});
    row.dataset.queueEntry=entry.id;
    row.querySelector('.song-main').setAttribute('aria-describedby','queue-drag-help');
    row.insertBefore(button('Поставить следующей','top',()=>moveUpcoming(entry.id,queue.entries[queue.index+1]?.id),'icon-button small queue-next',{disabled:entry.id===rest[0]?.id}),row.querySelector('[aria-label="Убрать из очереди"]'));
    rows.append(row);
  }
  fragment.append(el('p',{id:'queue-drag-help',class:'subtitle queue-drag-help'},'Зажми для переноса. ⇈ — следующей. План на 10 фрагментов, он может меняться.'));
  sortableQueue(rows,{onMove:moveUpcoming,onActive:active=>{queueDragging=active;if(!active)queueMicrotask(()=>{if(view==='queue'&&!queueDragging)render();});}});
  fragment.append(rows);
  if(!matches.length&&words.length)fragment.append(empty('Ничего не найдено','Попробуй название песни или исполнителя.'));
  if(matches.length>shown)fragment.append(button('Показать ещё',null,()=>{shown+=100;render();},'button full'));
  return fragment;
}
function moveUpcoming(id,beforeId=null){
  if(queue.moveUpcoming(id,beforeId)){queueForecast=null;render();persist();}
}
function readyDiscovery(track){
  const card=el('section',{class:'discovery-ready'},el('span',{class:'eyebrow'},'ПРОСЛУШАТЬ И ВЫБРАТЬ'),el('h2',{},track.title),el('p',{},track.artist+' · '+time(track.duration)),
    el('div',{class:'discovery-actions'},button('Слушать','play',()=>startPlaylist([track.id]),'button primary'),
      button(isSaved(track)?'Скачано':'Скачать',isSaved(track)?'check':'download',()=>isSaved(track)?toast('Песня уже на устройстве'):download([track]),'button')),
    button('Добавить в плейлисты','plus',()=>choosePlaylists(track),'button full'),button('Оценки и разметка',null,()=>reviewDetails(track),'button full'));
  const membership=data.playlists.filter(p=>p.trackIds.includes(track.id));
  if(membership.length){card.append(el('p',{},'В плейлистах: '+membership.map(p=>p.name).join(', ')),button('Действия с песней','more',()=>songMenu(track),'button full'));}
  else card.append(el('p',{class:'subtitle'},'В плейлисты пока не добавлена.'));
  return card;
}
function discoverPage(){
  const fragment=document.createDocumentFragment();fragment.append(heading('Найти музыку','Исполнитель, название или ссылка на песню'));
  const input=el('input',{id:'discover-query',type:'search',autocapitalize:'none',autocorrect:'off',autocomplete:'off',spellcheck:'false',enterkeyhint:'search',value:discovery.query,maxlength:512,placeholder:'Например, София Ротару — Белая зима','aria-label':'Найти новую песню',oninput:e=>{discovery.query=e.target.value;}});
  const submit=button('Найти','search',()=>beginSearch(),'button primary');submit.disabled=discovery.busy;
  input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!discovery.busy){e.preventDefault();beginSearch().catch(showError);}});
  fragment.append(el('div',{class:'discover-form'},el('label',{class:'search'},icon('search'),input),submit));
  const job=discovery.job,pending=job&&['queued','processing'].includes(job.status);
  if(discovery.error)fragment.append(el('p',{class:'discovery-error',role:'alert'},discovery.error),button('Обновить','repeat',()=>refreshDiscovery(),'button'));
  if(job){
    fragment.append(el('p',{class:'subtitle'},job.query));
    if(pending)fragment.append(el('div',{class:'discovery-status',role:'status'},el('div',{class:'spinner'}),el('span',{},job.stage)));
    if(job.status==='failed')fragment.append(el('p',{role:'alert'},job.error||'Не получилось. Попробуй другую версию.'),button(job.kind==='clip'?'Повторить подготовку':'Повторить поиск','search',()=>job.clip?sendDiscovery({action:'clip',...job.clip},true):beginSearch(),'button'));
    const track=job.trackId&&tracks.get(job.trackId);
    if(track)fragment.append(readyDiscovery(track));
    if(job.candidates?.length){
      fragment.append(el('h2',{},job.status==='awaiting_choice'?'Выбери другую версию':'Найденные записи'));
      const list=el('div',{class:'discovery-results'});
      for(const candidate of job.candidates){
        const label=(candidate.inLibrary?'Уже в библиотеке · ':'')+(candidate.source==='yandex'?'Яндекс Музыка':'YouTube Music');
        const listen=button('Послушать','play',()=>prepareDiscovery(job.id,candidate.index),'button');listen.disabled=discovery.busy||pending;
        list.append(el('article',{class:'discovery-result'},el('div',{class:'song-text'},el('strong',{},candidate.title+(candidate.version?' ('+candidate.version+')':'')),el('span',{},candidate.artist),el('span',{},label+(candidate.duration?' · '+time(candidate.duration):''))),listen));
      }
      fragment.append(list);
    }
    if(!pending&&!job.youtubeOnly)fragment.append(button('Поискать в YouTube Music','search',()=>beginSearch(true),'button full'));
  }else fragment.append(el('p',{},'Найди запись и послушай её. Потом выбери плейлисты, в которые хочешь её добавить.'));
  const recent=discovery.recent.filter(x=>x.id!==job?.id);
  if(recent.length){fragment.append(el('h2',{class:'discovery-history-title'},'Последние запросы'));for(const item of recent)fragment.append(button(item.query+' · '+item.stage,null,async()=>{discoveryRevision++;discovery.query=item.query;discovery.listenJob=null;await acceptDiscovery(item);scheduleDiscovery();},'discovery-history'));}
  return fragment;
}
function scheduleDiscovery(){clearTimeout(discoveryTimer);if(view==='discover'&&!discovery.busy&&discovery.job&&['queued','processing'].includes(discovery.job.status))discoveryTimer=setTimeout(()=>refreshDiscovery().catch(()=>{}),1800);}
async function acceptDiscovery(job){
  discovery.job=job;await storage.putMeta('discovery',{query:discovery.query,id:job.id});
  if(job.trackId&&!tracks.has(job.trackId))await refresh(true);
  if(view==='discover')render();
  if(job.trackId&&discovery.listenJob===job.id&&view==='discover'&&!document.hidden){discovery.listenJob=null;await startPlaylist([job.trackId]);}
}
async function refreshDiscovery(){
  if(discoveryReading||discovery.busy)return;discoveryReading=true;const revision=discoveryRevision;
  try{
    if(document.hidden)return;
    const result=await api('discover');if(revision!==discoveryRevision)return;discovery.recent=result.jobs;discovery.error=null;
    let job=discovery.job;
    if(job)job=result.jobs.find(x=>x.id===job.id)||await api('discover/'+job.id);
    else job=result.jobs[0];
    if(revision!==discoveryRevision)return;if(job)await acceptDiscovery(job);else if(view==='discover')render();
  }catch(error){discovery.error=error.message;if(view==='discover')render();}
  finally{discoveryReading=false;scheduleDiscovery();}
}
async function sendDiscovery(body,listen=false){
  if(discovery.busy)return;
  if(!online)throw new Error('Для поиска нужно подключение к сети.');
  discoveryRevision++;clearTimeout(discoveryTimer);discovery.busy=true;discovery.error=null;
  try{
    const pending=await storage.meta('discovery-request');
    const request=pending&&JSON.stringify(pending.body)===JSON.stringify(body)?pending:{body,opId:crypto.randomUUID()};
    await storage.putMeta('discovery-request',request);if(view==='discover')render();
    const job=await api('discover',{...body,opId:request.opId});await storage.putMeta('discovery-request',null);
    discovery.listenJob=listen?job.id:null;await acceptDiscovery(job);
    discovery.recent=[job,...discovery.recent.filter(x=>x.id!==job.id)].slice(0,12);
  }catch(error){discovery.error=error.message;throw error;}
  finally{discovery.busy=false;if(view==='discover')render();scheduleDiscovery();}
}
async function beginSearch(youtubeOnly=false){
  const value=discovery.query.trim();if(!value)throw new Error('Введи исполнителя и название или ссылку.');
  discovery.query=value;await sendDiscovery({action:'search',query:value,youtubeOnly});
}
async function prepareDiscovery(jobId,index){await sendDiscovery({action:'prepare',jobId,index},true);}
let scrollBusy=false,listTouching=false,scrollTimer=null,renderPending=false;
function flushBackgroundRender(){if(renderPending&&!scrollBusy&&!listTouching){renderPending=false;render();}}
document.addEventListener('scroll',()=>{scrollBusy=true;clearTimeout(scrollTimer);scrollTimer=setTimeout(()=>{scrollBusy=false;flushBackgroundRender();},180);},{passive:true});
document.addEventListener('touchstart',e=>{if(e.target.closest('main'))listTouching=true;},{passive:true,capture:true});
for(const kind of ['touchend','touchcancel'])document.addEventListener(kind,()=>{listTouching=false;setTimeout(flushBackgroundRender,0);},{passive:true,capture:true});
function render(background=false) {
  if(!canEdit()&&['discover','reviews'].includes(view))view='home';
  document.querySelector('.bottom-nav [data-view="discover"]').hidden=!canEdit();
  if(background&&(scrollBusy||listTouching)&&['queue','playlist','home','saved'].includes(view)){renderPending=true;renderMini();return;}
  if(queueDragging&&view==='queue'){renderMini();return;}
  if(!catalogReady){
    if(catalogError?.status===401||catalogError?.status===403)main.replaceChildren(empty('Твоя музыка — здесь',native?'Для подключения нажми «Открыть бота», затем «Подключить приложение».':'Для первого входа открой приложение из бота.',el('a',{class:'button primary',href:'https://t.me/aidj_codex_music_bot?start='+(native?'android':'apk')},'Открыть бота')));
    else if(catalogError)main.replaceChildren(empty('Не удалось открыть музыку',catalogError.message,button('Повторить',null,initialize,'button primary')));
    else main.replaceChildren(el('div',{class:'loading',role:'status'},el('div',{class:'spinner'}),'Загружаю библиотеку…'));
    return;
  }
  const active=document.activeElement,focused=['search','discover-query'].includes(active?.id);
  const tree=view==='playlist'?playlistPage():view==='saved'?savedPage():view==='queue'?queuePage():view==='discover'?discoverPage():view==='reviews'?reviewsPage():home();
  const location=view+':'+(selectedPlaylist??'');
  const topLevel=(node,parent)=>{while(node?.parentNode&&node.parentNode!==parent)node=node.parentNode;return node?.parentNode===parent?node:null;};
  const keep=focused?topLevel(active,main):null,fresh=focused?topLevel(tree.querySelector('#'+active.id),tree):null;
  if(keep&&fresh&&renderedLocation===location){
    // Keep the focused field connected: replacing/refocusing it restarts Android IME.
    const wanted=active.id==='search'?query:discovery.query;if(active.value!==wanted)active.value=wanted;
    const oldButton=keep.querySelector('button'),newButton=fresh.querySelector('button');
    if(oldButton&&newButton){oldButton.hidden=newButton.hidden;oldButton.disabled=newButton.disabled;}
    for(const node of [...main.childNodes])if(node!==keep)node.remove();
    let after=false;
    for(const node of [...tree.childNodes]){if(node===fresh){after=true;continue;}if(after)main.append(node);else main.insertBefore(node,keep);}
  }else main.replaceChildren(tree);
  renderedLocation=location;
  for(const node of document.querySelectorAll('.bottom-nav button'))node.classList.toggle('active',node.dataset.view===(view==='playlist'?'home':view));
  renderMini();
}
function requestDjPriority(){
  if(!canEdit()||!djMode(activePreferences())||!loggedIn||!online)return;
  const ids=queue.entries.slice(Math.max(0,queue.index),Math.max(0,queue.index)+10).map(e=>e.trackId),key=JSON.stringify(ids);
  if(key===lastDjPriority)return;lastDjPriority=key;
  api('dj',{trackIds:ids,nextTrackIds:ids}).catch(()=>{if(lastDjPriority===key)lastDjPriority='';});
}
function djWorkText(){
  if(!online||serverUnavailable)return 'Нет связи с сервером · статус DJ не обновляется';
  const state=data.djService?.state,job=data.djWork;
  if(['stopped','not-started'].includes(state))return 'Подготовка DJ не запущена';
  if(state==='stale')return 'Давно нет обновлений подготовки DJ';
  if(job)return 'DJ готовит: '+job.artist+' — '+job.title;
  return state==='idle'?'DJ ждёт следующую песню':'Ожидаю данные о подготовке DJ';
}
async function persist() {
  try{
    if(!native&&remixMode(activePreferences())){arrangeWebRemix(queue,tracks,activePreferences());const plan=forecastWebRemix(queue,tracks,activePreferences());if(JSON.stringify(plan)!==JSON.stringify(queueForecast)){queueForecast=plan;render(true);}}
    requestDjPriority();
    if(native&&nativeReady){const key=JSON.stringify(playerSnapshot(true));if(key!==lastNativeQueue){lastNativeQueue=key;nativeRevision=Math.max(Date.now(),nativeRevision+1);await command('queue',{queue:nativeQueue()});}}
    await storage.putMeta('player',{...playerSnapshot(true),time:audio.currentTime||restoredTime||0});
  }catch{}
}
async function loadCurrent(play,replaceQueue=false) {
  if(play&&!native&&document.hidden&&djMode(activePreferences())){audio.pause();return;}
  const token=++generation, entry=queue.current, original=entry&&tracks.get(entry.trackId),track=native?original:playbackTrack(original,entry,djMode(activePreferences()));
  if(!track){audio.pause();audio.removeAttribute('src');audio.load();loadedEntry=null;renderMini();return;}
  if(native){nativeRevision=Math.max(Date.now(),nativeRevision+1);lastNativeQueue=JSON.stringify(playerSnapshot(true));await command(play?'play':'queue',{queue:nativeQueue(),replaceQueue});loadedEntry=queue.currentId;renderMini();if($('full-player').open)renderFull();return;}
  if(loadedEntry===entry.id&&audio.src){const part=segment(track);if(audio.currentTime<part.start||audio.currentTime>=part.end)audio.currentTime=part.start;applyBrowserVolume();if(play)await audio.play();return;}
  if(entry.id!==restoreEntry)restoredTime=0;
  audio.pause();
  const content=await storage.blob(track.md5);
  if(token!==generation)return;
  if(!content&&!online)throw new Error('Эта песня не скачана. Выбери трек из раздела «На устройстве».');
  audio.pause();const previousURL=objectURL;
  objectURL=content?URL.createObjectURL(content):null;
  audio.src=objectURL||'/api/audio/'+track.id;loadedEntry=entry.id;
  audio.playbackRate=djMode(activePreferences())?(entry.dj?.rate||1):1;
  applyBrowserVolume();
  if(previousURL)URL.revokeObjectURL(previousURL);
  if('mediaSession' in navigator){navigator.mediaSession.metadata=new MediaMetadata({title:track.title,artist:track.artist,album:'AI DJ',artwork:[{src:new URL('/icon.svg',location.href).href,sizes:'any',type:'image/svg+xml'}]});}
  renderMini();if($('full-player').open)renderFull();
  if(play)try{await audio.play();}catch{toast('Нажми ▶, чтобы включить музыку.');}
}
async function togglePlay() {
  if(!queue.current){toast('Выбери песню или плейлист');return;}
  if(audio.paused){await loadCurrent(true);}else audio.pause();
}
async function next(ended=false) {
  if(native&&djSupported&&!ended){await command('next');return;}
  if(!native&&remixMode(activePreferences())){
    if(finishWebRemix(queue,tracks,activePreferences(),!ended))await loadCurrent(true);else{audio.pause();loadedEntry=null;}
    await persist();render();return;
  }
  if(queue.next(ended)){if(ended&&queue.repeat==='one')audio.currentTime=segment(tracks.get(queue.current?.trackId)).start;await loadCurrent(true);}
  else{audio.pause();audio.currentTime=0;}
  await persist();render();
}
async function previous() {if(native&&djSupported){await command('previous');return;}const part=segment(playingTrack()||tracks.get(queue.current?.trackId));if(remixMode(activePreferences())||audio.currentTime-part.start>3){audio.currentTime=part.start;return;}queue.previous();await loadCurrent(true);await persist();render();}
function cycleRepeat(){if(remixMode(activePreferences()))return;queue.repeat=queue.repeat==='off'?'all':queue.repeat==='all'?'one':'off';persist();syncControls();}
async function removeQueue(id) {queueForecast=null;const before=queue.currentId,playing=!audio.paused;queue.remove(id);if(before!==queue.currentId)await loadCurrent(playing,true);await persist();render();}
function renderMini() {
  const track=queue.current&&tracks.get(queue.current.trackId);$('mini-player').hidden=!track;
  if(!track)return;
  $('mini-title').textContent=track.title;$('mini-artist').textContent=track.artist;$('mini-art').textContent=track.artist.slice(0,1)||'♪';syncControls();
}
function syncControls(){
  const playing=!audio.paused;
  for(const id of ['mini-play','full-play'])if($(id)){$(id).replaceChildren(icon(playing?'pause':'play'));$(id).setAttribute('aria-label',playing?'Пауза':'Воспроизвести');}
  if($('full-shuffle')){$('full-shuffle').classList.toggle('selected',queue.shuffle);$('full-shuffle').setAttribute('aria-pressed',String(queue.shuffle));}
  if($('full-repeat')){$('full-repeat').classList.toggle('selected',queue.repeat!=='off');$('full-repeat').title=queue.repeat==='one'?'Повтор песни':queue.repeat==='all'?'Повтор очереди':'Без повтора';}
  if(!native&&'mediaSession' in navigator)navigator.mediaSession.playbackState=playing?'playing':'paused';
}
function progress(){
  const total=Number.isFinite(audio.duration)?audio.duration:(queue.current&&tracks.get(queue.current.trackId)?.duration)||0;
  const current=audio.currentTime||0;
  $('mini-progress-fill').style.width=(total?Math.min(100,current/total*100):0)+'%';
  if($('seek')&&document.activeElement!==$('seek')){$('seek').max=String(total);$('seek').value=String(current);}
  if($('elapsed'))$('elapsed').textContent=time(current);if($('total'))$('total').textContent=time(total);
  if('mediaSession' in navigator&&navigator.mediaSession.setPositionState&&total>0&&current<=total)try{navigator.mediaSession.setPositionState({duration:total,playbackRate:audio.playbackRate,position:current});}catch{}
}
function djCurrentCaption(){
  const track=tracks.get(queue.current?.trackId),entry=queue.current;
  if(!entry||!track)return '';
  if(entry?.djFull)return 'DJ · песня целиком';
  return (activePreferences().audition&&activePreferences().mode==='dj'?'Только переходы · ':'')+entryCaption(track,entry)[0];
}
function renderFull(){
  const track=queue.current&&tracks.get(queue.current.trackId);if(!track)return;
  const content=$('full-content');
  const top=el('div',{class:'player-top'},button('Свернуть плеер','down',()=>$('full-player').close()),el('span',{},'СЕЙЧАС ИГРАЕТ'),button('Действия с песней','more',()=>songMenu(track)));
  const seek=el('input',{id:'seek',class:'seek',type:'range',min:0,max:track.duration,value:audio.currentTime||0,step:.25,'aria-label':'Позиция воспроизведения',oninput:e=>{$('elapsed').textContent=time(e.target.value);},onchange:e=>{audio.currentTime=Number(e.target.value);persist();}});
  const play=button('Воспроизвести','play',togglePlay,'icon-button play-large');play.id='full-play';
  const repeat=button('Повтор','repeat',cycleRepeat,'icon-button');repeat.id='full-repeat';repeat.hidden=remixMode(activePreferences());
  const part=segment(playingTrack()||track),range=canEdit()?button('Выбрать фрагмент','edit',()=>rangeEditor(track),'button full'):el('div',{class:'button full'});range.id='range-label';range.textContent='Фрагмент · '+formatTime(part.start)+'–'+formatTime(part.end);
  content.replaceChildren(top,el('div',{class:'player-title-row'},el('span',{class:'song-text'},el('strong',{},track.title),el('span',{},track.artist)),button(isSaved(track)?'Скачано':'Скачать',isSaved(track)?'check':'download',()=>download([track]),'icon-button'+(isSaved(track)?' saved-icon':''))),seek,
    el('div',{class:'times'},el('span',{id:'elapsed'},time(audio.currentTime)),el('span',{id:'total'},time(track.duration))),
    el('div',{class:'transport'},el('span',{'aria-hidden':'true'}),button('Предыдущая песня','previous',previous),play,button('Следующая песня','next',()=>next()),repeat),
    ratingControls(track),...(reviewing?[button('Закончить разметку',null,()=>{reviewing=false;renderFull();},'text-button')]:[]),range,
    ...(djMode(activePreferences())?[el('div',{class:'dj-now'},el('span',{id:'dj-now-label'},djCurrentCaption()),queue.current.dj&&!queue.current.djFull?button('Доиграть полностью',null,async()=>{queue.current.djFull=true;queueForecast=null;await persist();await updatePlayerRange();renderFull();},'button full'):null)]:[]),
    el('div',{class:'player-footer'},el('span',{},isSaved(track)?'На устройстве':'Яндекс Диск'),button('Очередь','queue',()=>{$('full-player').close();navigate('queue');},'button')));
  syncControls();progress();
}
function openFull(){if(!queue.current)return;renderFull();$('full-player').showModal();}
function rangeEditor(track){
  let part=segment(tracks.get(track.id)||track),drag=null;
  const ruler=el('div',{class:'range-ruler'}),window=el('div',{class:'range-window',tabindex:0,role:'slider','aria-label':'Переместить фрагмент'});
  const left=el('button',{class:'range-handle start',role:'slider','aria-label':'Начало фрагмента'}),right=el('button',{class:'range-handle end',role:'slider','aria-label':'Конец фрагмента'});
  ruler.append(window,left,right);
  const from=el('input',{type:'text','aria-label':'Начало, время',value:formatTime(part.start)}),to=el('input',{type:'text','aria-label':'Конец, время',value:formatTime(part.end)});
  const summary=el('p',{class:'range-summary',role:'status'});
  const paint=()=>{
    ruler.style.setProperty('--start',100*part.start/part.total+'%');ruler.style.setProperty('--end',100*part.end/part.total+'%');
    from.value=formatTime(part.start);to.value=formatTime(part.end);summary.textContent=formatTime(part.start)+'–'+formatTime(part.end)+' · '+formatTime(part.end-part.start);
    for(const [node,value] of [[left,part.start],[right,part.end],[window,part.start]]){node.setAttribute('aria-valuemin','0');node.setAttribute('aria-valuemax',String(part.total));node.setAttribute('aria-valuenow',String(value));node.setAttribute('aria-valuetext',formatTime(value));}
  };
  const commit=()=>savePlaybackRange(track,part).catch(showError);
  for(const [node,kind] of [[left,'start'],[right,'end'],[window,'move']]){
    node.addEventListener('pointerdown',event=>{event.preventDefault();node.setPointerCapture(event.pointerId);drag={kind,x:event.clientX,part:{...part},rect:ruler.getBoundingClientRect()};});
    node.addEventListener('pointermove',event=>{if(!drag)return;const delta=(event.clientX-drag.x)/drag.rect.width*part.total;part=drag.kind==='move'?moveSegment(drag.part,delta):changeBoundary(drag.part,drag.kind,drag.part[drag.kind]+delta);paint();});
    node.addEventListener('pointerup',()=>{if(drag){drag=null;commit();}});
    node.addEventListener('pointercancel',()=>{if(drag)part=drag.part;drag=null;paint();});
    node.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const delta=(event.key==='ArrowLeft'?-1:1)*(event.shiftKey?10:1);part=kind==='move'?moveSegment(part,delta):changeBoundary(part,kind,part[kind]+delta);paint();commit();});
  }
  const typed=()=>{try{const start=parseTime(from.value),end=parseTime(to.value);if(start<0||end-start<.5||end>part.total+.05)throw new Error('Укажи фрагмент внутри песни, не короче 0,5 секунды.');part={...part,start,end:Math.min(end,part.total)};paint();commit();}catch(error){showError(error);paint();}};
  for(const input of [from,to]){input.addEventListener('blur',event=>{if(event.relatedTarget!==from&&event.relatedTarget!==to)typed();});input.addEventListener('keydown',event=>{if(event.key==='Enter')typed();});}
  showSheet('Фрагмент песни',el('p',{},track.artist+' — '+track.title),el('p',{},'Двигай границы или перетаскивай выделенный участок. Выбор сохраняется автоматически.'),ruler,
    el('div',{class:'range-fields'},el('label',{},'От',from),el('label',{},'До',to)),summary,
    button('Слушать фрагмент','play',async()=>{await savePlaybackRange(track,part);await startPlaylist([track.id]);},'button primary full'),
    button('Вся песня','repeat',async()=>{part={start:0,end:part.total,total:part.total};paint();await savePlaybackRange(track,part);},'button full'),
    button('Сохранить фрагмент отдельно','download',async()=>{if(!online)throw new Error('Для сохранения отдельного файла нужно подключение к сети.');await sendDiscovery({action:'clip',trackId:track.id,start:part.start,end:part.end},true);$('sheet').close();if($('full-player').open)$('full-player').close();navigate('discover');},'button full'));
  paint();
}
function optionsKey(){return view==='playlist'?'playlist:'+selectedPlaylist:view==='reviews'?'reviews:'+reviewTab:view;}
function optionsForView(){return listSettings(listOptions[optionsKey()]||(view==='reviews'&&reviewTab==='all'?{sort:'dislikes'}:{}));}
function filterCount(ids){return `После фильтров: ${filtered(ids).length} из ${ids.length}`;}
const voteFilterNames={like:'Лайк',dislike:'Дизлайк',neutral:'Всё равно',unrated:'Нет оценки'};
const voteFilterSymbols={like:'👍',dislike:'👎',neutral:'😐',unrated:'○'};
function voterLabel(id){return data.review?.voters?.find(p=>p.id===id)?.label||'Участник недоступен';}
function listTools(ids){
  const value=optionsForView(),names={default:'По порядку плейлиста',likes:'Больше лайков',dislikes:'Больше дизлайков',score:'По балансу оценок',votes:'Больше оценок'};
  const dj=progressFor(ids),onlyDj=!!djOnlyViews[optionsKey()];
  const filters=onlyDj||value.ratingMode==='priority'||value.noDislikes||value.state!=='all'||value.minScore!==null||value.people.length>0;
  const people=el('div',{class:'active-people-filters'});
  if(value.ratingMode==='priority')people.append(button('По важности · '+value.priorityOutcomes.map(v=>voteFilterSymbols[v]).join(' / '),'settings',()=>listOptionsSheet(ids),'person-filter-chip'));
  for(const rule of value.ratingMode==='priority'?[]:value.people)people.append(button(voterLabel(rule.id)+' · '+rule.votes.map(v=>voteFilterSymbols[v]).join(' / '),'close',async()=>{
    const next=optionsForView();next.people=next.people.filter(p=>p.id!==rule.id);listOptions[optionsKey()]=next;shown=70;render();await storage.putMeta('review-list-options',listOptions);
  },'person-filter-chip',{'aria-label':'Убрать условие: '+voterLabel(rule.id)}));
  return el('div',{class:'list-tools'},button(names[value.sort]+(filters?' · фильтры включены':''),'settings',()=>listOptionsSheet(ids),'button list-options-button'),
    value.ratingMode!=='priority'&&value.people.length>1?el('p',{class:'people-filter-caption'},value.peopleMode==='all'?'Совпадают все условия по людям':'Достаточно одного условия по людям'):null,
    el('div',{class:'dj-library-tools'},button('Только DJ','music',async()=>{djOnlyViews[optionsKey()]=!onlyDj;shown=70;await storage.putMeta('dj-list-filters',djOnlyViews);render();},'button dj-only'+(onlyDj?' primary':''),{'aria-pressed':String(onlyDj)}),
      el('span',{class:'dj-library-progress','aria-live':'polite'},`Фрагменты: ${dj.ready} · обработано ${dj.processed} из ${dj.total}`)),
    el('p',{class:'dj-work-status','aria-live':'polite'},djWorkText()),
    people,el('p',{class:'filter-count',role:'status','aria-live':'polite'},filterCount(ids)));
}
function priorityEditor(value,roster,save){
  const outcomes=el('div',{class:'priority-outcomes',role:'group','aria-label':'Какие итоговые оценки оставить'});
  const list=el('div',{class:'priority-list','aria-label':'Люди по важности'});
  const status=el('p',{class:'visually-hidden',role:'status','aria-live':'polite'});
  const search=el('input',{type:'search',placeholder:'Имя или @ник',autocapitalize:'none',autocomplete:'off','aria-label':'Добавить участника по важности'});
  const additions=el('div',{class:'priority-additions'});
  const person=id=>roster.find(p=>p.id===id)||{id,label:'Участник недоступен'};
  for(const [kind,label] of [['like','Лайк'],['dislike','Дизлайк'],['unrated','Без итоговой']])outcomes.append(el('button',{type:'button',class:'voter-choice '+kind,'data-priority-outcome':kind,'aria-label':'Итог: '+label,onclick:()=>{
    value.priorityOutcomes=value.priorityOutcomes.includes(kind)?value.priorityOutcomes.filter(v=>v!==kind):[...value.priorityOutcomes,kind];save().catch(showError);
  }},el('span',{'aria-hidden':'true'},voteFilterSymbols[kind]),el('small',{},label)));
  const paintAdditions=()=>{
    const wanted=normalize(search.value).trim(),available=roster.filter(p=>!p.missing&&!(value.priorityOrder||[]).includes(p.id)&&(!wanted||normalize(p.label+' '+(p.username||'')).includes(wanted)));
    additions.replaceChildren(...available.map(p=>button(p.label+(p.username?' · @'+p.username:''),'plus',()=>{value.priorityOrder=[...(value.priorityOrder||[]),p.id];save().catch(showError);},'button full priority-add-person',{'aria-label':'Добавить по важности: '+p.label})));
    if(!available.length)additions.append(el('p',{class:'subtitle'},wanted?'Никого не нашлось':'Все участники уже в списке'));
  };
  search.addEventListener('input',paintAdditions);
  const update=()=>{
    for(const node of outcomes.children)node.setAttribute('aria-pressed',String(value.priorityOutcomes.includes(node.dataset.priorityOutcome)));
    list.replaceChildren(...(value.priorityOrder||[]).map((id,index)=>{
      const p=person(id);
      return el('div',{class:'priority-row','data-priority-id':id},
        button('Переместить: '+p.label,'grip',()=>{},'icon-button priority-handle',{'aria-describedby':'priority-drag-help'}),
        el('span',{class:'priority-rank','aria-label':'Приоритет '+(index+1)},index+1),
        el('span',{class:'voter-filter-name'},el('strong',{},p.label),p.username?el('small',{},'@'+p.username):null),
        button('Убрать из приоритета: '+p.label,'close',()=>{value.priorityOrder=value.priorityOrder.filter(v=>v!==id);save().catch(showError);},'icon-button small'));
    }));
    if(!list.children.length)list.append(el('p',{class:'subtitle'},'Добавь людей ниже, чтобы определять итоговую оценку.'));
    paintAdditions();
  };
  const section=el('section',{class:'priority-editor'},el('h3',{},'Оставить по итоговой оценке'),outcomes,
    el('p',{class:'subtitle'},'Первая оценка 👍 или 👎 сверху определяет итог. «Всё равно» и отсутствие оценки пропускаются.'),
    el('h3',{},'Порядок важности'),el('p',{id:'priority-drag-help',class:'subtitle'},'Сверху — самый важный. Перетаскивай за точки. С клавиатуры — стрелками вверх и вниз.'),list,
    el('details',{class:'priority-add'},el('summary',{},'Добавить человека'),el('label',{class:'voter-search'},icon('search'),search),additions),status);
  sortablePeople(list,{scrollElement:$('sheet'),onMove:(from,to)=>{value.priorityOrder=movePerson(value.priorityOrder||[],from,to);save().catch(showError);},onAnnounce:(id,to)=>{status.textContent=person(id).label+': позиция '+(to+1);}});
  return {section,update};
}
function listOptionsSheet(ids){
  const key=optionsKey(),value=optionsForView();
  let priority=null,normalSection=null,general=null;
  const filterModes=el('div',{class:'people-filter-modes rating-mode-toggle',role:'group','aria-label':'Режим фильтрации'});
  const counter=el('p',{class:'filter-count',role:'status','aria-live':'polite'},filterCount(ids));
  const roster=[...(data.review?.voters||[])];
  for(const rule of value.people)if(!roster.some(p=>p.id===rule.id))roster.push({id:rule.id,label:'Участник недоступен',missing:true});
  const peopleList=el('div',{class:'voter-filter-list'}),modes=el('div',{class:'people-filter-modes',role:'group','aria-label':'Как сочетать условия по людям'});
  const updateControls=()=>{
    for(const node of filterModes.children)node.setAttribute('aria-pressed',String(node.dataset.ratingMode===value.ratingMode));
    if(normalSection)normalSection.hidden=value.ratingMode==='priority';
    if(general)general.hidden=value.ratingMode==='priority';
    if(priority){priority.section.hidden=value.ratingMode!=='priority';priority.update();}
    for(const node of modes.querySelectorAll('button'))node.setAttribute('aria-pressed',String(node.dataset.mode===value.peopleMode));
    for(const card of peopleList.querySelectorAll('.voter-filter-card')){
      const votes=value.people.find(p=>p.id===card.dataset.voterId)?.votes||[];
      card.classList.toggle('has-condition',votes.length>0);
      card.querySelector('.clear-voter').hidden=!votes.length;
      for(const node of card.querySelectorAll('[data-vote-kind]'))node.setAttribute('aria-pressed',String(votes.includes(node.dataset.voteKind)));
    }
  };
  const save=async()=>{listOptions[key]=listSettings(value);counter.textContent=filterCount(ids);updateControls();shown=70;render();await storage.putMeta('review-list-options',listOptions);};
  for(const [id,label] of [['all','Все условия'],['any','Любое условие']])modes.append(el('button',{type:'button','data-mode':id,'aria-pressed':String(value.peopleMode===id),onclick:()=>{value.peopleMode=id;save().catch(showError);}},label));
  for(const [id,label] of [['people','По условиям'],['priority','По важности']])filterModes.append(el('button',{type:'button','data-rating-mode':id,onclick:()=>{
    value.ratingMode=id;if(id==='priority'&&value.priorityOrder===null)value.priorityOrder=roster.filter(p=>!p.missing).map(p=>p.id);save().catch(showError);
  }},label));
  const search=el('input',{type:'search',placeholder:'Имя или @ник',autocapitalize:'none',autocorrect:'off',autocomplete:'off',spellcheck:'false','aria-label':'Найти участника'});
  const paintPeople=()=>{
    const wanted=normalize(search.value).trim();peopleList.replaceChildren();
    for(const person of roster.filter(p=>!wanted||normalize(p.label+' '+(p.username||'')).includes(wanted))){
      const clear=button('Убрать условие: '+person.label,'close',()=>{value.people=value.people.filter(p=>p.id!==person.id);save().catch(showError);},'icon-button small clear-voter');
      const header=el('div',{class:'voter-filter-heading'},el('span',{class:'voter-avatar','aria-hidden':'true'},person.label.slice(0,1).toUpperCase()),
        el('span',{class:'voter-filter-name'},el('strong',{},person.label),person.username?el('small',{},'@'+person.username):null),clear);
      const choices=el('div',{class:'voter-filter-choices',role:'group','aria-label':'Оценки: '+person.label});
      for(const kind of voteKinds)choices.append(el('button',{type:'button',class:'voter-choice '+kind,'data-vote-kind':kind,'aria-label':person.label+': '+voteFilterNames[kind],disabled:!!person.missing,onclick:()=>{
        const previous=value.people.find(p=>p.id===person.id)?.votes||[];
        const votes=previous.includes(kind)?previous.filter(v=>v!==kind):[...previous,kind];
        value.people=value.people.filter(p=>p.id!==person.id);if(votes.length)value.people.push({id:person.id,votes});
        save().catch(showError);
      }},el('span',{'aria-hidden':'true'},voteFilterSymbols[kind]),el('small',{},voteFilterNames[kind])));
      peopleList.append(el('div',{class:'voter-filter-card','data-voter-id':person.id},header,choices));
    }
    if(!peopleList.childNodes.length)peopleList.append(el('p',{class:'subtitle'},roster.length?'Никого не нашлось. Попробуй другое имя.':'Участники появятся, когда начнут размечать песни.'));
    updateControls();
  };
  search.addEventListener('input',paintPeople);
  const sorting=el('select',{'aria-label':'Сортировка песен'},...Object.entries({default:'Порядок плейлиста / добавления',likes:'Сначала больше лайков',dislikes:'Сначала больше дизлайков',score:'Баланс: лайки минус дизлайки',votes:'Сначала больше оценок'}).map(([id,label])=>el('option',{value:id,selected:value.sort===id},label)));
  sorting.addEventListener('change',()=>{value.sort=sorting.value;save().catch(showError);});
  const state=el('select',{'aria-label':'Фильтр по оценкам'},...Object.entries({all:'Все песни',rated:'Уже есть оценки',unrated:'Никто ещё не оценил','mine-missing':'Я ещё не оценил'}).map(([id,label])=>el('option',{value:id,selected:value.state===id},label)));
  state.addEventListener('change',()=>{value.state=state.value;save().catch(showError);});
  const no=el('input',{type:'checkbox',checked:value.noDislikes,'aria-label':'Без дизлайков',onchange:e=>{value.noDislikes=e.target.checked;save().catch(showError);}});
  const minimum=el('input',{type:'number',min:-10000,max:10000,step:1,value:value.minScore??'',placeholder:'Без ограничения','aria-label':'Минимум: лайки минус дизлайки',onchange:e=>{value.minScore=e.target.value===''?null:Number(e.target.value);save().catch(showError);}});
  normalSection=el('section',{class:'people-filters'},el('div',{class:'people-filters-title'},el('h3',{},'Кто как оценил'),button('Сбросить людей',null,()=>{value.people=[];save().catch(showError);},'text-button')),modes,
      el('p',{class:'subtitle'},'У одного человека можно выбрать несколько оценок.'),
      !online?el('p',{class:'subtitle'},'Без сети — по последним сохранённым оценкам.'):null,
      !data.review?.votes?el('p',{class:'subtitle'},'Подключись к сети, чтобы загрузить оценки участников.'):null,
      el('label',{class:'voter-search'},icon('search'),search),peopleList);
  general=el('details',{class:'general-filters',open:value.noDislikes||value.state!=='all'||value.minScore!==null},el('summary',{},'Общие фильтры'),
      el('label',{class:'review-field'},'Оценки',state),el('label',{class:'review-check'},no,'Без дизлайков'),el('label',{class:'review-field'},'Минимум: лайки − дизлайки',minimum));
  priority=priorityEditor(value,roster,save);
  showSheet('Фильтры песен',filterModes,el('label',{class:'review-field'},'Сортировать',sorting),normalSection,priority.section,general,
    button('Сбросить фильтры и сортировку',null,async()=>{listOptions[key]=listSettings(null);await storage.putMeta('review-list-options',listOptions);render();listOptionsSheet(ids);},'button full'),
    el('div',{class:'filter-result'},counter,el('small',{},'Фильтры сохраняются сразу'),button('К песням',null,()=>$('sheet').close(),'button primary full')));
  paintPeople();
}

function priorityBadge(track){
  const options=optionsForView();if(options.ratingMode!=='priority')return null;
  const decision=priorityRating(track,options.priorityOrder,data.review||{},voteOverrides);
  const text=!decision.known?'Нет данных об оценках':decision.value===null?'○ Без итоговой оценки':(decision.value===1?'👍':'👎')+' · '+voterLabel(decision.personId);
  return el('span',{class:'rating-badges priority-decision','aria-label':'Итог по важности: '+text},text);
}
function ratingBadges(track){
  if(optionsForView().ratingMode==='priority')return priorityBadge(track);
  const r=ratingOf(track);
  return r.total||r.proposals?el('span',{class:'rating-badges','aria-label':`Оценки: ${r.likes} лайков, ${r.dislikes} дизлайков, ${r.neutral} нейтральных`},`👍 ${r.likes}   👎 ${r.dislikes}   😐 ${r.neutral}`):null;
}
function ratingControls(track,withDetails=true){
  if(!canEdit())return el('div',{class:'rating-readonly'},ratingBadges(track));
  const r=ratingOf(track),row=el('div',{class:'rating-controls','data-track-id':track.id,'data-details':String(withDetails)});
  const votes=el('div',{class:'rating-votes'});
  for(const [value,label,aria,n] of [[1,'👍','Нравится',r.likes],[-1,'👎','Не нравится',r.dislikes],[0,'😐','Всё равно',r.neutral]]){
    votes.append(el('button',{class:'rating-vote'+(r.mine===value?' selected':''),'aria-label':aria,'aria-pressed':String(r.mine===value),onclick:()=>saveVote(track,r.mine===value?null:value).catch(showError)},label+' '+n));
  }
  row.append(votes);
  if(withDetails)row.append(button('Кто оценил',null,()=>reviewDetails(track),'text-button rating-people'));
  if(r.pending)row.append(el('span',{class:'rating-pending'},'Синхронизируется'));
  return row;
}
function refreshRatingBars(){
  for(const node of document.querySelectorAll('.rating-controls')){
    const track=tracks.get(node.dataset.trackId);if(track)node.replaceWith(ratingControls(track,node.dataset.details==='true'));
  }
}
async function saveVote(track,value){
  if(!canEdit())throw new Error('Оценки доступны владельцу.');
  voteOverrides[track.md5]={trackId:track.id,value,opId:crypto.randomUUID()};
  await storage.putMeta('review-votes',voteOverrides);render();refreshRatingBars();
  if(activeReviewTrack===track.id&&$('sheet').open)paintReviewDetails(track);
  flushVotes();
  if(reviewing&&queue.current?.trackId===track.id&&value!==null)await next();
}
async function flushVotes(){
  if(voteSyncing||!loggedIn||!navigator.onLine||!canEdit())return;voteSyncing=true;
  try{
    for(const [md5,draft] of Object.entries(voteOverrides)){
      const result=await api('reviews',{action:'vote',trackId:draft.trackId,value:draft.value,opId:draft.opId});
      for(const track of data.tracks)if(track.md5===md5)track.rating={...result.rating};
      reviewPeople[md5]=result;
      if(voteOverrides[md5]===draft)delete voteOverrides[md5];
      for(const track of playlistChanges.base.tracks)if(track.md5===md5)track.rating={...result.rating};
      await storage.putMeta('review-votes',voteOverrides);await storage.putMeta('review-people',reviewPeople);await cacheCatalog();
      render();refreshRatingBars();
      if(activeReviewTrack&&tracks.get(activeReviewTrack)?.md5===md5&&$('sheet').open)paintReviewDetails(tracks.get(activeReviewTrack));
    }
  }catch{}finally{voteSyncing=false;}
}
function paintReviewDetails(track){
  const detail=reviewPeople[track.md5],r=ratingOf(tracks.get(track.id)||track),group=el('div',{class:'review-people-list'});
  const people=(detail?.people||[]).filter(p=>!p.isMine).map(p=>({...p}));
  const self=detail?.people?.find(p=>p.isMine);
  if(r.mine!==null||self?.proposed)people.unshift({...self,label:'Ты',isMine:true,vote:r.mine,proposed:self?.proposed||false});
  for(const p of people){
    const label=p.isMine?'Ты':p.label;
    const name=p.username?el('button',{class:'text-button',onclick:()=>openLink('https://t.me/'+p.username)},label):el('span',{},label);
    group.append(el('div',{class:'review-person'},name,el('span',{class:'review-person-details'},
      el('span',{},p.vote===1?'👍 Нравится':p.vote===-1?'👎 Не нравится':p.vote===0?'😐 Всё равно':'Без оценки'),
      p.proposed?el('span',{},'Автор предложения'):null)));
  }
  const children=[el('p',{},track.artist),priorityBadge(track),el('p',{class:'subtitle'},'Для плейлиста «'+(data.review?.playlistName||'Volleyball')+'»'),ratingControls(track,false),
    el('p',{class:'subtitle'},votesCount(r.total)+(!online?' · сохранённые данные':'')),group];
  if(!people.length)children.push(el('p',{},detail?'Эту песню пока никто не оценил.':'Загружаю участников…'));
  if(detail?.similar?.some(x=>!x.exact))children.push(el('p',{class:'subtitle'},'Похожие в общем плейлисте: '+detail.similar.filter(x=>!x.exact).map(x=>x.label).join('; ')));
  if(r.proposals){
    children.push(el('p',{class:'subtitle'},'Предложили: '+r.proposals));
    const shared=data.playlists.find(p=>p.id===data.review?.playlistId);
    const present=shared?.trackIds.some(id=>id===track.id||tracks.get(id)?.md5===track.md5);
    if(!present)children.push(button('Добавить в '+(data.review?.playlistName||'Volleyball'),'plus',async()=>{
      await edit('add',{playlistId:data.review.playlistId,trackId:track.id});toast('Песня добавлена в общий плейлист');
    },'button primary full'));
    children.push(button('Снять с разметки',null,()=>reviewAction('archive',track),'button full'));
  }else if(detail&&!detail.inPlaylist)children.push(button('Предложить на разметку','plus',()=>reviewAction('propose',track),'button full'));
  children.push(button('Добавить в другие плейлисты','plus',()=>choosePlaylists(track),'button full'));
  showSheet('Оценки · '+track.title,...children);activeReviewTrack=track.id;
}
async function reviewDetails(track){
  paintReviewDetails(track);
  if(!online){if(!reviewPeople[track.md5])toast('Список участников появится при подключении к сети');return;}
  const result=await api('reviews',{action:'details',trackId:track.id});reviewPeople[track.md5]=result;await storage.putMeta('review-people',reviewPeople);
  if(activeReviewTrack===track.id&&$('sheet').open)paintReviewDetails(tracks.get(track.id)||track);
}
async function reviewAction(action,track){
  if(!online)throw new Error('Для управления предложениями нужно подключение к сети.');
  const result=await api('reviews',{action,trackId:track.id,opId:crypto.randomUUID()});reviewPeople[track.md5]=result;
  await storage.putMeta('review-people',reviewPeople);await refresh(true);if(activeReviewTrack===track.id&&$('sheet').open)paintReviewDetails(tracks.get(track.id)||track);
  toast(action==='archive'?'Предложение снято с разметки':result.alreadyInPlaylist?'Эта песня уже есть в общем плейлисте':'Песня отправлена на разметку и сохранится в Предложке');
}
function reviewsPage(){
  const info=data.review||{},fragment=document.createDocumentFragment();
  fragment.append(button('Моя музыка','back',()=>navigate('home'),'back'),heading('Оценки и предложения',`${votesCount(info.total||0)} · ${counted(info.participants||0,['участник','участника','участников'])}`));
  fragment.append(el('div',{class:'review-tabs'},...[["all","Общий плейлист"],["proposals","Предложения"]].map(([id,label])=>button(label,null,()=>{reviewTab=id;shown=70;render();},'button'+(reviewTab===id?' primary':'')))));
  fragment.append(el('div',{class:'toolbar'},button('Размечать','play',startReview,'button primary'),button('Ссылка друзьям','external',()=>openLink('https://t.me/aidj_codex_music_bot?start=review'),'button')));
  if(reviewTab==='proposals')fragment.append(el('p',{class:'subtitle'},'Открой «Кто оценил» в меню песни: там авторы предложений и добавление в общий плейлист. На телефон песни сами не скачиваются.'));
  const pool=new Set(info.poolTrackIds||[]);
  const list=data.tracks.filter(t=>reviewTab==='proposals'?ratingOf(t).proposals>0:pool.has(t.id)||ratingOf(t).total>0);
  const unique=[...new Map(list.map(t=>[t.md5,t])).values()];
  fragment.append(searchBox(),listTools(unique.map(t=>t.id)),trackList(filtered(unique.map(t=>t.id))));return fragment;
}
async function startReview(){
  const pool=new Set(data.review?.poolTrackIds||[]),list=data.tracks.filter(t=>pool.has(t.id)&&ratingOf(t).mine===null);
  const ids=reviewOrder([...new Map(list.map(t=>[t.md5,t])).values()],voteOverrides).map(t=>t.id);
  if(!ids.length){toast('Ты уже оценил все доступные песни');return;}
  await startPlaylist(ids);reviewing=true;openFull();
}

function showSheet(title,...children){activeReviewTrack=null;$('sheet-content').replaceChildren(el('div',{class:'sheet-head'},el('h2',{},title),button('Закрыть','close',()=>$('sheet').close())),...visibleChildren(children));if(!$('sheet').open)$('sheet').showModal();}
function sheetAction(label,name,fn,danger=false){return button(label,name,fn,'sheet-action'+(danger?' danger':''));}
function removeFromPlaylist(track,playlist){
  showSheet('Убрать песню?',el('p',{},track.artist+' — '+track.title),el('p',{},'Из плейлиста «'+playlist.name+'». Скачанный файл останется на устройстве.'),
    button('Убрать из плейлиста','trash',async()=>{await edit('remove',{playlistId:playlist.id,trackId:track.id});$('sheet').close();toast('Песня убрана из плейлиста');},'button danger full'));
}
function appendQueue(track,next){
  queueForecast=null;queue.append(track.id,next);
  if(djMode(activePreferences())){
    const at=Math.max(0,queue.index),current=queue.current;
    queue.entries=remixMode(activePreferences())?planRemix(queue.entries,tracks,{pin:current,nextPin:next?null:queue.entries[queue.index+1],remixDurationSeconds:activePreferences().remixDurationSeconds,remixMaxVisits:activePreferences().remixMaxVisits,allowTimed:clientVersion>=25}):[...queue.entries.slice(0,at),...planDj(queue.entries.slice(at),tracks,activePreferences().length,{pin:current}).map(e=>e.id===current?.id?current:e)];
    lastDjPriority='';
  }
  persist();$('sheet').close();toast(next?'Будет играть следующей':'Добавлено в очередь');render();
}
function songMenu(track,context={}){
  showSheet(track.title,
    el('p',{},track.artist),
    ratingControls(track),
    canEdit()&&sheetAction('Выбрать фрагмент','edit',()=>rangeEditor(track)),
    sheetAction('Воспроизведение · '+modeName(activePreferences()),'music',playbackOptions),
    sheetAction(isSaved(track)?'Удалить с устройства':'Скачать на устройство',isSaved(track)?'trash':'download',async()=>{
      $('sheet').close();if(isSaved(track)){await storage.remove(track.md5);saved.delete(track.md5);render();toast('Удалено с устройства');}else await download([track]);
    }),
    sheetAction('Слушать следующей','next',()=>appendQueue(track,true)),
    sheetAction('В конец очереди','queue',()=>appendQueue(track,false)),
    sheetAction('Добавить в плейлисты','plus',()=>choosePlaylists(track)),
    ...(context.playlist?[context.playlist]:data.playlists.filter(p=>p.trackIds.includes(track.id))).filter(p=>(canEdit()||p.personal)&&!p.djReview&&!p.combined).map(p=>sheetAction(context.playlist?'Убрать из этого плейлиста':'Убрать из «'+p.name+'»','trash',()=>removeFromPlaylist(track,p),true)));
}
function playlistMenu(p){
  if(p.personal){showSheet(p.name,el('p',{},'Личный плейлист хранится на этом устройстве.'),
    sheetAction('Воспроизведение · '+modeName(preferencesFor(p.id)),'music',()=>playbackOptions(p.id)),
    sheetAction('Изменить порядок','queue',()=>{editing={ids:[...p.trackIds],revision:null};query='';$('sheet').close();render();}),
    sheetAction('Переименовать','edit',()=>nameForm('Название личного плейлиста',p.name,async name=>{await edit('rename',{playlistId:p.id,name});$('sheet').close();})),
    sheetAction('Удалить личный плейлист','trash',async()=>{await edit('delete',{playlistId:p.id});$('sheet').close();navigate('home');},true));return;}
  if(!canEdit()){showSheet(p.name,el('p',{},'Общий плейлист · доступен для прослушивания.'),
    sheetAction('Воспроизведение · '+modeName(preferencesFor(p.id)),'music',()=>playbackOptions(p.id)),
    sheetAction('Скачать плейлист','download',()=>downloadPlaylist(p)),
    sheetAction('Создать личную копию','plus',()=>newPersonalPlaylist(p.trackIds,p.name)));return;}
  if(p.combined){
    showSheet(p.name,el('p',{},'Песни автоматически собираются из выбранных плейлистов. Одинаковые записи показаны один раз.'),
      sheetAction('Воспроизведение · '+modeName(preferencesFor(p.id)),'music',()=>playbackOptions(p.id)),
      sheetAction('Скачать плейлист','download',()=>downloadPlaylist(p)),
      sheetAction('Переименовать','edit',()=>nameForm('Название объединения',p.name,async name=>{await edit('rename-combined',{playlistId:p.id,name});$('sheet').close();toast('Название сохранено');})),
      sheetAction('Удалить объединение','trash',()=>showSheet('Удалить объединение?',el('p',{},p.name),el('p',{},'Исходные плейлисты и песни останутся.'),button('Удалить объединение',null,async()=>{await edit('delete-combined',{playlistId:p.id});$('sheet').close();navigate('home');toast('Объединение удалено');},'button full danger')),true));return;
  }
  if(p.djReview){showSheet(p.name,el('p',{},'Подборка сложных песен для прослушивания. Варианты по звуку помечены: проверь, не обрезались ли слова на входе и выходе. Состав обновляется по мере анализа.'),sheetAction('Воспроизведение · '+modeName(preferencesFor(p.id)),'music',()=>playbackOptions(p.id)),sheetAction('Скачать для проверки','download',()=>downloadPlaylist(p)));return;}
  showSheet(p.name,
    sheetAction('Скачать плейлист','download',()=>downloadPlaylist(p)),
    sheetAction('Изменить порядок','queue',async()=>{
      await playlistChanges.flush();if(playlistChanges.pending.length)throw new Error('Изменения ещё сохраняются. Попробуй чуть позже.');
      const latest=data.playlists.find(x=>x.id===p.id);editing={ids:[...latest.trackIds],revision:latest.revision};query='';$('sheet').close();render();
    }),
    sheetAction('Переименовать','edit',()=>nameForm('Название плейлиста',p.name,async name=>{await edit('rename',{playlistId:p.id,name});$('sheet').close();toast('Плейлист переименован');})),
    sheetAction('Открыть на Диске','external',()=>openLink(p.diskUrl)));
}
function nameForm(title,value,submit){
  const input=el('input',{type:'text',value,maxlength:64,placeholder:'Например, В дорогу','aria-label':'Название плейлиста',autocomplete:'off'});
  const action=button('Сохранить',null,async()=>{action.disabled=true;try{await submit(input.value.trim());}finally{action.disabled=false;}},'button primary full');
  input.addEventListener('keydown',e=>{if(e.key==='Enter')action.click();});showSheet(title,input,action);setTimeout(()=>input.focus(),100);
}
function newPlaylist(after=null){
  if(!canEdit()){newPersonalPlaylist([], '',after);return;}
  const sources=data.playlists.filter(p=>!p.djReview&&!p.combined);
  if(after||sources.length<2||data.combinedSupported!==true){nameForm('Новый плейлист','',async name=>{await edit('create',{name});$('sheet').close();toast('Плейлист создан');if(after)after();});return;}
  const selected=new Set(),input=el('input',{type:'text',maxlength:64,placeholder:'Например, Volleyball + Предложка','aria-label':'Название плейлиста',autocomplete:'off'});
  let requestDraft=null;
  const summary=el('p',{class:'subtitle',role:'status'},'Без выбранных источников получится пустой плейлист.'),checks=el('div',{class:'combined-source-list'});
  const update=()=>{
    const seen=new Set();for(const p of sources.filter(p=>selected.has(p.id)))for(const id of p.trackIds){const track=tracks.get(id);if(track)seen.add(track.md5||id);}
    summary.textContent=selected.size===1?'Выбери ещё один источник для объединения.':selected.size?count(seen.size)+' · без дублей. Новые песни появятся автоматически.':'Без выбранных источников получится пустой плейлист.';
    const label=selected.size?'Создать объединение':'Создать плейлист';submit.textContent=label;submit.setAttribute('aria-label',label);submit.disabled=selected.size===1;
  };
  for(const p of sources)checks.append(el('label',{class:'check'},el('input',{type:'checkbox','aria-label':'Источник: '+p.name,onchange:e=>{e.target.checked?selected.add(p.id):selected.delete(p.id);update();}}),el('span',{},p.name+' · '+count(p.trackIds.length))));
  const submit=button('Создать плейлист',null,async()=>{
    if(selected.size===1)throw new Error('Для объединения выбери минимум два плейлиста.');
    const name=input.value.trim();if(!name)throw new Error('Введи название плейлиста.');submit.disabled=true;
    try{
      const action=selected.size?'combine':'create',payload={name,...(selected.size?{sourcePlaylistIds:sources.filter(p=>selected.has(p.id)).map(p=>p.id)}:{})};
      const signature=JSON.stringify({action,...payload});if(requestDraft?.signature!==signature)requestDraft={signature,opId:crypto.randomUUID()};
      await edit(action,{...payload,opId:requestDraft.opId});
      const created=data.playlists.find(p=>p.name===name);$('sheet').close();toast(selected.size?'Объединение создано':'Плейлист создан');if(created)navigate('playlist',created.id);
    }finally{update();}
  },'button primary full');
  input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!submit.disabled)submit.click();});
  showSheet('Новый плейлист',input,el('p',{class:'subtitle'},'Можно объединить несколько плейлистов для прослушивания.'),checks,summary,submit);setTimeout(()=>input.focus(),100);
}
function newPersonalPlaylist(trackIds=[],name='',after=null){
  nameForm('Личный плейлист · на этом устройстве',name,async name=>{
    await edit('create',{name,trackIds});$('sheet').close();toast('Личный плейлист создан');
    if(after)after();else navigate('playlist',personalPlaylists.items.at(-1).id);
  });
}
function choosePlaylists(track){
  const selected=new Set();const checks=[];
  for(const p of data.playlists.filter(p=>(canEdit()||p.personal)&&!p.djReview&&!p.combined)){
    const present=p.trackIds.includes(track.id),input=el('input',{type:'checkbox',checked:present,disabled:present,onchange:e=>e.target.checked?selected.add(p.id):selected.delete(p.id)});
    checks.push(el('label',{class:'check'},input,el('span',{},p.name+(present?' · уже добавлена':''))));
  }
  const submit=button('Добавить',null,async()=>{
    if(!selected.size){toast('Отметь плейлисты');return;}submit.disabled=true;
    try{for(const id of selected)await edit('add',{playlistId:id,trackId:track.id});$('sheet').close();toast('Добавлено в плейлисты');}finally{submit.disabled=false;}
  },'button primary full');
  showSheet('Добавить в плейлисты',...checks,submit,sheetAction('Новый плейлист','plus',()=>newPlaylist(()=>choosePlaylists(track))));
}
const downloads=native?new NativeDownloads(()=>renderDownloads()):new storage.Downloads({
  fetchTrack:track=>fetch('/api/audio/'+track.id),existing:md5=>saved.has(md5),
  store:async(track,content)=>{await storage.save(track,content);saved.set(track.md5,{md5:track.md5,bytes:content.size,title:track.title,artist:track.artist,id:track.id});render();if($('full-player').open)renderFull();},
  changed:()=>renderDownloads(),
});
async function download(list){
  if(!native&&!online)throw new Error('Для загрузки нужно подключение к сети.');
  const missing=list.filter(t=>!isSaved(t));if(!missing.length){toast('Всё уже скачано');return;}
  try{await navigator.storage?.persist?.();}catch{}
  downloads.add(missing);$('sheet').close();
}
function downloadPlaylist(p){
  const missing=p.trackIds.map(id=>tracks.get(id)).filter(t=>t&&!isSaved(t)),total=missing.reduce((sum,t)=>sum+t.bytes,0);
  if(!missing.length){toast('Плейлист уже скачан');return;}
  showSheet('Скачать плейлист?',el('p',{},p.name),el('div',{class:'stats'},el('span',{},count(missing.length)),el('strong',{},bytes(total))),
    button('Скачать на устройство','download',()=>download(missing),'button primary full'));
}
function renderDownloads(){
  const panel=$('downloads-progress');panel.hidden=!downloads.active&&!downloads.pending.length;document.body.classList.toggle('has-downloads',!panel.hidden);
  if(panel.hidden)return;
  const error=downloads.error?.name==='QuotaExceededError'?'Не хватает места на устройстве':downloads.error?.message;
  const resuming=downloads.paused||downloads.needsRetry;
  const phase=downloads.phase&&!['Скачиваю','Загрузка приостановлена'].includes(downloads.phase)?downloads.phase:'';
  const title=downloads.needsRetry?`Не скачано: ${downloads.failedCount}`:downloads.paused?'Загрузка приостановлена':`Скачивание · ${downloads.completed} / ${downloads.total}`;
  const detail=error||(downloads.paused?'Нажми «Продолжить», чтобы возобновить загрузку.':phase||downloads.active?.title)||'Готово к продолжению';
  panel.replaceChildren(...visibleChildren([el('div',{},el('strong',{},title),el('span',{},detail)),
    downloads.bytesTotal>0?el('progress',{class:'batch-download-progress',max:100,value:downloads.percent||0,'aria-label':'Скачивание плейлиста'}):null,
    button(downloads.needsRetry?'Повторить':resuming?'Продолжить':'Пауза',null,()=>native?downloads.setPaused(!resuming):downloads.toggle(),'text-button',{disabled:downloads.controlling}),button('Отменить загрузку','close',()=>downloads.cancel(),'icon-button small')]));
}
function openLink(url){if(native)native.open(url);else if(tg?.initData)tg.openLink(url);else window.open(url,'_blank','noopener,noreferrer');}
function applyBrowserVolume(){
  if(native)return;
  const track=queue.current&&tracks.get(queue.current.trackId);if(!track){audio.volume=1;return;}
  const part=segment(playbackTrack(track,queue.current,djMode(activePreferences()))),preferences=activePreferences();
  let gain=fadeGain(audio.currentTime,part.start,part.end,preferences.mode==='smooth'?preferences.seconds:djMode(preferences)?Math.min(.5,queue.current?.dj?.fade||.3):0)*(djMode(preferences)?.7:1);
  const m=track.loudness;let correction=0;
  if(m?.method==='ebu-r128-v1'&&[m.integratedLufs,m.truePeakDbtp].every(Number.isFinite)&&m.integratedLufs>=-70&&m.integratedLufs<=0&&m.truePeakDbtp>=-60&&m.truePeakDbtp<=6)correction=Math.min(12,-16-m.integratedLufs,-1-m.truePeakDbtp);
  gain*=Math.pow(10,Math.min(0,correction)/20);
  if(Math.abs(audio.volume-gain)>.001)audio.volume=gain;
}
async function savePlaybackPreferences(value,playlistId=null){
  const oldPreferences=preferencesFor(playlistId),preferences=normalisePlayback(value);
  if(playlistId==null){playbackPreferences=preferences;await storage.putMeta('playback-preferences',preferences);}
  else {playlistPlayback[playlistId]=preferences;await storage.putMeta('playlist-playback',playlistPlayback);}
  if(queue.playlistId===playlistId){
    queueForecast=null;
    // Keep the current cue stable; settings affect the next song. Turning DJ off restores the original range.
    const current=queue.current,sameMode=oldPreferences.mode===preferences.mode,planned=remixMode(preferences)?planRemix(queue.entries,tracks,{...(sameMode?{pin:current,nextPin:queue.entries[queue.index+1]}:{}),remixDurationSeconds:preferences.remixDurationSeconds,remixMaxVisits:preferences.remixMaxVisits,remixLookahead:preferences.remixLookahead,allowTimed:clientVersion>=25}):planDj(queue.entries,tracks,preferences.length);
    queue.entries=planned.map(e=>e.id===current?.id&&djMode(preferences)&&sameMode?current:e);lastDjPriority='';
    if(native&&nativeReady&&queue.current){
      nativeRevision=Math.max(Date.now(),nativeRevision+1);
      const result=await command('queue',{queue:nativeQueue()}),live=result.player?.queue;
      // The playback transition wins, then retry only this still-current settings
      // intent against its fresh snapshot. Never retry an obsolete user action.
      if(result.queueAccepted===false&&live?.playbackEpoch===nativePlaybackEpoch&&live.uiRevision===nativeRevision&&queue.playlistId===playlistId&&JSON.stringify(preferencesFor(playlistId))===JSON.stringify(preferences)){
        const playing=queue.current;
        const options={...(sameMode?{pin:playing,nextPin:queue.entries[queue.index+1]}:{}),remixDurationSeconds:preferences.remixDurationSeconds,remixMaxVisits:preferences.remixMaxVisits,allowTimed:true};
        queue.entries=(remixMode(preferences)?planRemix(queue.entries,tracks,options):planDj(queue.entries,tracks,preferences.length)).map(e=>e.id===playing?.id&&djMode(preferences)&&sameMode?playing:e);
        nativeRevision=Math.max(Date.now(),nativeRevision+1);await command('queue',{queue:nativeQueue()});
      }
    }
    else applyBrowserVolume();
    await persist();
  }
  render();
}
function playbackOptions(playlistId=view==='playlist'?selectedPlaylist:queue.playlistId){
  const preferences=preferencesFor(playlistId),smooth=preferences.mode==='smooth';
  const choose=async mode=>{await savePlaybackPreferences({...preferencesFor(playlistId),mode},playlistId);playbackOptions(playlistId);};
  const buttons=['normal','smooth',...(djSupported?['dj']:[]),...(clientVersion>=19?['remix']:[])].map(mode=>button(modeName({mode}),preferences.mode===mode?'check':'music',()=>choose(mode),'button full'+(preferences.mode===mode?' primary':''),{'aria-pressed':String(preferences.mode===mode)}));
  const nodes=[el('div',{class:'playback-modes',role:'group','aria-label':'Режим воспроизведения'},...buttons)];
  if(djMode(preferences)){
    const p=data.playlists.find(p=>p.id===playlistId),ids=p?.trackIds||queue.entries.map(e=>e.trackId),stats=progressFor(ids,preferences),ready=stats.ready;
    nodes.push(el('p',{},remixMode(preferences)?'Фрагменты с возвратами к песням. Следующая выбирается в пределах настроенной глубины очереди. Песня уходит из очереди после выбранного числа фрагментов или раньше, если подходящие куски закончились.':'Сокращённые песни и наложение переходов. Темп близких песен подстраивается в пределах 3%. Ручные фрагменты сохраняются.'),
      !remixMode(preferences)&&el('div',{class:'dj-lengths',role:'group','aria-label':'Длительность DJ-фрагмента'},...Object.entries({short:'Покороче',normal:'Обычно',long:'Подольше'}).map(([length,label])=>button(label,preferences.length===length?'check':null,async()=>{await savePlaybackPreferences({...preferences,length},playlistId);playbackOptions(playlistId);},'button',{'aria-pressed':String(preferences.length===length)}))),
      el('p',{class:'subtitle'},remixMode(preferences)?'Длительность подбирается около выбранной. Начало и конец — по границам фраз; уже звучащий фрагмент не меняется.':'Примерно 80 / 110 / 140 секунд. Окончание фразы важнее времени.'),
      remixMode(preferences)?button('Слушать ремикс','play',async()=>{const readyIds=ids;$('sheet').close();if(playlistId!=null){view='playlist';selectedPlaylist=playlistId;}await startPlaylist(readyIds);},'button full primary'):null,
      el('p',{id:'dj-ready-summary',role:'status','data-playlist':playlistId??''},`Подготовлены DJ-фрагменты: ${ready} из ${ids.length}.`+(ready<ids.length?' Без подходящего фрагмента песня играет целиком.':''),
      el('p',{id:'dj-global-work',class:'dj-work-status'},djWorkText()),
      playlistId!=null?button('Показать готовые DJ-фрагменты','music',async()=>{djOnlyViews['playlist:'+playlistId]=true;await storage.putMeta('dj-list-filters',djOnlyViews);$('sheet').close();navigate('playlist',playlistId);},'button full'):null));
    if(remixMode(preferences)&&clientVersion>=21){
      const durationLabel=el('strong',{id:'remix-duration-value'},time(preferences.remixDurationSeconds));
      nodes.splice(1,0,el('div',{class:'stats'},el('label',{for:'remix-duration'},'Длительность фрагмента'),durationLabel),
        el('input',{id:'remix-duration',class:'fade-duration',type:'range',min:10,max:120,step:1,value:preferences.remixDurationSeconds,'aria-label':'Длительность фрагмента ремикса',oninput:e=>{durationLabel.textContent=time(Number(e.target.value));},onchange:async e=>{try{await savePlaybackPreferences({...preferencesFor(playlistId),remixDurationSeconds:Number(e.target.value)},playlistId);}catch(error){showError(error);}}}),
        el('p',{class:'subtitle'},'От 10 секунд до 2 минут. Новая длительность применяется к следующим фрагментам.'));
      if(clientVersion>=30){
        const visitsLabel=el('strong',{},String(preferences.remixMaxVisits));
        nodes.splice(4,0,el('div',{class:'stats'},el('label',{for:'remix-visits'},'Фрагментов из одной песни'),visitsLabel),
          el('input',{id:'remix-visits',class:'fade-duration',type:'range',min:1,max:10,step:1,value:preferences.remixMaxVisits,'aria-label':'Фрагментов из одной песни',oninput:e=>{visitsLabel.textContent=e.target.value;},onchange:async e=>{try{await savePlaybackPreferences({...preferencesFor(playlistId),remixMaxVisits:Number(e.target.value)},playlistId);}catch(error){showError(error);}}}),
          el('p',{class:'subtitle'},'Не больше выбранного числа. Уже звучащий фрагмент доиграет; одинаковые куски повторяться не будут.'));
        const depthLabel=el('strong',{},String(preferences.remixLookahead));
        nodes.splice(7,0,el('div',{class:'stats'},el('label',{for:'remix-depth'},'Глубина выбора из очереди'),depthLabel),
          el('input',{id:'remix-depth',class:'fade-duration',type:'range',min:1,max:50,step:1,value:preferences.remixLookahead,'aria-label':'Глубина выбора из очереди',oninput:e=>{depthLabel.textContent=e.target.value;},onchange:async e=>{try{await savePlaybackPreferences({...preferencesFor(playlistId),remixLookahead:Number(e.target.value)},playlistId);}catch(error){showError(error);}}}),
          el('p',{class:'subtitle'},'Число следующих песен для подбора перехода. 1 — строго по порядку; 50 — больше свободы. По умолчанию 10.'));
      }
    }
    if(native&&native.version()>=19&&preferences.mode==='dj')nodes.push(el('label',{class:'check'},el('input',{type:'checkbox',checked:preferences.audition,'aria-label':'Только переходы',onchange:async e=>{await savePlaybackPreferences({...preferences,audition:e.target.checked},playlistId);}}),el('span',{},'Только переходы')),el('p',{class:'subtitle'},'Для прослушивания стыков: около 10 секунд до и после перехода, середина песни пропускается.'));
    if(queue.playlistId===playlistId)requestDjPriority();
  }else{
    const value=el('strong',{class:'fade-value'},preferences.seconds.toLocaleString('ru-RU')+' с');
    nodes.push(el('p',{},smooth?'Громкость плавно уменьшается в конце песни и нарастает в начале следующей.':'Песни переключаются как обычно.'),
      el('div',{class:'stats'},el('span',{},'Длительность затухания и нарастания'),value),
      el('input',{class:'fade-duration',type:'range',min:1,max:8,step:.5,value:preferences.seconds,disabled:!smooth,'aria-label':'Длительность плавного перехода',oninput:e=>{value.textContent=Number(e.target.value).toLocaleString('ru-RU')+' с';},onchange:async e=>{try{await savePlaybackPreferences({...preferences,seconds:Number(e.target.value)},playlistId);}catch(error){showError(error);}}}));
    if(!djSupported)nodes.push(el('p',{class:'subtitle'},'DJ-режим доступен в Android-приложении 0.12.0 и новее.'));
  }
  if(!native&&djMode(preferences))nodes.push(el('p',{class:'subtitle'},'Веб-DJ работает с открытым плеером. При сворачивании DJ встаёт на паузу. Фоновый DJ доступен в Android-приложении.'));
  showSheet('Воспроизведение'+(playlistId!=null?' · '+(data.playlists.find(p=>p.id===playlistId)?.name||'Плейлист'):''),...nodes);
}
async function settings(){
  const total=[...saved.values()].reduce((sum,x)=>sum+x.bytes,0);
  let estimate;try{if(!native)estimate=await navigator.storage?.estimate?.();}catch{}
  showSheet('Моя музыка',el('div',{class:'stats'},el('span',{},'На устройстве'),el('strong',{},bytes(total))),
    estimate&&el('div',{class:'stats'},el('span',{},'Доступно для хранения'),el('strong',{},bytes(Math.max(0,estimate.quota-estimate.usage)))),
    sheetAction('Обновить библиотеку','music',async()=>{$('sheet').close();await refresh();}),
    sheetAction('Воспроизведение · '+modeName(activePreferences()),'music',playbackOptions),
    tg?.initData&&sheetAction('Открыть в Chrome','external',async()=>{const {url}=await api('browser-link',{});tg.openLink(url);}),
    !native&&sheetAction('На главный экран','plus',async()=>{
      if(installPrompt){await installPrompt.prompt();installPrompt=null;}else showSheet('На главный экран',el('p',{},/iPad|iPhone|iPod/.test(navigator.userAgent)?'Открой плеер в Safari. Нажми «Поделиться» → «На экран Домой».':'Открой плеер в Chrome. В меню ⋮ выбери «Добавить на главный экран» или «Установить приложение».'));
    }),
    native&&sheetAction('Подключить приложение','external',()=>openLink('https://t.me/aidj_codex_music_bot?start=android')),
    canEdit()&&native&&sheetAction('Перенести из Mini App','download',async()=>{
      globalThis.__importRequested=true;
      await command('importStart');
      showSheet('Перенос из Mini App',el('p',{},'Подготавливаю соединение. Затем откроется Telegram — там нажми «Перенести скачанное».'));
    }),
    canEdit()&&!native&&sheetAction('Перенести скачанное в APK','download',importCached),
    sheetAction('Установить / обновить Android-приложение','download',downloadAPK),
    !native&&sheetAction('Подключить Android-приложение','external',connectAndroid),
    sheetAction('Открыть бота','external',()=>{const url='https://t.me/aidj_codex_music_bot';if(tg?.initData)tg.openTelegramLink(url);else openLink(url);})
  );
}
for(const [id,name,fn] of [['settings','settings',settings],['mini-play','play',togglePlay],['mini-next','next',()=>next()]]){$(id).replaceChildren(icon(name));$(id).addEventListener('click',()=>Promise.resolve(fn()).catch(showError));}
$('now-playing').addEventListener('click',openFull);
for(const node of document.querySelectorAll('.bottom-nav button')){const kind=node.dataset.view;node.append(icon(kind==='home'?'music':kind==='discover'?'search':kind==='saved'?'download':'queue'),el('span',{},kind==='home'?'Музыка':kind==='discover'?'Найти':kind==='saved'?'Скачано':'Очередь'));node.addEventListener('click',()=>navigate(kind));}
for(const id of ['sheet','full-player'])$(id).addEventListener('click',e=>{if(e.target===$(id)){const r=$(id).getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$(id).close();}});
audio.addEventListener('play',()=>{applyBrowserVolume();syncControls();persist();});audio.addEventListener('pause',()=>{syncControls();persist();});
function checkWebRange(){
  if(native||audio.paused||audio.seeking||audio.readyState<1||!queue.current||rangeAdvancing)return;
  const part=segment(playingTrack()||tracks.get(queue.current.trackId));
  if(audio.currentTime>=part.end-.025){rangeAdvancing=true;next(true).catch(showError).finally(()=>rangeAdvancing=false);}
  else if(audio.currentTime<part.start-.05)audio.currentTime=part.start;
}
audio.addEventListener('ended',()=>{if(rangeAdvancing||!native&&!audio.ended)return;rangeAdvancing=true;next(true).catch(showError).finally(()=>rangeAdvancing=false);});
audio.addEventListener('timeupdate',()=>{
  checkWebRange();
  progress();if(Date.now()-lastPersist>5000){lastPersist=Date.now();persist();}
});
audio.addEventListener('loadedmetadata',()=>{if(restoredTime){audio.currentTime=Math.min(restoredTime,audio.duration||restoredTime);restoredTime=0;}if(!native&&queue.current){const part=segment(playingTrack()||tracks.get(queue.current.trackId));if(audio.currentTime<part.start||audio.currentTime>=part.end)audio.currentTime=part.start;}applyBrowserVolume();progress();});
if(!native)setInterval(()=>{checkWebRange();if(!audio.paused)applyBrowserVolume();},50);
audio.addEventListener('error',()=>{loadedEntry=null;toast(online?'Не удалось воспроизвести песню. Попробуй ещё раз.':'Нет сети. Выбери скачанную песню.');});
window.addEventListener('online',()=>{online=true;serverUnavailable=false;connection();(async()=>{await flushVotes();await refresh(true);})().catch(showError);});window.addEventListener('offline',()=>{if(!native)online=false;connection();});
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installPrompt=e;});
document.addEventListener('visibilitychange',()=>{if(document.hidden){if(!native&&djMode(activePreferences()))audio.pause();persist();}else{checkForUpdate();syncLibrary();if(view==='discover')refreshDiscovery().catch(showError);}});
setInterval(()=>{if(!document.hidden){flushRanges();flushVotes();playlistChanges.flush();}if(!native||native.version()<5)syncLibrary();if((serverUnavailable||!loggedIn)&&!document.hidden)api('session').then(()=>refresh(true)).catch(()=>{});},20000);
if(!native&&'mediaSession' in navigator){
  const handlers={play:()=>loadCurrent(true),pause:()=>audio.pause(),previoustrack:previous,nexttrack:()=>next(),seekto:e=>{audio.currentTime=e.seekTime;},seekbackward:e=>{audio.currentTime=Math.max(0,audio.currentTime-(e.seekOffset||10));},seekforward:e=>{audio.currentTime=Math.min(audio.duration,audio.currentTime+(e.seekOffset||10));}};
  for(const [name,fn] of Object.entries(handlers))try{navigator.mediaSession.setActionHandler(name,event=>Promise.resolve(fn(event)).catch(showError));}catch{}
}
setInterval(checkForUpdate,900000);
$('update-notice').addEventListener('click',()=>downloadAPK().catch(showError));
async function initialize(){
  try{tg?.ready();tg?.expand();tg?.setHeaderColor('#141514');tg?.setBackgroundColor('#141514');}catch{}
  catalogError=null;render();
  try{
    // Player controls must keep receiving native state even if a cache read
    // fails. A catalog retry must not leave a playing service uncontrollable.
    if(native){nativeReady=true;globalThis.__aidjNativeState(JSON.parse(native.snapshot()));}
    const params=new URLSearchParams(location.hash.slice(1)),loginCode=params.get('login');
    if(loginCode){history.replaceState(null,'',location.pathname);await api('auth',{loginCode});}
    else if(tg?.initData)await api('auth',{initData:tg.initData});
    else{try{await api('session');}catch{}if(!appAccess)appAccess=await storage.meta('access');}
    personalPlaylists=new PersonalPlaylists(await storage.meta(personalKey()));
    saved=new Map((await storage.files()).map(x=>[x.md5,x]));
    playbackPreferences=normalisePlayback(await storage.meta('playback-preferences'));playlistPlayback=await storage.meta('playlist-playback')||{};
    djOnlyViews=await storage.meta('dj-list-filters')||{};listOptions=await storage.meta('review-list-options')||{};voteOverrides=await storage.meta('review-votes')||{};reviewPeople=await storage.meta('review-people')||{};
    rangeOverrides=await storage.meta('playback-ranges')||{};
    const lastSearch=await storage.meta('discovery');if(lastSearch){discovery.query=lastSearch.query||'';if(lastSearch.id)discovery.job={id:lastSearch.id};}
    let cached=null;try{cached=await storage.meta('catalog');}catch{}
    playlistChanges.restore(cached,appAccess?.role==='listener'?[]:await storage.meta('playlist-changes'));if(cached)displayCatalog(playlistChanges.project());
    let state=null;try{state=await storage.meta('player');}catch{}
    if(state){queue=new Queue(state);queue.entries=restoreRemixMetadata(queue.entries.filter(x=>tracks.has(x.trackId)));lastNativeSnapshot='';restoredTime=state.time||0;restoreEntry=queue.currentId;renderMini();}
    if(native){nativeReady=true;globalThis.__aidjNativeState(JSON.parse(native.snapshot()));}
    await refresh(true);checkForUpdate();
    flushRanges();flushVotes();
    render();
    if(!native&&'serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
    connection();
    if(!native&&new URLSearchParams(location.hash.slice(1)).get('import')==='1')await importCached();
  }catch(error){
    if(catalogReady){serverUnavailable=true;if(!native)online=navigator.onLine;connection();render();}else{catalogError=error;render();}
  }
}
async function checkForUpdate(){
  if(!native||document.hidden||Date.now()-lastUpdateCheck<300000)return;lastUpdateCheck=Date.now();
  try{const response=await nativeFetch('android-version');if(!response.ok)return;const v=await response.json();
    latestRelease=Number.isInteger(v.versionCode)&&v.versionCode>native.version()&&/^https:\/\//.test(v.url||'')?v:null;
    $('update-notice').hidden=!latestRelease;if(latestRelease){$('update-notice').textContent='Обновить';$('update-notice').setAttribute('aria-label','Обновить приложение до '+latestRelease.versionName);}
  }catch{}
}
async function downloadAPK(){
  const version=latestRelease||await api('android-version');
  if(native&&version.versionCode<=native.version()){toast('Установлена последняя версия');return;}
  if(tg?.initData&&tg.downloadFile&&tg.isVersionAtLeast('8.0')){tg.downloadFile({url:version.url,file_name:'AI-DJ-'+version.versionName+'.apk'});}
  else openLink(version.url);
}
async function connectAndroid(){const {url}=await api('android-connect',{});openLink(url);}
async function importCached(){
  const current=await storage.files();
  const byHash=new Map(data.tracks.map(track=>[track.md5,track]));
  const candidates=current.map(file=>byHash.get(file.md5)).filter(Boolean);
  if(!candidates.length){toast('В Mini App пока нет скачанных песен');return;}
  const remove=el('input',{type:'checkbox'});
  const caption=el('p',{},'Сначала открой в APK настройки → «Перенести из Mini App». Файлы будут скопированы внутри телефона.');
  const status=el('p',{role:'status'});let cancelled=false;
  const start=button('Перенести песни · '+candidates.length,'download',async()=>{
    start.disabled=true;cancelled=false;
    try{
      const descriptor=await api('import-current');
      const transfer=new LocalTransfer(descriptor);
      const result=await transfer.run(candidates,storage.blob,{
        cancelled:()=>cancelled,
        progress:(done,total,track)=>{status.textContent=`${done} / ${total} · ${track.title}`;},
        completed:async track=>{if(remove.checked){await storage.remove(track.md5);saved.delete(track.md5);}},
      });
      render();status.textContent=`${cancelled?'Остановлено':'Готово'}. В APK: ${count(result.done)}.`;toast(cancelled?'Перенос остановлен':'Перенос завершён');
    }finally{start.disabled=false;}
  },'button primary full');
  showSheet('Перенести в APK',caption,el('div',{class:'stats'},el('span',{},count(candidates.length)),el('strong',{},bytes(candidates.reduce((sum,t)=>sum+t.bytes,0)))),
    el('label',{class:'check'},remove,el('span',{},'Удалить копии из Mini App после переноса')),status,start,
    button('Остановить',null,()=>{cancelled=true;status.textContent='Завершаю текущий файл…';},'button full'));
}
if(native){
  globalThis.__aidjNativeState=state=>{
    if(!nativeReady)return;
    if(['admin','listener'].includes(state.access?.role))appAccess=state.access;
    const p=state.player, before=queue.currentId, beforeCue=JSON.stringify(queue.current);
    if(state.network?.connected!==undefined){nativeConnected=state.network.connected;online=nativeConnected;connection();}
    if(state.network?.known){serverUnavailable=!state.network.reachable;if(state.network.reachable&&nativeConnected!==false)online=true;connection();}
    const conflict=state.queueConflict&&state.requestedUiRevision===nativeRevision;
    const accepted=!!p?.queue?.entries&&(p.queue.playbackEpoch??0)>=nativePlaybackEpoch&&(p.queue.uiRevision>=nativeRevision||conflict);
    const snapshot=accepted?JSON.stringify(p.queue):'';
    if(accepted&&(snapshot!==lastNativeSnapshot||conflict)){const value=p.queue.remixMetadataOmitted?{...p.queue,entries:restoreRemixMetadata(p.queue.entries)}:p.queue;queue=new Queue(value);nativeRevision=p.queue.uiRevision||0;nativePlaybackEpoch=p.queue.playbackEpoch||0;lastNativeSnapshot=snapshot;lastNativeQueue=JSON.stringify(playerSnapshot(true));loadedEntry=queue.currentId;}
    const forecast=accepted?readQueueForecast(p):queueForecast,forecastChanged=JSON.stringify(forecast)!==JSON.stringify(queueForecast);queueForecast=forecast;
    if(p&&accepted)audio.accept(p);
    if(state.downloads)downloads.accept(state.downloads);
    if(state.libraryRevision&&state.libraryRevision!==libraryRevision)syncLibrary(state.libraryRevision);
    let filesChanged=false;
    if(Array.isArray(state.files)){const files=JSON.stringify(state.files);if(files!==lastFiles){lastFiles=files;saved=new Map(state.files.map(x=>[x.md5,x]));filesChanged=true;}}
    if(filesChanged||forecastChanged||before!==queue.currentId||beforeCue!==JSON.stringify(queue.current)){render(true);requestDjPriority();if($('full-player').open)renderFull();}
    if(p?.error&&p.error!==globalThis.__nativeError){globalThis.__nativeError=p.error;toast(p.error);}
    if(globalThis.__importRequested&&state.transfer?.ready){globalThis.__importRequested=false;openLink('https://t.me/aidj_codex_music_bot?start=import');}
    if(globalThis.__importRequested&&state.transfer?.error){globalThis.__importRequested=false;toast(state.transfer.error);}
  };
  globalThis.__aidjBack=()=>{if($('sheet').open){$('sheet').close();return true;}if($('full-player').open){$('full-player').close();return true;}if(view!=='home'){navigate('home');return true;}return false;};
}
initialize();
