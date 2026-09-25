/**
 * Files handed to the app by the operating system's share sheet.
 *
 * The manifest's share_target POSTs to /share. The service worker (public/
 * sw.js) answers that POST: it parks the files in this cache and redirects to
 * /?shared=1. The library then takes them from here and uploads them through
 * the normal path. Nothing is uploaded until someone is signed in — the files
 * simply wait in the cache until the library next mounts.
 */

export const SHARE_CACHE = 'shared-files-v1'
const NAME_HEADER = 'X-Shared-Name'

let taking = null

/** Remove every parked file from the cache and return it as File objects.
 * One drain at a time: React's development double-mount would otherwise
 * read the same files twice and upload them twice. */
export function takeSharedFiles() {
  // Whoever asked first gets the files; anyone asking mid-drain gets none.
  if (taking) return taking.then(() => [])
  taking = (async () => {
    if (typeof caches === 'undefined') return []
    const cache = await caches.open(SHARE_CACHE)
    const keys = await cache.keys()
    const files = []
    for (const req of keys) {
      const resp = await cache.match(req)
      if (!resp) continue
      const name = decodeURIComponent(resp.headers.get(NAME_HEADER) || '') || 'shared'
      const type = resp.headers.get('Content-Type') || ''
      files.push(new File([await resp.blob()], name, { type }))
      await cache.delete(req)
    }
    return files
  })().finally(() => {
    taking = null
  })
  return taking
}
