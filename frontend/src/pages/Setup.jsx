import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { setTokens } from '../api'
import { APP_NAME } from '../constants/branding'

export default function Setup() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const resp = await fetch('/api/auth/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
      if (!resp.ok) throw new Error((await resp.json()).detail || 'Setup failed')
      setTokens(await resp.json())
      navigate('/', { replace: true })
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <h1>{APP_NAME}</h1>
        <p className="tagline">Create the first account to get started.</p>
        <input
          type="email"
          name="email"
          id="setup-email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="username"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          inputMode="email"
          required
        />
        <input
          type="password"
          name="new-password"
          id="setup-password"
          placeholder="Password (8+ characters)"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          minLength={8}
          required
        />
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? 'Creating…' : 'Create account'}
        </button>
      </form>
    </div>
  )
}
