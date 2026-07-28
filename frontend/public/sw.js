// App shell offline support. Two rules:
//  - hashed build assets: cache-first (immutable by name)
//  - navigations: network-first, falling back to the cached shell so the
//    PWA opens with the server unreachable (offline documents live in a
//    separate cache the app manages itself — see src/offline.js)
// /api requests are never touched here.
const SHELL_CACHE = 'app-shell-v1'

self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys()
      await Promise.all(
        keys
          .filter((k) => k.startsWith('app-shell-') && k !== SHELL_CACHE)
          .map((k) => caches.delete(k)),
      )
      await self.clients.claim()
    })(),
  )
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  if (url.origin !== location.origin) return
  if (url.pathname.startsWith('/api/')) return

  if (event.request.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(event.request)
          // Only a good response becomes the offline shell: a 502 from nginx
          // during an api restart used to be stored and then served offline
          // until the next successful load replaced it.
          if (fresh.ok && fresh.type === 'basic') {
            const cache = await caches.open(SHELL_CACHE)
            cache.put('/', fresh.clone())
          }
          return fresh
        } catch {
          const cached = await caches.match('/')
          if (cached) return cached
          throw new Error('offline, no cached shell')
        }
      })(),
    )
    return
  }

  if (url.pathname.startsWith('/assets/') || url.pathname === '/manifest.webmanifest') {
    event.respondWith(
      (async () => {
        const cached = await caches.match(event.request)
        if (cached) return cached
        const fresh = await fetch(event.request)
        const cache = await caches.open(SHELL_CACHE)
        cache.put(event.request, fresh.clone())
        return fresh
      })(),
    )
  }
})
