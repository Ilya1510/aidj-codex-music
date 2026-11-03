let opening;
const native=globalThis.AndroidMusic;
function db() {
  if (!opening) opening = new Promise((resolve, reject) => {
    const request = indexedDB.open('aidj-music', 1);
    request.onupgradeneeded = () => { for (const name of ['blobs', 'files', 'meta']) request.result.createObjectStore(name); };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error('Закрой другие окна плеера и попробуй снова.'));
  });
  return opening;
}
async function get(store, key) {
  const database = await db();
  return new Promise((resolve, reject) => {
    const request = database.transaction(store).objectStore(store).get(key);
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
}
export const blob = md5 => get('blobs', md5);
export const meta = key => native ? Promise.resolve(JSON.parse(native.readMeta(key))) : get('meta', key);
export async function putMeta(key, value) {
  if(native){native.writeMeta(key,JSON.stringify(value));return;}
  const database = await db();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction('meta', 'readwrite');
    transaction.objectStore('meta').put(value, key);
    transaction.oncomplete = resolve; transaction.onabort = () => reject(transaction.error);
  });
}
export async function files() {
  if(native)return JSON.parse(native.files());
  const database = await db();
  return new Promise((resolve, reject) => {
    const request = database.transaction('files').objectStore('files').getAll();
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
}
export async function save(track, content) {
  if (content.size !== track.bytes) throw new Error('Файл скачался не полностью. Попробуй ещё раз.');
  const database = await db();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(['blobs', 'files'], 'readwrite');
    transaction.objectStore('blobs').put(content, track.md5);
    transaction.objectStore('files').put({md5: track.md5, title: track.title, artist: track.artist, bytes: content.size, id: track.id}, track.md5);
    transaction.oncomplete = resolve;
    transaction.onabort = () => reject(transaction.error || new Error('Не удалось сохранить файл.'));
  });
}
export async function remove(md5) {
  if(native){const {command}=await import('./native.mjs');await command('removeFile',{md5});return;}
  const database = await db();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(['blobs', 'files'], 'readwrite');
    for (const name of ['blobs', 'files']) transaction.objectStore(name).delete(md5);
    transaction.oncomplete = resolve; transaction.onabort = () => reject(transaction.error);
  });
}

export class Downloads {
  constructor({fetchTrack, existing, store, changed = () => {}}) {
    Object.assign(this, {fetchTrack, existing, store, changed});
    this.inflight = new Map(); this.pending = []; this.active = null; this.paused = false; this.completed = 0; this.total = 0;
  }
  add(tracks) {
    if (!this.active && !this.pending.length) { this.completed = 0; this.total = 0; this.error = null; }
    const known = new Set(this.pending.map(x => x.md5));
    if (this.active) known.add(this.active.md5);
    for (const track of tracks) if (!this.existing(track.md5) && !known.has(track.md5)) {
      known.add(track.md5); this.pending.push(track); this.total++;
    }
    this.paused = false; this.changed(); this.pump();
  }
  async one(track) {
    if (this.existing(track.md5)) return;
    if (this.inflight.has(track.md5)) return this.inflight.get(track.md5);
    const task = (async () => {
      const response = await this.fetchTrack(track);
      if (!response.ok) {
        let message = 'Не удалось скачать песню.';
        try { message = (await response.json()).error || message; } catch {}
        throw new Error(message);
      }
      const content = await response.blob();
      if (content.size !== track.bytes) throw new Error('Файл скачался не полностью.');
      await this.store(track, content);
    })().finally(() => this.inflight.delete(track.md5));
    this.inflight.set(track.md5, task); return task;
  }
  async pump() {
    if (this.active || this.paused) return;
    while (this.pending.length && !this.paused) {
      const track = this.pending.shift(); this.active = track; this.changed();
      try { await this.one(track); this.completed++; }
      catch (error) { this.pending.unshift(track); this.paused = true; this.error = error; }
      this.active = null; this.changed();
    }
  }
  toggle() { this.paused = !this.paused; this.error = null; this.changed(); if (!this.paused) this.pump(); }
  cancel() { this.pending = []; this.total = this.active ? 1 : 0; this.completed = 0; this.paused = true; this.changed(); }
}
