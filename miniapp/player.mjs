export class Queue {
  constructor(snapshot = {}) {
    this.playlistId = snapshot.playlistId ?? null;
    this.fixedSet = snapshot.fixedSet && typeof snapshot.fixedSet.id==='string' ? {...snapshot.fixedSet} : null;
    this.entries = Array.isArray(snapshot.entries) ? snapshot.entries : [];
    this.currentId = snapshot.currentId || null;
    this.manualNextId = snapshot.manualNextId && snapshot.manualNextId!==this.currentId ? snapshot.manualNextId : null;
    this.shuffle = this.fixedSet ? false : Boolean(snapshot.shuffle);
    this.repeat = this.fixedSet ? 'all' : ['off', 'all', 'one'].includes(snapshot.repeat) ? snapshot.repeat : 'off';
  }
  get index() { return this.entries.findIndex(x => x.id === this.currentId); }
  get current() { return this.entries[this.index] || null; }
  get following() { return this.entries[this.index+1] || ((this.fixedSet||this.repeat==='all')?this.entries[0]:null) || null; }
  get upcoming() {
    const at=this.index;
    return this.fixedSet&&at>=0 ? [...this.entries.slice(at+1),...this.entries.slice(0,at)] : this.entries.slice(Math.max(0,at+1));
  }
  entry(trackId) { return { id: crypto.randomUUID(), trackId, ordinal: Math.max(-1, ...this.entries.map(x => x.ordinal)) + 1 }; }
  start(ids, index = 0, shuffle = false, random = Math.random) {
    if(this.fixedSet)this.repeat='off';
    this.manualNextId=null;this.fixedSet=null;
    this.entries = [];
    for (const id of ids) this.entries.push(this.entry(id));
    this.shuffle = shuffle;
    if (shuffle) this.mix(this.entries, random);
    this.currentId = this.entries[shuffle ? 0 : index]?.id || null;
    return this.current;
  }
  mix(items, random) {
    for (let i = items.length - 1; i > 0; i--) {
      const j = Math.floor(random() * (i + 1));
      [items[i], items[j]] = [items[j], items[i]];
    }
    return items;
  }
  toggleShuffle(random = Math.random) {
    this.manualNextId=null;
    this.shuffle = !this.shuffle;
    const at = Math.max(0, this.index + 1), rest = this.entries.slice(at);
    if (this.shuffle) this.mix(rest, random); else rest.sort((a, b) => a.ordinal - b.ordinal);
    this.entries.splice(at, rest.length, ...rest);
  }
  select(id) { if (this.entries.some(x => x.id === id)) this.currentId = id;if(this.manualNextId===this.currentId)this.manualNextId=null; return this.current; }
  next(ended = false) {
    if (ended && !this.fixedSet && this.repeat === 'one') return this.current;
    const next = this.index + 1;
    if (next < this.entries.length) this.currentId = this.entries[next].id;
    else if ((this.fixedSet || this.repeat === 'all') && this.entries.length) {
      if (this.shuffle&&!this.fixedSet) this.mix(this.entries, Math.random);
      this.currentId = this.entries[0].id;
    } else return null;
    if(this.manualNextId===this.currentId)this.manualNextId=null;
    return this.current;
  }
  previous() { if (this.index > 0) this.currentId = this.entries[this.index - 1].id; else if(this.fixedSet&&this.entries.length)this.currentId=this.entries.at(-1).id; return this.current; }
  append(id, next = false) {
    const item = this.entry(id);
    this.entries.splice(next && this.index >= 0 ? this.index + 1 : this.entries.length, 0, item);
    if (!this.currentId) this.currentId = item.id;
    return item;
  }
  remove(id) {
    if(this.manualNextId===id)this.manualNextId=null;
    const at = this.entries.findIndex(x => x.id === id);
    if (at < 0) return;
    const following = this.entries[at + 1]?.id || null;
    this.entries.splice(at, 1);
    if (this.currentId === id) this.currentId = following;
  }
  move(id, delta) {
    const at = this.entries.findIndex(x => x.id === id), to = at + delta;
    if (at < 0 || to < 0 || to >= this.entries.length) return;
    const [item] = this.entries.splice(at, 1); this.entries.splice(to, 0, item);
  }
  moveUpcoming(id,beforeId=null) {
    const first=Math.max(0,this.index+1),at=this.entries.findIndex(x=>x.id===id);
    if(at<first||id===beforeId)return false;
    const before=beforeId==null?this.entries.length:this.entries.findIndex(x=>x.id===beforeId);
    if(before<first)return false;
    const to=before-(at<before?1:0);if(at===to)return false;
    const [item]=this.entries.splice(at,1);this.entries.splice(to,0,item);
    this.manualNextId=this.entries[Math.max(0,this.index+1)]?.id||null;
    return true;
  }
  clearUpcoming() { this.manualNextId=null;if (this.index >= 0) this.entries.splice(this.index + 1); else this.entries = []; }
  snapshot() { return { playlistId:this.playlistId, ...(this.fixedSet?{fixedSet:this.fixedSet}:{}), entries: this.entries, currentId: this.currentId, shuffle: this.shuffle, repeat: this.repeat,...(this.manualNextId?{manualNextId:this.manualNextId}:{}) }; }
}
