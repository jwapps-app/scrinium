// App shell offline support. Two rules:
//  - hashed build assets: cache-first (immutable by name)
//  - navigations: network-first, falling back to the cached shell so the
//    PWA opens with the server unreachable (offline documents live in a
//    separate cache the app manages itself — see src/offline.js)
// /api requests are never touched here.
//
// One more job: the manifest's share_target POSTs shared files to /share.
// That POST is caught here, the files are parked in a cache the app reads
// (src/share.js), and the browser is sent on to /?shared=1 — the app shell —
// where the library uploads them through its normal path.
const SHELL_CACHE = 'app-shell-v1'
const SHARE_CACHE = 'shared-files-v1'

async function parkSharedFiles(request) {
  const form = await request.formData()
  const cache = await caches.open(SHARE_CACHE)
  const stamp = Date.now()
  let n = 0
  for (const entry of form.getAll('file')) {
    if (!entry || typeof entry.arrayBuffer !== 'function') continue // a plain field, not a file
    n += 1
    await cache.put(
      `/shared/files/${stamp}-${n}`,
      new Response(entry, {
        headers: {
          'Content-Type': entry.type || 'application/octet-stream',
          'X-Shared-Name': encodeURIComponent(entry.name || ''),
        },
      }),
    )
  }
}

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

  // Exactly POST /share: public links are GET /share/<token> and must keep
  // reaching the app as ordinary navigations.
  if (event.request.method === 'POST' && url.pathname === '/share') {
    event.respondWith(
      (async () => {
        try {
          await parkSharedFiles(event.request)
        } catch {
          /* unreadable form: nothing to park, still land in the app */
        }
        // 303 so the browser follows with a GET rather than replaying the POST.
        return Response.redirect('/?shared=1', 303)
      })(),
    )
    return
  }

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
