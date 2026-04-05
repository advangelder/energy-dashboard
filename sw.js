const CACHE = 'energy-dashboard-v2';
const ASSETS = [
  '/',
  '/index.html',
  '/manifest.json'
];

self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(ASSETS))
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Only cache same-origin navigation/asset requests; let API calls through
  if (url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
