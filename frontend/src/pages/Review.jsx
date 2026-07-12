import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'
import DocumentDetails from '../components/DocumentDetails'
import Shell from '../components/Shell'
import Thumb from '../components/Thumb'

/**
 * Triage mode: deal untagged documents one at a time. Keyboard-first —
 * ← → move, X trashes, O opens. The details strip saves in place; a doc
 * leaves the pile the moment it gets a correspondent or type.
 */
export default function Review() {
  const [docs, setDocs] = useState(null)
  const [index, setIndex] = useState(0)
  const [error, setError] = useState('')
  const navigate = useNavigate()
  const indexRef = useRef(0)
  indexRef.current = index

  const load = useCallback(async () => {
    try {
      const data = await apiJson('/api/documents?needs_review=true&limit=200&sort=oldest')
      setDocs(data.items)
      setIndex((i) => Math.min(i, Math.max(0, data.items.length - 1)))
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const doc = docs?.[index]

  const trash = useCallback(async () => {
    const current = docs?.[indexRef.current]
    if (!current) return
    await apiFetch(`/api/documents/${current.id}`, { method: 'DELETE' })
    window.dispatchEvent(new Event('library-changed'))
    load()
  }, [docs, load])

  useEffect(() => {
    function onKey(e) {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      if (e.key === 'ArrowRight') setIndex((i) => Math.min(i + 1, (docs?.length || 1) - 1))
      else if (e.key === 'ArrowLeft') setIndex((i) => Math.max(i - 1, 0))
      else if (e.key === 'x' || e.key === 'X') trash()
      else if (e.key === 'o' || e.key === 'O') {
        const current = docs?.[indexRef.current]
        if (current) navigate(`/doc/${current.id}`)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [docs, trash, navigate])

  return (
    <Shell>
      <div className="library">
        <h1 className="page-title">Review</h1>
        {error && <p className="error">{error}</p>}
        {docs && docs.length === 0 && (
          <p className="settings-help">
            Nothing to review — every finished document has been filed.{' '}
            <Link to="/">Back to the library.</Link>
          </p>
        )}
        {doc && (
          <div className="review-card">
            <div className="review-nav">
              <button className="ghost" disabled={index === 0} onClick={() => setIndex(index - 1)}>
                ← Prev
              </button>
              <span className="settings-hint">
                {index + 1} of {docs.length} to file
              </span>
              <button
                className="ghost"
                disabled={index >= docs.length - 1}
                onClick={() => setIndex(index + 1)}
              >
                Next →
              </button>
              <span className="bulk-spacer" />
              <Link to={`/doc/${doc.id}`} className="button-link ghost-link">
                Open (O)
              </Link>
              <button className="ghost danger" onClick={trash}>
                Trash (X)
              </button>
            </div>
            <h2 className="review-title">{doc.title}</h2>
            <DocumentDetails doc={doc} onChange={load} />
            <div className="review-preview">
              <Thumb id={doc.id} className="review-thumb" />
            </div>
            <p className="settings-hint">
              Assign a From or Type and it leaves the pile. ← → to move, X to
              trash, O to open the full document.
            </p>
          </div>
        )}
      </div>
    </Shell>
  )
}
