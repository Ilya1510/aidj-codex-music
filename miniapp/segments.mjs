export function segment(track={}){
  const total=Math.max(.5,Number(track.duration)||.5);
  const start=Math.max(0,Math.min(Number(track.rangeStart)||0,total-.5));
  const end=Math.max(start+.5,Math.min(Number.isFinite(track.rangeEnd)?track.rangeEnd:total,total));
  return {start,end,total};
}
export function moveSegment(part,delta){
  const width=part.end-part.start,start=Math.max(0,Math.min(part.start+delta,part.total-width));
  return {...part,start,end:start+width};
}
export function changeBoundary(part,side,value){
  return side==='start'?{...part,start:Math.max(0,Math.min(value,part.end-.5))}:{...part,end:Math.min(part.total,Math.max(value,part.start+.5))};
}
export function parseTime(value){
  const parts=value.trim().replace(',','.').split(':');
  if(!parts.length||parts.length>3||parts.some(x=>!/^\d+(?:\.\d+)?$/.test(x))||parts.slice(1).some(x=>Number(x)>=60))throw new Error('Время: минуты:секунды, например 1:30.');
  return parts.reverse().reduce((sum,x,i)=>sum+Number(x)*60**i,0);
}
export function formatTime(value){
  const tenths=Math.round(value*10),minutes=Math.floor(tenths/600),seconds=(tenths%600)/10;
  return minutes+':'+seconds.toFixed(1).padStart(4,'0').replace(/\.0$/,'');
}
