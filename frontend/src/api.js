const STORAGE_KEY = 'auth_tokens'

export function getTokens() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || null
  } catch {
    return null
  }
}

export function setTokens(tokens) {
  if (tokens) localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
  else localStorage.removeItem(STORAGE_KEY)
  window.dispatchEvent(new Event('auth-changed'))
}

async function refreshTokens() {
  const tokens = getTokens()
  if (!tokens?.refresh_token) return null
  const resp = await fetch('/api/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: tokens.refresh_token }),
  })
  if (!resp.ok) {
    setTokens(null)
    return null
  }
  const fresh = await resp.json()
  setTokens(fresh)
  return fresh
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
    throw new Error(detail)
  }
  if (resp.status === 204) return null
  return resp.json()
}
