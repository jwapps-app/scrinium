import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { apiDelete, apiJson } from '../api'
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

  // Settle whatever is being typed before the card changes documents. The
  // details strip saves on blur, so blurring here makes a half-typed note
  // land on the document it was written for; the tick lets that handler run
  // before the index moves and the strip remounts for the next document.
  const settleEdits = useCallback(async () => {
    const el = document.activeElement
    if (el && el !== document.body && typeof el.blur === 'function') el.blur()
    await new Promise((resolve) => setTimeout(resolve, 0))
  }, [])

  const move = useCallback(
    async (delta) => {
      await settleEdits()
      setIndex((i) => Math.min(Math.max(i + delta, 0), Math.max((docs?.length || 1) - 1, 0)))
    },
    [docs, settleEdits],
  )

  const trash = useCallback(async () => {
    const current = docs?.[indexRef.current]
    if (!current) return
    setError('')
    try {
      await apiDelete(`/api/documents/${current.id}`)
    } catch (err) {
      setError(`Could not trash "${current.title}": ${err.message}`)
      return
    }
    window.dispatchEvent(new Event('library-changed'))
    load()
  }, [docs, load])

  useEffect(() => {
    function onKey(e) {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      if (e.key === 'ArrowRight') move(1)
      else if (e.key === 'ArrowLeft') move(-1)
      else if (e.key === 'x' || e.key === 'X') trash()
      else if (e.key === 'o' || e.key === 'O') {
        const current = docs?.[indexRef.current]
        if (current) navigate(`/doc/${current.id}`)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [docs, trash, move, navigate])

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
              <button className="ghost" disabled={index === 0} onClick={() => move(-1)}>
                ← Prev
              </button>
              <span className="settings-hint">
                {index + 1} of {docs.length} to file
              </span>
              <button
                className="ghost"
                disabled={index >= docs.length - 1}
                onClick={() => move(1)}
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
            <ReviewReasons reasons={doc.review_reasons} />
            <DocumentDetails key={doc.id} doc={doc} onChange={load} />
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

/**
 * Why this document is here, and how much it matters.
 *
 * Most documents in this pile are simply unfiled, which is the normal state
 * of anything just uploaded — not a fault. Saying so explicitly is the point:
 * without it, "To review 3" sits next to "Needs attention" and reads like
 * three things broke.
 */
function ReviewReasons({ reasons }) {
  if (!reasons?.length) return null
  const routine = reasons.every((r) => r.severity === 'info')

  return (
    <div className="review-reasons">
      <div className="reason-chips">
        {reasons.map((r) => (
          <span key={r.key} className={`reason-chip ${r.severity}`}>
            {r.severity === 'problem' ? '! ' : ''}
            {r.label}
          </span>
        ))}
      </div>
      <ul className="reason-details">
        {reasons.map((r) => (
          <li key={r.key}>
            <strong>{r.label}:</strong> {r.detail}
          </li>
        ))}
      </ul>
      {routine && (
        <p className="reason-allclear">
          Nothing is wrong with this document — it just hasn’t been filed.
        </p>
      )}
    </div>
  )
}

