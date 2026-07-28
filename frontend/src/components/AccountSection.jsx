import { useCallback, useEffect, useState } from 'react'
import QRCode from 'qrcode'
import { apiFetch, apiJson, setTokens } from '../api'
import { useIsAdmin } from '../session'

/** Settings card: change password, manage accounts. */
export default function AccountSection() {
  const [users, setUsers] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pw, setPw] = useState({ current: '', next: '' })
  const [newUser, setNewUser] = useState({ email: '', password: '' })
  const [totpEnabled, setTotpEnabled] = useState(null)
  const [enroll, setEnroll] = useState(null) // {secret, otpauth_url, qr}
  const [enrollCode, setEnrollCode] = useState('')
  const [disable, setDisable] = useState({ password: '', code: '' })
  const [showDisable, setShowDisable] = useState(false)
  // Account management is owner-only server-side; hide it for everyone else
  // rather than offering buttons that come back 403.
  const isAdmin = useIsAdmin()

  const load = useCallback(() => {
    apiJson('/api/auth/users').then(setUsers).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
    apiJson('/api/auth/totp').then((d) => setTotpEnabled(d.enabled)).catch(() => {})
  }, [load])

  async function startEnroll() {
    setError('')
    try {
      const data = await apiJson('/api/auth/totp/setup', { method: 'POST' })
      const qr = await QRCode.toDataURL(data.otpauth_url, { width: 220, margin: 1 })
      setEnroll({ ...data, qr })
    } catch (err) {
      setError(err.message)
    }
  }

  async function confirmEnroll(e) {
    e.preventDefault()
    setError('')
    try {
      await apiJson('/api/auth/totp/enable', {
        method: 'POST',
        body: JSON.stringify({ code: enrollCode }),
      })
      setEnroll(null)
      setEnrollCode('')
      setTotpEnabled(true)
      setNotice('Two-factor is on. You will need a code at every sign-in.')
    } catch (err) {
      setError(err.message)
    }
  }

  async function confirmDisable(e) {
    e.preventDefault()
    setError('')
    try {
      await apiJson('/api/auth/totp/disable', {
        method: 'POST',
        body: JSON.stringify({ password: disable.password, code: disable.code }),
      })
      setShowDisable(false)
      setDisable({ password: '', code: '' })
      setTotpEnabled(false)
      setNotice('Two-factor is off.')
    } catch (err) {
      setError(err.message)
    }
  }

  async function changePassword(e) {
    e.preventDefault()
    setError('')
    setNotice('')
    try {
      const result = await apiJson('/api/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: pw.current,
          new_password: pw.next,
        }),
      })
      // The server invalidates all older tokens on password change and
      // returns a fresh pair — adopt it so this session continues.
      if (result?.access_token) {
        setTokens({
          access_token: result.access_token,
          refresh_token: result.refresh_token,
        })
      }
      setPw({ current: '', next: '' })
      setNotice('Password changed. Other signed-in devices will need to sign in again.')
    } catch (err) {
      setError(err.message)
    }
  }

  async function addUser(e) {
    e.preventDefault()
    setError('')
    setNotice('')
    try {
      await apiJson('/api/auth/users', {
        method: 'POST',
        body: JSON.stringify(newUser),
      })
      setNewUser({ email: '', password: '' })
      setNotice('Account created.')
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function removeUser(u) {
    if (!window.confirm(`Remove the account ${u.email}?`)) return
    try {
      await apiFetch(`/api/auth/users/${u.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <section className="settings-section">
      <h2>Account</h2>
      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      <form className="rule-form" onSubmit={changePassword}>
        <div className="rule-form-row">
          <input
            type="password"
            placeholder="Current password"
            autoComplete="current-password"
            value={pw.current}
            onChange={(e) => setPw({ ...pw, current: e.target.value })}
            required
          />
          <input
            type="password"
            placeholder="New password (8+ characters)"
            autoComplete="new-password"
            minLength={8}
            value={pw.next}
            onChange={(e) => setPw({ ...pw, next: e.target.value })}
            required
          />
          <button type="submit">Change password</button>
        </div>
      </form>

      <div className="organize-block">
        <div className="organize-head">
          <strong>Two-factor authentication</strong>
          {totpEnabled === false && !enroll && (
            <button className="ghost" onClick={startEnroll}>
              Enable
            </button>
          )}
          {totpEnabled === true && !showDisable && (
            <button className="ghost danger" onClick={() => setShowDisable(true)}>
              Disable
            </button>
          )}
        </div>
        <p className="settings-help">
          {totpEnabled
            ? 'On — sign-in requires a code from your authenticator app.'
            : 'Add a 6-digit code from an authenticator app (1Password, Apple Passwords, Google Authenticator…) to every sign-in. If the device is ever lost, two-factor can only be removed with direct database access.'}
        </p>
        {enroll && (
          <form className="rule-form" onSubmit={confirmEnroll}>
            <img src={enroll.qr} alt="TOTP QR code" className="totp-qr" />
            <p className="settings-help">
              Scan with your authenticator, or enter the key manually:{' '}
              <code>{enroll.secret}</code>
            </p>
            <div className="rule-form-row">
              <input
                placeholder="6-digit code from the app"
                value={enrollCode}
                onChange={(e) => setEnrollCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                required
              />
              <button type="submit">Turn on</button>
              <button type="button" className="ghost" onClick={() => setEnroll(null)}>
                Cancel
              </button>
            </div>
          </form>
        )}
        {showDisable && (
          <form className="rule-form" onSubmit={confirmDisable}>
            <div className="rule-form-row">
              <input
                type="password"
                placeholder="Password"
                value={disable.password}
                onChange={(e) => setDisable({ ...disable, password: e.target.value })}
                required
              />
              <input
                placeholder="Current 6-digit code"
                value={disable.code}
                onChange={(e) => setDisable({ ...disable, code: e.target.value })}
                inputMode="numeric"
                required
              />
              <button type="submit" className="ghost danger">
                Turn off
              </button>
            </div>
          </form>
        )}
      </div>

      {users.length > 0 && (
        <ul className="rule-list">
          {users.map((u) => (
            <li key={u.id} className="rule-row">
              <div className="rule-main">
                <strong>{u.email}</strong>
                <span className="rule-detail">
                  {[u.is_me && 'this is you', u.is_admin && 'owner']
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </div>
              {isAdmin && !u.is_me && (
                <button className="ghost danger" onClick={() => removeUser(u)}>
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {isAdmin && (
      <form className="rule-form" onSubmit={addUser}>
        <div className="rule-form-row">
          <input
            type="email"
            placeholder="Email for a new account"
            value={newUser.email}
            onChange={(e) => setNewUser({ ...newUser, email: e.target.value })}
            required
          />
          <input
            type="password"
            placeholder="Their password (8+ characters)"
            autoComplete="off"
            minLength={8}
            value={newUser.password}
            onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
            required
          />
          <button type="submit" className="ghost">
            Add account
          </button>
        </div>
      </form>
      )}
    </section>
  )
}
