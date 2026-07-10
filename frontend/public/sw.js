// Minimal service worker: enough for installability. Offline caching and the
// share_target ingest handler come with the mobile-capture milestone.
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
self.addEventListener('fetch', () => {})
