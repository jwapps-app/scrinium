/**
 * Who is signed in. Fetched once and shared, so every component can ask
 * whether to render owner-only controls without each one hitting /auth/me.
 *
 * The server enforces the rules regardless — this only decides what to show,
 * so a member sees a coherent UI instead of buttons that answer 403.
 */
import { useEffect, useState } from 'react'
import { apiJson } from './api'

let cached = null
let inFlight = null
const listeners = new Set()

function publish() {
  for (const fn of listeners) fn(cached)
}

export async function loadSession(force = false) {
  if (cached && !force) return cached
  if (!inFlight) {
    inFlight = apiJson('/api/auth/me')
      .then((me) => {
        cached = me
        publish()
        return me
      })
      .catch(() => null)
      .finally(() => {
        inFlight = null
      })
  }
  return inFlight
}

/** Drop the cached identity — call on sign-out so the next user starts clean. */
export function clearSession() {
  cached = null
  publish()
}

export function useSession() {
  const [me, setMe] = useState(cached)
  useEffect(() => {
    listeners.add(setMe)
    loadSession()
    return () => listeners.delete(setMe)
  }, [])
  return me
}

/** True only once the identity is known and says owner — defaults to hiding
 * owner-only controls while loading rather than flashing them. */
export function useIsAdmin() {
  return useSession()?.is_admin === true
}
