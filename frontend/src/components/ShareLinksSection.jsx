import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'

/** Everything currently shared publicly — audit and revoke in one place. */
export default function ShareLinksSection() {
  const [links, setLinks] = useState([])
  const [error, setError] = useState('')

  const load = useCallback(() => {
    apiJson('/api/share-links').then(setLinks).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!error && links.length === 0) return null

  return (
    <section className="settings-section">
      <h2>Active share links</h2>
      {error && <p className="error">{error}</p>}
      <p className="settings-help">
        Anyone holding these URLs can view the document — this is the full
        list of what&apos;s currently exposed.
      </p>
      <ul className="rule-list">
        {links.map((l) => (
          <li key={l.id} className="rule-row">
            <div className="rule-main">
              <Link to={`/doc/${l.document_id}`}>
                <strong>{l.document_title}</strong>
              </Link>
              <span className="rule-detail">
                {l.expires_at
                  ? `expires ${new Date(l.expires_at).toLocaleDateString()}`
                  : 'never expires'}
              </span>
            </div>
            <button
              className="ghost"
              onClick={() => {
                navigator.clipboard
                  ?.writeText(`${window.location.origin}${l.url_path}`)
                  .catch(() => {})
              }}
            >
              Copy
            </button>
            <button
              className="ghost danger"
              onClick={async () => {
                await apiFetch(`/api/share-links/${l.id}`, { method: 'DELETE' })
                load()
              }}
            >
              Revoke
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
