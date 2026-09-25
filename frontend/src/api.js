const STORAGE_KEY = 'auth_tokens'

// Bumped on every login/logout. A refresh that started under an older
// session must not write its result over a newer one, so refreshes compare
// this before storing anything.
let sessionGeneration = 0
let refreshInFlight = null

export function getTokens() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || null
  } catch {
    return null
  }
}

function storeTokens(tokens) {
  if (tokens) localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
  else localStorage.removeItem(STORAGE_KEY)
  window.dispatchEvent(new Event('auth-changed'))
}

/** Login/logout: a new session begins, and anything still in flight for the
 * old one is discarded. Token refreshes go through storeTokens instead — a
 * refresh continues the same session rather than starting another. */
export function setTokens(tokens) {
  sessionGeneration += 1
  storeTokens(tokens)
}

/** Sign out for real: revoke server-side, then drop everything this browser
 * holds. Clearing the offline cache matters — it keeps whole PDFs and their
 * titles under origin-scoped keys, so leaving it behind exposed one account's
 * documents to the next person to sign in on the same browser. */
export async function signOut() {
  try {
    await apiFetch('/api/auth/logout', { method: 'POST' })
  } catch {
    /* offline or already invalid — still clear locally */
  }
  try {
    const { clearSession } = await import('./session')
    clearSession()
  } catch {
    /* no session cached */
  }
  try {
    const { clearOffline } = await import('./offline')
    await clearOffline()
  } catch {
    /* cache API unavailable */
  }
  try {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith('readpos:')) localStorage.removeItem(key)
    }
  } catch {
    /* storage disabled */
  }
  setTokens(null)
}

/** The session ended without the user asking (refresh token expired or
 * revoked). Same identity cleanup as sign-out, but the offline copies stay:
 * an expired token is no reason to lose documents someone deliberately saved
 * to read without the server. If a different account signs in next, the
 * session module notices the identity change and clears them then. */
async function authLost() {
  try {
    const { clearSession } = await import('./session')
    clearSession()
  } catch {
    /* no session cached */
  }
  setTokens(null)
}

/** One refresh at a time. Every request that meets a 401 while one is in
 * flight waits on the same promise — a page of thumbnails expiring together
 * used to fire one refresh per thumbnail and trip the server's rate limit,
 * and the resulting 429 was then treated as a sign-out. */
function refreshTokens() {
  if (refreshInFlight) return refreshInFlight
  const tokens = getTokens()
  if (!tokens?.refresh_token) return Promise.resolve(null)
  const generation = sessionGeneration
  refreshInFlight = (async () => {
    const resp = await fetch('/api/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: tokens.refresh_token }),
    })
    if (generation !== sessionGeneration) return null
    if (resp.status === 401 || resp.status === 403) {
      // The refresh token itself is invalid or revoked: the session is over.
      await authLost()
      return null
    }
    if (!resp.ok) {
      // Rate limit, gateway error, restart: transient. Keep the tokens and
      // let the caller's request fail — the next one will try again.
      const err = new Error(`Could not refresh the session (${resp.status})`)
      err.status = resp.status
      err.fromServer = true
      throw err
    }
    const fresh = await resp.json()
    if (generation !== sessionGeneration) return null
    storeTokens(fresh)
    return fresh
  })().finally(() => {
    refreshInFlight = null
  })
  return refreshInFlight
}

export async function apiFetch(path, options = {}, retry = true) {
  const tokens = getTokens()
  const headers = { ...(options.headers || {}) }
  if (tokens?.access_token) headers.Authorization = `Bearer ${tokens.access_token}`
  const resp = await fetch(path, { ...options, headers })
  if (resp.status === 401 && retry) {
    const fresh = await refreshTokens()
    if (fresh) return apiFetch(path, options, false)
  }
  return resp
}


/** DELETE that actually reports failure — apiFetch returns error responses
 * as-is, and several callers were treating any response as success. */
export async function apiDelete(path) {
  const resp = await apiFetch(path, { method: 'DELETE' })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      detail = (await resp.json()).detail || detail
    } catch {
      /* not json */
    }
    throw new Error(detail)
  }
}

export async function apiJson(path, options = {}) {
  const resp = await apiFetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      detail = (await resp.json()).detail || detail
    } catch {
      /* not json */
    }
    const err = new Error(detail)
    // The server answered — callers must not treat this as "offline".
    err.fromServer = true
    err.status = resp.status
    throw err
  }
  if (resp.status === 204) return null
  return resp.json()
}

const TEXT_SUFFIXES = ['.txt', '.md', '.epub', '.docx', '.xlsx', '.pptx', '.odt']
export function isTextNative(doc) {
  if (!doc?.original_filename || doc.has_archive) return false
  const name = doc.original_filename.toLowerCase()
  return TEXT_SUFFIXES.some((s) => name.endsWith(s))
}
