export function playbackSettings(value){
  const seconds=typeof value?.seconds==='number'?value.seconds:NaN;
  return {audition:value?.audition===true,mode:['smooth','dj','remix'].includes(value?.mode)?value.mode:'normal',length:['short','normal','long'].includes(value?.length)?value.length:'normal',seconds:Number.isFinite(seconds)?Math.max(1,Math.min(8,Math.round(seconds*2)/2)):3};
}
export function fadeGain(position,start,end,seconds){
  if(!Number.isFinite(seconds)||seconds<=0)return 1;
  if(![position,start,end].every(Number.isFinite)||end<=start)return 1;
  const window=Math.min(seconds,(end-start)/2);
  return Math.max(0,Math.min(1,(position-start)/window,(end-position)/window));
}
