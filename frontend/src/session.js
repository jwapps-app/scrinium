/**
 * Who is signed in. Fetched once and shared, so every component can ask
 * whether to render owner-only controls without each one hitting /auth/me.
 *
 * The server enforces the rules regardless — this only decides what to show,
 * so a member sees a coherent UI instead of buttons that answer 403.
 */
import { useEffect, useState } from 'react'
import { apiJson } from './api'

// The last account this browser loaded an identity for. Offline copies
// survive a token expiry, so this is how the next sign-in can tell whether
// it is the same person coming back or someone else who must not see them.
const LAST_USER_KEY = 'session_user_id'

let cached = null
let inFlight = null
// Bumped by clearSession so an identity request already on the wire cannot
// repopulate the cache after the session it belonged to is gone.
let generation = 0
const listeners = new Set()

function publish() {
  for (const fn of listeners) fn(cached)
}

async function reconcileOfflineOwner(me) {
  let previous = null
  try {
    previous = localStorage.getItem(LAST_USER_KEY)
  } catch {
    return
  }
  if (previous && previous !== me.id) {
    try {
      const { clearOffline } = await import('./offline')
      await clearOffline()
      for (const key of Object.keys(localStorage)) {
        if (key.startsWith('readpos:')) localStorage.removeItem(key)
      }
    } catch {
      /* cache API unavailable */
    }
  }
  try {
    localStorage.setItem(LAST_USER_KEY, me.id)
  } catch {
    /* storage disabled */
  }
}

export async function loadSession(force = false) {
  if (cached && !force) return cached
  if (!inFlight) {
    const mine = generation
    inFlight = apiJson('/api/auth/me')
      .then(async (me) => {
        if (mine !== generation) return null
        await reconcileOfflineOwner(me)
        if (mine !== generation) return null
        cached = me
        publish()
        return me
      })
      .catch(() => null)
      .finally(() => {
        if (mine === generation) inFlight = null
      })
  }
  return inFlight
}

/** Drop the cached identity — call on sign-out or when the session expires
 * so the next user starts clean. Anything still loading is discarded too. */
export function clearSession() {
  generation += 1
  cached = null
  inFlight = null
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
