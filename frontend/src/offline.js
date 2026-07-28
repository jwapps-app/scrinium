/**
 * Offline document store, built on the Cache API.
 *
 * "Keep offline" fetches the document's file (with auth) and stores the
 * response — plus a small metadata record — under synthetic /offline/ URLs
 * in a dedicated cache. Reading needs no service-worker interception: the
 * viewer falls back to caches.match() when the network is gone. The service
 * worker's only job is keeping the app shell loadable offline.
 */
import { apiFetch } from './api'

const CACHE = 'offline-docs-v1'

const fileKey = (id) => `/offline/docs/${id}/file`
const metaKey = (id) => `/offline/docs/${id}/meta`

export async function keepOffline(doc) {
  const cache = await caches.open(CACHE)
  const resp = await apiFetch(`/api/documents/${doc.id}/file`)
  if (!resp.ok) throw new Error('Could not fetch the file')
  await cache.put(fileKey(doc.id), resp.clone())
  const meta = {
    id: doc.id,
    title: doc.title,
    page_count: doc.page_count,
    saved_at: new Date().toISOString(),
  }
  await cache.put(
    metaKey(doc.id),
    new Response(JSON.stringify(meta), {
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

export async function removeOffline(id) {
  const cache = await caches.open(CACHE)
  await cache.delete(fileKey(id))
  await cache.delete(metaKey(id))
}

export async function isOffline(id) {
  const cache = await caches.open(CACHE)
  return (await cache.match(metaKey(id))) !== undefined
}

export async function offlineFile(id) {
  const cache = await caches.open(CACHE)
  return cache.match(fileKey(id))
}

export async function offlineMeta(id) {
  const cache = await caches.open(CACHE)
  const resp = await cache.match(metaKey(id))
  return resp ? resp.json() : null
}

export async function listOffline() {
  const cache = await caches.open(CACHE)
  const keys = await cache.keys()
  const docs = []
  for (const req of keys) {
    if (req.url.endsWith('/meta')) {
      const resp = await cache.match(req)
      if (resp) docs.push(await resp.json())
    }
  }
  return docs.sort((a, b) => (a.title || '').localeCompare(b.title || ''))
}

/** Drop every offline copy. Called on sign-out: the cache is origin-scoped and
 * carries no notion of who saved it, so anything left behind would be readable
 * by the next account to sign in on this browser. */
export async function clearOffline() {
  await caches.delete(CACHE)
}
