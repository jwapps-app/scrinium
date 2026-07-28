import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'
import ComparePane from '../components/ComparePane'

/**
 * Review flow for documents whose OCR yielded very little text. Shows one scan
 * at a time and cycles through the worklist as you resolve each: Re-OCR (force
 * a fresh pass), Delete, Looks fine (dismiss so it stops resurfacing), or Skip.
 */
export default function WeakOcr() {
  const navigate = useNavigate()
  const [items, setItems] = useState(null)
  const [index, setIndex] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    apiJson('/api/insights/weak-ocr')
      .then((d) => setItems(d.items || []))
      .catch((e) => setError(e.message))
  }, [])

  const doc = items && items[index]

  // The whole point of this screen is judging OCR quality, which is impossible
  // from the page image alone: you have to see what the OCR actually read.
  const [ocrText, setOcrText] = useState(null)
  useEffect(() => {
    if (!doc) return
    let cancelled = false
    setOcrText(null)
    apiJson(`/api/documents/${doc.id}/text`)
      .then((d) => {
        if (!cancelled) setOcrText(d.text || '')
      })
      .catch(() => {
        if (!cancelled) setOcrText('')
      })
    return () => {
      cancelled = true
    }
  }, [doc?.id])

  async function act(fn) {
    if (busy || !doc) return
    setBusy(true)
    setError('')
    try {
      await fn(doc)
      setIndex((i) => i + 1) // straight to the next scan
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const reOcr = () =>
    act((d) =>
      apiJson(`/api/documents/${d.id}/reprocess`, {
        method: 'POST',
        body: JSON.stringify({ mode: 'force' }),
      }),
    )
  const remove = () =>
    act((d) => apiFetch(`/api/documents/${d.id}`, { method: 'DELETE' }))
  const dismiss = () =>
    act((d) =>
      apiJson('/api/insights/weak-ocr/dismiss', {
        method: 'POST',
        body: JSON.stringify({ id: d.id }),
      }),
    )
  const skip = () => act(() => Promise.resolve())

  if (items === null) {
    return (
      <div className="compare-view">
        <header className="compare-bar">
          <button className="ghost" onClick={() => navigate('/insights')}>
            ← Back
          </button>
        </header>
        <div className="compare-empty">Loading…</div>
      </div>
    )
  }

  if (!doc) {
    return (
      <div className="compare-view">
        <header className="compare-bar">
          <button className="ghost" onClick={() => navigate('/insights')}>
            ← Back to Insights
          </button>
        </header>
        <div className="compare-empty">
          <p>
            {items.length === 0
              ? 'No documents need review.'
              : 'All caught up — every weak scan has been reviewed.'}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="compare-view">
      <header className="compare-bar">
        <button className="ghost" onClick={() => navigate('/insights')}>
          ← Back
        </button>
        <span className="compare-count">
          {index + 1} of {items.length}
        </span>
        <div className="compare-acts">
          <button className="ghost" disabled={busy} onClick={reOcr}>
            Re-OCR
          </button>
          <button className="ghost" disabled={busy} onClick={dismiss}>
            Looks fine
          </button>
          <button className="ghost" disabled={busy} onClick={skip}>
            Skip
          </button>
          <button className="ghost danger" disabled={busy} onClick={remove}>
            Delete
          </button>
        </div>
      </header>
      <div className="compare-titles single">
        <span title={doc.title}>
          {doc.title} — {doc.chars_per_page} chars/page · {doc.pages} pages
          {doc.engine ? ` · ${doc.engine}` : ''}
        </span>
      </div>
      {error && <p className="error compare-error">{error}</p>}
      <div className="compare-panes">
        <ComparePane key={doc.id} docId={doc.id} />
        <div className="ocr-readout">
          <p className="settings-help">
            What the OCR read. Compare it with the page: gibberish, a handful of
            stray characters, or text that stops after the first page all mean a
            re-OCR is worth it. Readable text that simply happens to be sparse —
            a title page, a photo plate, a form — is fine as it is.
          </p>
          {ocrText === null ? (
            <p className="settings-help">Loading text…</p>
          ) : ocrText.trim() === '' ? (
            <p className="error">
              No text at all was extracted from this document.
            </p>
          ) : (
            <pre className="ocr-text">{ocrText}</pre>
          )}
        </div>
      </div>
    </div>
  )
}
