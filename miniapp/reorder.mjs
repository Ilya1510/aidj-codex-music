// Pointer-based reordering keeps the dragged node connected until release.
// Only the handle captures touch; the rest of the list keeps ordinary scrolling.
export function sortablePeople(list,{scrollElement,onMove,onAnnounce=()=>{}}){
  let drag=null,frame=0;
  const rows=()=>[...list.querySelectorAll('[data-priority-id]')];
  const clean=()=>{for(const row of rows()){row.style.transform='';row.classList.remove('dragging','shifted');}list.classList.remove('is-dragging');};
  const paint=()=>{
    if(!drag)return;
    const delta=drag.y-drag.startY+scrollElement.scrollTop-drag.scrollTop;
    const center=drag.rects[drag.from].top+drag.rects[drag.from].height/2+delta;
    let to=0;
    for(let i=1;i<drag.rects.length;i++)if(center>(drag.rects[i-1].top+drag.rects[i-1].height/2+drag.rects[i].top+drag.rects[i].height/2)/2)to=i;
    drag.to=to;
    const gap=drag.rects.length>1?Math.max(0,drag.rects[1].top-drag.rects[0].bottom):0;
    const shift=drag.rects[drag.from].height+gap;
    drag.rows.forEach((row,i)=>{
      const displaced=to>drag.from&&i>drag.from&&i<=to?-shift:to<drag.from&&i>=to&&i<drag.from?shift:0;
      row.style.transform=`translateY(${i===drag.from?delta:displaced}px)`;
      row.classList.toggle('shifted',i!==drag.from&&displaced!==0);
    });
  };
  const tick=()=>{
    if(!drag)return;
    if(!list.isConnected||!scrollElement.open){finish(false);return;}
    const bounds=scrollElement.getBoundingClientRect();
    const footer=scrollElement.querySelector('.filter-result');
    const top=bounds.top+40,bottom=Math.min(bounds.bottom-20,footer?.getBoundingClientRect().top??bounds.bottom-20);
    const speed=drag.y<top+40?-Math.min(14,(top+40-drag.y)/4):drag.y>bottom-40?Math.min(14,(drag.y-bottom+40)/4):0;
    if(speed)scrollElement.scrollTop=Math.max(0,Math.min(drag.maxScroll,scrollElement.scrollTop+speed));
    paint();frame=requestAnimationFrame(tick);
  };
  const finish=commit=>{
    if(!drag)return;
    const current=drag;drag=null;cancelAnimationFrame(frame);clean();
    if(current.handle.hasPointerCapture(current.pointer))current.handle.releasePointerCapture(current.pointer);
    if(commit&&current.from!==current.to){onMove(current.from,current.to);onAnnounce(current.id,current.to);}
  };
  list.addEventListener('pointerdown',event=>{
    const handle=event.target.closest('.priority-handle');
    if(!handle||event.button!==0||drag)return;
    event.preventDefault();const row=handle.closest('[data-priority-id]'),all=rows();
    drag={rows:all,rects:all.map(r=>r.getBoundingClientRect()),from:all.indexOf(row),to:all.indexOf(row),id:row.dataset.priorityId,
      pointer:event.pointerId,handle,y:event.clientY,startY:event.clientY,scrollTop:scrollElement.scrollTop,maxScroll:scrollElement.scrollHeight-scrollElement.clientHeight};
    handle.setPointerCapture(event.pointerId);row.classList.add('dragging');list.classList.add('is-dragging');frame=requestAnimationFrame(tick);
  });
  list.addEventListener('pointermove',event=>{if(drag&&event.pointerId===drag.pointer){event.preventDefault();drag.y=event.clientY;paint();}},{passive:false});
  list.addEventListener('pointerup',event=>{if(drag&&event.pointerId===drag.pointer){drag.y=event.clientY;paint();finish(true);}});
  list.addEventListener('pointercancel',event=>{if(drag&&event.pointerId===drag.pointer)finish(false);});
  list.addEventListener('lostpointercapture',event=>{if(drag&&event.pointerId===drag.pointer)finish(false);});
  list.addEventListener('keydown',event=>{
    const handle=event.target.closest('.priority-handle');if(!handle||!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
    event.preventDefault();const all=rows(),row=handle.closest('[data-priority-id]'),from=all.indexOf(row);
    const to=event.key==='Home'?0:event.key==='End'?all.length-1:Math.max(0,Math.min(all.length-1,from+(event.key==='ArrowUp'?-1:1)));
    if(from===to)return;const id=row.dataset.priorityId;onMove(from,to);onAnnounce(id,to);
    const moved=rows().find(r=>r.dataset.priorityId===id);moved?.querySelector('.priority-handle')?.focus({preventScroll:true});moved?.scrollIntoView({block:'nearest'});
  });
}
