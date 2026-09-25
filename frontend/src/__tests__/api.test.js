import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The refresh path reaches for the identity cache to clear it; stub that so
// the assertions here are about tokens and refresh traffic only.
vi.mock('../session', () => ({ clearSession: vi.fn() }))

const OLD = { access_token: 'old', refresh_token: 'refresh-1' }
const FRESH = { access_token: 'new', refresh_token: 'refresh-2' }

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A fake server: anything with the old access token is 401, anything with
 * the new one is 200, and the refresh endpoint answers as configured. */
function fakeFetch({ refresh }) {
  const state = { refreshCalls: 0 }
  state.fetch = vi.fn(async (path, options = {}) => {
    if (path === '/api/auth/refresh') {
      state.refreshCalls += 1
      return refresh()
    }
    const auth = options.headers?.Authorization
    if (auth === `Bearer ${FRESH.access_token}`) return json(200, { ok: true, path })
    return json(401, { detail: 'expired' })
  })
  return state
}

describe('apiFetch token refresh', () => {
  let api
  let session

  beforeEach(async () => {
    vi.resetModules()
    localStorage.clear()
    api = await import('../api')
    session = await import('../session')
    session.clearSession.mockClear()
    api.setTokens(OLD)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('coalesces forty concurrent 401s into a single refresh', async () => {
    const server = fakeFetch({ refresh: () => json(200, FRESH) })
    vi.stubGlobal('fetch', server.fetch)

    const responses = await Promise.all(
      Array.from({ length: 40 }, (_, i) => api.apiFetch(`/api/thumb/${i}`)),
    )

    expect(server.refreshCalls).toBe(1)
    expect(responses.every((r) => r.ok)).toBe(true)
    expect(api.getTokens()).toEqual(FRESH)
    expect(session.clearSession).not.toHaveBeenCalled()
  })

  it('keeps the tokens when the refresh is rate-limited', async () => {
    const server = fakeFetch({ refresh: () => json(429, { detail: 'slow down' }) })
    vi.stubGlobal('fetch', server.fetch)

    await expect(api.apiFetch('/api/documents')).rejects.toThrow(/429/)

    expect(api.getTokens()).toEqual(OLD)
    expect(session.clearSession).not.toHaveBeenCalled()
  })

  it('ends the session when the refresh token itself is rejected', async () => {
    const server = fakeFetch({ refresh: () => json(401, { detail: 'revoked' }) })
    vi.stubGlobal('fetch', server.fetch)

    const resp = await api.apiFetch('/api/documents')

    expect(resp.status).toBe(401)
    expect(api.getTokens()).toBeNull()
    expect(session.clearSession).toHaveBeenCalledTimes(1)
  })

  it('discards a refresh that finishes after the session changed', async () => {
    let release
    let started
    const gate = new Promise((resolve) => {
      release = resolve
    })
    const refreshStarted = new Promise((resolve) => {
      started = resolve
    })
    const server = fakeFetch({
      refresh: async () => {
        started()
        await gate
        return json(200, FRESH)
      },
    })
    vi.stubGlobal('fetch', server.fetch)

    const pending = api.apiFetch('/api/documents')
    await refreshStarted
    // Someone else signs in on this browser while the refresh is in flight.
    const NEXT = { access_token: 'next', refresh_token: 'refresh-9' }
    api.setTokens(NEXT)
    release()
    await pending

    expect(api.getTokens()).toEqual(NEXT)
  })
})
