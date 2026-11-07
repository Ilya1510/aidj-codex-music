export class Queue {
  constructor(snapshot = {}) {
    this.playlistId = snapshot.playlistId ?? null;
    this.entries = Array.isArray(snapshot.entries) ? snapshot.entries : [];
    this.currentId = snapshot.currentId || null;
    this.shuffle = Boolean(snapshot.shuffle);
    this.repeat = ['off', 'all', 'one'].includes(snapshot.repeat) ? snapshot.repeat : 'off';
  }
  get index() { return this.entries.findIndex(x => x.id === this.currentId); }
  get current() { return this.entries[this.index] || null; }
  entry(trackId) { return { id: crypto.randomUUID(), trackId, ordinal: Math.max(-1, ...this.entries.map(x => x.ordinal)) + 1 }; }
  start(ids, index = 0, shuffle = false, random = Math.random) {
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
    this.shuffle = !this.shuffle;
    const at = Math.max(0, this.index + 1), rest = this.entries.slice(at);
    if (this.shuffle) this.mix(rest, random); else rest.sort((a, b) => a.ordinal - b.ordinal);
    this.entries.splice(at, rest.length, ...rest);
  }
  select(id) { if (this.entries.some(x => x.id === id)) this.currentId = id; return this.current; }
  next(ended = false) {
    if (ended && this.repeat === 'one') return this.current;
    const next = this.index + 1;
    if (next < this.entries.length) this.currentId = this.entries[next].id;
    else if (this.repeat === 'all' && this.entries.length) {
      if (this.shuffle) this.mix(this.entries, Math.random);
      this.currentId = this.entries[0].id;
    } else return null;
    return this.current;
  }
  previous() { if (this.index > 0) this.currentId = this.entries[this.index - 1].id; return this.current; }
  append(id, next = false) {
    const item = this.entry(id);
    this.entries.splice(next && this.index >= 0 ? this.index + 1 : this.entries.length, 0, item);
    if (!this.currentId) this.currentId = item.id;
    return item;
  }
  remove(id) {
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
  clearUpcoming() { if (this.index >= 0) this.entries.splice(this.index + 1); else this.entries = []; }
  snapshot() { return { playlistId:this.playlistId, entries: this.entries, currentId: this.currentId, shuffle: this.shuffle, repeat: this.repeat }; }
}
