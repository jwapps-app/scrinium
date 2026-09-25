/**
 * Offline document store, built on the Cache API.
 *
 * "Keep offline" fetches the document's file (with auth) and stores the
 * response — plus a small metadata record — under synthetic /offline/ URLs
 * in a dedicated cache. Reading needs no service-worker interception: the
 * viewer falls back to caches.match() when the network is gone. The service
 * worker's only job is keeping the app shell loadable offline.
 */
import { apiFetch, apiJson, isTextNative } from './api'

const CACHE = 'offline-docs-v1'

const fileKey = (id) => `/offline/docs/${id}/file`
const metaKey = (id) => `/offline/docs/${id}/meta`
const textKey = (id) => `/offline/docs/${id}/text`

export async function keepOffline(doc) {
  const cache = await caches.open(CACHE)
  const resp = await apiFetch(`/api/documents/${doc.id}/file`)
  if (!resp.ok) throw new Error('Could not fetch the file')
  await cache.put(fileKey(doc.id), resp.clone())
  // A text-native document (EPUB, DOCX…) is read from its extracted text,
  // not from the original bytes, so that has to be saved too.
  if (isTextNative(doc)) {
    const { text } = await apiJson(`/api/documents/${doc.id}/text`)
    await cache.put(
      textKey(doc.id),
      new Response(JSON.stringify({ text: text || '' }), {
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  }
  // The viewer decides how to open a document from has_archive and the
  // original filename; without them a cached copy could never be shown.
  const meta = {
    id: doc.id,
    title: doc.title,
    page_count: doc.page_count,
    has_archive: Boolean(doc.has_archive),
    original_filename: doc.original_filename || null,
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
  await cache.delete(textKey(id))
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

/** Extracted reader text for a text-native document, or null if none saved. */
export async function offlineText(id) {
  const cache = await caches.open(CACHE)
  const resp = await cache.match(textKey(id))
  if (!resp) return null
  const { text } = await resp.json()
  return text ?? ''
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

/** Drop every offline copy. Called on sign-out and when a different account
 * signs in: the cache is origin-scoped and carries no notion of who saved
 * it, so anything left behind would be readable by the next account to sign
 * in on this browser. */
export async function clearOffline() {
  await caches.delete(CACHE)
}
