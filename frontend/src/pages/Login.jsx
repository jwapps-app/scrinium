import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { setTokens } from '../api'
import { APP_NAME, APP_TAGLINE } from '../constants/branding'

export default function Login() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
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
        body: JSON.stringify({ email, password }),
      })
      if (!resp.ok) throw new Error((await resp.json()).detail || 'Login failed')
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
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
