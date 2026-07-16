import { useEffect, useState } from 'react'
import { apiFetch } from '../api'

// Thumbnails need the auth header, so <img src> can't hit the API directly;
// fetch once per document and cache the object URL for the session.
const cache = new Map()

export default function Thumb({ id, className = '' }) {
  const [url, setUrl] = useState(cache.get(id) || null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    // Reset per-id state: a reused <Thumb> (no key) must not carry the
    // previous document's image or a sticky failed flag to the next one.
    setFailed(false)
    setUrl(cache.get(id) || null)
    if (cache.has(id)) {
      return
    }
    let cancelled = false
    apiFetch(`/api/documents/${id}/thumbnail`)
      .then((resp) => (resp.ok ? resp.blob() : Promise.reject(new Error('none'))))
      .then((blob) => {
        const objectUrl = URL.createObjectURL(blob)
        cache.set(id, objectUrl)
        if (!cancelled) setUrl(objectUrl)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [id])

  if (failed) return <div className={`thumb-fallback ${className}`}>PDF</div>
  if (!url) return <div className={`thumb-loading ${className}`} />
  return <img src={url} className={className} alt="" loading="lazy" />
}

export function invalidateThumb(id) {
  const url = cache.get(id)
  if (url) URL.revokeObjectURL(url)
  cache.delete(id)
}
