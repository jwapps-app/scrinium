import { useCallback, useEffect, useState } from 'react'
import { apiFetch, apiJson } from '../api'

/** Settings card: change password, manage accounts. */
export default function AccountSection() {
  const [users, setUsers] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pw, setPw] = useState({ current: '', next: '' })
  const [newUser, setNewUser] = useState({ email: '', password: '' })

  const load = useCallback(() => {
    apiJson('/api/auth/users').then(setUsers).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function changePassword(e) {
    e.preventDefault()
    setError('')
    setNotice('')
    try {
      await apiJson('/api/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: pw.current,
          new_password: pw.next,
        }),
      })
      setPw({ current: '', next: '' })
      setNotice('Password changed.')
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

      {users.length > 0 && (
        <ul className="rule-list">
          {users.map((u) => (
            <li key={u.id} className="rule-row">
              <div className="rule-main">
                <strong>{u.email}</strong>
                {u.is_me && <span className="rule-detail">this is you</span>}
              </div>
              {!u.is_me && (
                <button className="ghost danger" onClick={() => removeUser(u)}>
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

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
    </section>
  )
}
