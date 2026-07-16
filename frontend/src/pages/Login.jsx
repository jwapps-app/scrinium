import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { setTokens } from '../api'
import { APP_NAME, APP_TAGLINE } from '../constants/branding'

export default function Login() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [needsTotp, setNeedsTotp] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    fetch('/api/auth/status')
      .then((r) => r.json())
      .then((s) => {
        if (s.needs_setup) navigate('/setup', { replace: true })
      })
      .catch(() => {})
  }, [navigate])

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, ...(totp ? { totp } : {}) }),
      })
      if (!resp.ok) {
        const detail = (await resp.json()).detail || 'Login failed'
        if (detail === 'totp_required') {
          setNeedsTotp(true)
          setError('')
          return
        }
        throw new Error(detail)
      }
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
        <p className="tagline">{APP_TAGLINE}</p>
        <input
          type="email"
          name="email"
          id="login-email"
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
          name="password"
          id="login-password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
        {needsTotp && (
          <input
            type="text"
            name="one-time-code"
            id="login-totp"
            placeholder="6-digit code"
            value={totp}
            onChange={(e) => setTotp(e.target.value)}
            autoComplete="one-time-code"
            inputMode="numeric"
            pattern="[0-9 ]*"
            autoFocus
            required
          />
        )}
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
