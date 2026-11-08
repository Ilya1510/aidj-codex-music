// Device-local playlists use negative IDs and never enter the shared edit API.
export class PersonalPlaylists {
  constructor(saved=[]){
    this.items=(Array.isArray(saved)?saved:[]).filter(p=>Number.isSafeInteger(p?.id)&&p.id<0&&typeof p.name==='string'&&p.name.trim()&&Array.isArray(p.trackIds)).slice(0,100).map(p=>({id:p.id,name:p.name.slice(0,64),personal:true,trackIds:[...new Set(p.trackIds.filter(id=>typeof id==='string'&&/^[a-f0-9]{24}$/.test(id)))].slice(0,5000)}));
  }
  mutate(action,body,validTracks){
    const name=()=>{const value=String(body.name||'').trim();if(!value||value.length>64)throw new Error('Название должно содержать от 1 до 64 символов.');return value;};
    if(action==='create'){
      if(this.items.length>=100)throw new Error('На устройстве уже 100 личных плейлистов.');
      const p={id:Math.min(0,...this.items.map(p=>p.id))-1,name:name(),personal:true,trackIds:[...new Set((body.trackIds||[]).filter(id=>validTracks.has(id)))]};
      this.items.push(p);return p;
    }
    const p=this.items.find(p=>p.id===body.playlistId);
    if(!p)throw new Error('Общий плейлист доступен только для прослушивания.');
    if(action==='rename')p.name=name();
    else if(action==='delete')this.items=this.items.filter(value=>value!==p);
    else if(action==='add'){if(!validTracks.has(body.trackId))throw new Error('Песня не найдена.');if(!p.trackIds.includes(body.trackId))p.trackIds.push(body.trackId);}
    else if(action==='remove')p.trackIds=p.trackIds.filter(id=>id!==body.trackId);
    else if(action==='reorder'){
      if(!Array.isArray(body.trackIds)||body.trackIds.length!==p.trackIds.length||new Set(body.trackIds).size!==p.trackIds.length||!body.trackIds.every(id=>p.trackIds.includes(id)))throw new Error('Состав плейлиста изменился.');
      p.trackIds=[...body.trackIds];
    }else throw new Error('Это действие недоступно для личного плейлиста.');
    return p;
  }
}
