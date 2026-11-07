// Durable local intents. Disk requests run serially, independently of the UI.
export class PlaylistChanges {
  constructor({save,send,refresh,cache,changed=()=>{},failed=()=>{},canSend=()=>true}) {
    Object.assign(this,{save,send,refresh,cache,changed,failed,canSend});
    this.base={tracks:[],playlists:[]};this.pending=[];this.writes=Promise.resolve();
    this.running=null;this.timer=null;this.attempts=0;this.generation=0;
  }
  restore(catalog,pending=[]) {
    this.base=catalog||this.base;
    this.pending=Array.isArray(pending)?pending.filter(p=>['add','remove'].includes(p.action)&&typeof p.opId==='string'):[];
  }
  project() {
    const byId=new Map(this.base.tracks.map(t=>[t.id,t]));
    const playlists=this.base.playlists.map(p=>({...p,trackIds:[...p.trackIds]}));
    for(const item of this.pending) {
      const p=playlists.find(p=>p.id===item.playlistId),track=byId.get(item.trackId);if(!p||!track)continue;
      const same=id=>id===track.id||(track.md5&&byId.get(id)?.md5===track.md5);
      if(item.action==='remove')p.trackIds=p.trackIds.filter(id=>!same(id));
      else if(!p.trackIds.some(same))p.trackIds.unshift(track.id);
    }
    return {...this.base,playlists};
  }
  // Serialize metadata writes too: an older asynchronous write must never
  // overwrite a newer intent or acknowledgement after an app restart.
  update(fn) {
    const task=this.writes.then(fn);this.writes=task.catch(()=>{});return task;
  }
  async accept(catalog,generation=this.generation) {
    return this.update(async()=>{if(generation!==this.generation)return false;await this.cache(catalog);this.base=catalog;this.changed(this.project());return true;});
  }
  async enqueue(action,{playlistId,trackId}) {
    if(!['add','remove'].includes(action))throw new Error('Неизвестное действие.');
    const item={action,playlistId,trackId,opId:crypto.randomUUID()};
    await this.update(async()=>{
      const previous=this.pending.findLast(p=>p.playlistId===playlistId&&p.trackId===trackId);
      if(previous?.action===action)return;
      const next=[...this.pending,item];await this.save(next);this.pending=next;this.changed(this.project());
    });
    // Do not return the network promise to the button handler.
    this.flush();
  }
  flush() {
    if(this.running)return this.running;
    clearTimeout(this.timer);this.timer=null;
    this.running=this.run().catch(()=>this.retry()).finally(()=>{
      this.running=null;
      if(this.pending.length&&this.canSend()&&!this.timer)queueMicrotask(()=>this.flush());
    });
    return this.running;
  }
  retry() {
    if(this.timer||!this.pending.length)return;
    const delay=Math.min(30000,2000*2**Math.min(this.attempts++,4));
    this.timer=setTimeout(()=>{this.timer=null;this.flush();},delay);
  }
  async run() {
    while(this.canSend()) {
      await this.writes;const item=this.pending[0];if(!item)return;
      // Freeze the first submitted revision/track identity for retries. The
      // server resumes an accepted opId, including a lost response after commit.
      if(item.revision===undefined)await this.update(async()=>{
        const p=this.base.playlists.find(p=>p.id===item.playlistId);
        const track=this.base.tracks.find(t=>t.id===item.trackId);
        const alias=item.action==='remove'&&track?.md5&&p?.trackIds.find(id=>this.base.tracks.some(t=>t.id===id&&t.md5===track.md5));
        const frozen={...item,revision:p?.revision??null,...(alias?{trackId:alias}:{})};
        const next=this.pending.map(p=>p===item?frozen:p);await this.save(next);this.pending=next;
      });
      const sent=this.pending[0];let catalog;
      try {catalog=await this.send({...sent});}
      catch(error) {
        if(error.status===409) {
          // No mutation was accepted with this revision (or the bot held its
          // lock). Rebase membership changes, without discarding other edits.
          await this.accept(await this.refresh());
          await this.update(async()=>{
            const next=this.pending.map(p=>p.opId===sent.opId?{...p,revision:undefined}:p);
            await this.save(next);this.pending=next;
          });
        } else if(error.status===400||error.status===404) {
          // A definite rejection can be rolled back. A timeout, 5xx or auth
          // failure is ambiguous and keeps the same durable operation instead.
          const fresh=await this.refresh();
          await this.finish(sent,fresh);this.failed(error,sent);continue;
        }
        this.retry();return;
      }
      await this.finish(sent,catalog);this.attempts=0;
    }
  }
  async finish(item,catalog) {
    await this.update(async()=>{
      // Cache the confirmed catalog before acknowledging the local operation.
      // If interrupted between writes, replaying the same opId is harmless.
      await this.cache(catalog);this.base=catalog;this.generation++;
      const next=this.pending.filter(p=>p.opId!==item.opId);await this.save(next);this.pending=next;
      this.changed(this.project());
    });
  }
}
