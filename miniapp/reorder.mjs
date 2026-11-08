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

// Touch scrolling stays native until a stationary long press starts a drag.
// Entry IDs, not song IDs/array indices, identify duplicate queue occurrences.
export function sortableQueue(list,{onMove,onActive=()=>{},holdMs=420}){
  let pending=null,drag=null,frame=0,suppressUntil=0;
  const scroll=document.scrollingElement;
  const rows=()=>[...list.querySelectorAll('[data-queue-entry]')];
  const paint=()=>{
    if(!drag)return;
    const delta=drag.y-drag.startY+scroll.scrollTop-drag.scrollTop;
    const center=drag.rects[drag.from].top+drag.rects[drag.from].height/2+delta;
    let to=0;
    for(let i=1;i<drag.rects.length;i++)if(center>(drag.rects[i-1].top+drag.rects[i-1].height/2+drag.rects[i].top+drag.rects[i].height/2)/2)to=i;
    drag.to=to;
    const gap=drag.rects.length>1?Math.max(0,drag.rects[1].top-drag.rects[0].bottom):0,shift=drag.rects[drag.from].height+gap;
    drag.rows.forEach((row,i)=>{row.style.transform=`translateY(${i===drag.from?delta:to>drag.from&&i>drag.from&&i<=to?-shift:to<drag.from&&i>=to&&i<drag.from?shift:0}px)`;});
  };
  const finish=commit=>{
    if(pending){clearTimeout(pending.timer);pending=null;}
    if(!drag)return;
    const d=drag;drag=null;cancelAnimationFrame(frame);suppressUntil=Date.now()+500;
    for(const row of d.rows){row.style.transform='';row.classList.remove('dragging');row.removeAttribute('aria-grabbed');}
    list.classList.remove('is-dragging');scroll.style.scrollBehavior=d.scrollBehavior;onActive(false);
    if(d.pointer!=null&&d.row.hasPointerCapture(d.pointer))d.row.releasePointerCapture(d.pointer);
    if(commit&&d.from!==d.to){const rest=d.rows.filter(r=>r!==d.row);onMove(d.id,rest[d.to]?.dataset.queueEntry??null);}
  };
  const tick=()=>{
    if(!drag)return;
    if(!list.isConnected||document.hidden){finish(false);return;}
    const mini=document.getElementById('mini-player'),bottom=mini&&!mini.hidden?mini.getBoundingClientRect().top:innerHeight-80;
    const speed=drag.y<70?-Math.min(16,(70-drag.y)/4):drag.y>bottom-45?Math.min(16,(drag.y-bottom+45)/4):0;
    if(speed)scroll.scrollTop+=speed;
    paint();frame=requestAnimationFrame(tick);
  };
  const arm=(target,x,y,identifier,pointer=null)=>{
    const row=target.closest('[data-queue-entry]');
    if(!row||target.closest('button:not(.song-main)')||drag||pending)return;
    pending={row,x,y,identifier,pointer,timer:setTimeout(()=>{
      const p=pending;if(!p||!row.isConnected)return;pending=null;
      const all=rows();drag={rows:all,rects:all.map(r=>r.getBoundingClientRect()),row,from:all.indexOf(row),to:all.indexOf(row),id:row.dataset.queueEntry,
        y:p.y,startY:p.y,scrollTop:scroll.scrollTop,scrollBehavior:scroll.style.scrollBehavior,identifier,pointer};
      scroll.style.scrollBehavior='auto';row.classList.add('dragging');row.setAttribute('aria-grabbed','true');list.classList.add('is-dragging');onActive(true);
      if(pointer!=null)row.setPointerCapture(pointer);frame=requestAnimationFrame(tick);
    },holdMs)};
  };
  const move=(event,x,y,identifier)=>{
    if(pending&&pending.identifier===identifier&&Math.hypot(x-pending.x,y-pending.y)>10)finish(false);
    if(drag&&drag.identifier===identifier){event.preventDefault();drag.y=y;paint();}
  };
  list.addEventListener('touchstart',e=>{if(e.touches.length!==1){finish(false);return;}const t=e.changedTouches[0];arm(e.target,t.clientX,t.clientY,t.identifier);},{passive:true});
  list.addEventListener('touchmove',e=>{const t=[...e.changedTouches].find(t=>t.identifier===(drag||pending)?.identifier);if(t)move(e,t.clientX,t.clientY,t.identifier);},{passive:false});
  list.addEventListener('touchend',e=>{if([...e.changedTouches].some(t=>t.identifier===(drag||pending)?.identifier))finish(true);});
  list.addEventListener('touchcancel',()=>finish(false));
  list.addEventListener('pointerdown',e=>{if(e.pointerType!=='touch'&&e.button===0)arm(e.target,e.clientX,e.clientY,e.pointerId,e.pointerId);});
  list.addEventListener('pointermove',e=>{if(e.pointerType!=='touch')move(e,e.clientX,e.clientY,e.pointerId);},{passive:false});
  list.addEventListener('pointerup',e=>{if(e.pointerType!=='touch'&&(drag||pending)?.identifier===e.pointerId)finish(true);});
  list.addEventListener('pointercancel',e=>{if(e.pointerType!=='touch')finish(false);});
  list.addEventListener('pointerout',e=>{if(pending?.pointer!=null&&!pending.row.contains(e.relatedTarget))finish(false);});
  list.addEventListener('lostpointercapture',e=>{if(drag?.pointer!=null&&drag.pointer===e.pointerId)finish(false);});
  list.addEventListener('contextmenu',e=>{if(pending||drag||Date.now()<suppressUntil)e.preventDefault();});
  list.addEventListener('click',e=>{if(drag||Date.now()<suppressUntil){e.preventDefault();e.stopImmediatePropagation();}},true);
  list.addEventListener('keydown',e=>{
    if(drag&&e.key==='Escape'){e.preventDefault();finish(false);return;}
    const row=e.target.closest('[data-queue-entry]');if(!row||!e.altKey||!['ArrowUp','ArrowDown','Home','End'].includes(e.key))return;
    e.preventDefault();const all=rows(),from=all.indexOf(row),to=e.key==='Home'?0:e.key==='End'?all.length-1:Math.max(0,Math.min(all.length-1,from+(e.key==='ArrowUp'?-1:1)));
    if(from!==to){const rest=all.filter(r=>r!==row);onMove(row.dataset.queueEntry,rest[to]?.dataset.queueEntry??null);}
  });
}
