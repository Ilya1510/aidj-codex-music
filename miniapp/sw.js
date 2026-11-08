const CACHE = 'aidj-shell-v27';
const SHELL = ['/', '/index.html', '/app.css', '/app.mjs', '/native.mjs', '/playlist-changes.mjs', '/transfer.mjs', '/segments.mjs', '/playback.mjs', '/dj.mjs', '/ratings.mjs', '/reorder.mjs', '/player.mjs', '/storage.mjs', '/icon.svg', '/manifest.webmanifest', '/telegram-web-app.js'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))));
self.addEventListener('activate', event => event.waitUntil((async () => {
  for (const key of await caches.keys()) if (key.startsWith('aidj-shell-') && key !== CACHE) await caches.delete(key);
  await self.clients.claim();
})()));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).then(response => {
      if (response.ok) { const copy = response.clone(); event.waitUntil(caches.open(CACHE).then(cache => cache.put('/', copy))); }
      return response.ok ? response : caches.match('/').then(cached => cached || response);
    }).catch(() => caches.match('/')));
  } else if (SHELL.includes(url.pathname)) {
    event.respondWith(fetch(event.request).then(response => {
      if (response.ok) { const copy = response.clone(); event.waitUntil(caches.open(CACHE).then(cache => cache.put(url.pathname, copy))); }
      return response.ok ? response : caches.match(url.pathname).then(cached => cached || response);
    }).catch(() => caches.match(url.pathname)));
  }
});
