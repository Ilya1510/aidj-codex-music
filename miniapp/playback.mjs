export function playbackSettings(value){
  const seconds=typeof value?.seconds==='number'?value.seconds:NaN;
  const remixDuration=typeof value?.remixDurationSeconds==='number'?value.remixDurationSeconds:NaN;
  const visits=typeof value?.remixMaxVisits==='number'?value.remixMaxVisits:NaN;
  const depth=typeof value?.remixLookahead==='number'?value.remixLookahead:NaN;
  return {audition:value?.audition===true,mode:['smooth','dj','remix'].includes(value?.mode)?value.mode:'normal',length:['short','normal','long'].includes(value?.length)?value.length:'normal',seconds:Number.isFinite(seconds)?Math.max(1,Math.min(8,Math.round(seconds*2)/2)):3,remixDurationSeconds:Number.isFinite(remixDuration)?Math.max(10,Math.min(120,Math.round(remixDuration))):12,remixMaxVisits:Number.isFinite(visits)?Math.max(1,Math.min(10,Math.round(visits))):3,remixLookahead:Number.isFinite(depth)?Math.max(1,Math.min(50,Math.round(depth))):10};
}
export function fadeGain(position,start,end,seconds){
  if(!Number.isFinite(seconds)||seconds<=0)return 1;
  if(![position,start,end].every(Number.isFinite)||end<=start)return 1;
  const window=Math.min(seconds,(end-start)/2);
  return Math.max(0,Math.min(1,(position-start)/window,(end-position)/window));
}
