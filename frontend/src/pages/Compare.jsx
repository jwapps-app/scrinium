import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { apiDelete, apiJson } from '../api'
import ComparePane from '../components/ComparePane'

/**
 * Side-by-side comparison of two documents (built for reviewing near-duplicate
 * pairs). Scrolling can be locked so both panes move together by a fixed page
 * offset; unlock to realign when one side has missing/extra pages, then re-lock
 * to capture the new offset and continue in step.
 */
export default function Compare() {
  const { a, b } = useParams()
  const navigate = useNavigate()
  const aRef = useRef(null)
  const bRef = useRef(null)
  const offsetRef = useRef(0) // B.page - A.page, captured when locking
  const [locked, setLocked] = useState(true)
  const [titles, setTitles] = useState({ a: '', b: '' })
  const [remaining, setRemaining] = useState(null)

  useEffect(() => {
    offsetRef.current = 0 // fresh pair — no page offset until you re-lock
    apiJson(`/api/documents/${a}`)
      .then((d) => setTitles((t) => ({ ...t, a: d.title })))
      .catch(() => setTitles((t) => ({ ...t, a: 'Document' })))
    apiJson(`/api/documents/${b}`)
      .then((d) => setTitles((t) => ({ ...t, b: d.title })))
      .catch(() => setTitles((t) => ({ ...t, b: 'Document' })))
  }, [a, b])

  useEffect(() => {
    apiJson('/api/insights/duplicates')
      .then((d) => setRemaining((d.pairs || []).length))
      .catch(() => {})
  }, [])

  // After resolving a pair, jump straight to the next one so you can cycle
  // through the whole set without returning to Insights each time.
  async function advance() {
    let data
    try {
      data = await apiJson('/api/insights/duplicates')
    } catch (err) {
      // Never bounce to Insights on failure: that is indistinguishable from
      // "nothing left to review", so a slow or erroring duplicates scan looked
      // exactly like finishing the queue. Stay put and say what broke.
      window.alert(`Could not load the next duplicate pair: ${err.message}`)
      return
    }
    const pairs = data.pairs || []
    // Skip only the pair just resolved, not every pair touching either of its
    // documents: duplicates come in clusters (three copies of a book yield
    // A/B, A/C, B/C), and excluding both ids dropped the whole rest of the
    // cluster. The server already filters what's resolved — dismissed pairs
    // and trashed documents.
    const next = pairs.find((p) => {
      const ids = [p.a.id, p.b.id]
      return !(ids.includes(a) && ids.includes(b))
    })
    setRemaining(pairs.length)
    if (next) {
      navigate(`/compare/${next.a.id}/${next.b.id}`, { replace: true })
    } else {
      navigate('/insights')
    }
  }

  const drive = useCallback(
    (fromRef, toRef, sign) => {
      if (!locked) return
      const from = fromRef.current
      const to = toRef.current
      if (!from || !to) return
      const pos = from.getPosition()
      // The driven pane swallows this programmatic scroll, so there's no echo.
      to.scrollToPosition({
        page: pos.page + sign * offsetRef.current,
        fraction: pos.fraction,
      })
    },
    [locked],
  )

  const onScrollA = useCallback(() => drive(aRef, bRef, +1), [drive])
  const onScrollB = useCallback(() => drive(bRef, aRef, -1), [drive])

  function toggleLock() {
    if (!locked && aRef.current && bRef.current) {
      // Capture the current page difference so we resume from where you aligned.
      offsetRef.current = bRef.current.getPosition().page - aRef.current.getPosition().page
    }
    setLocked((v) => !v)
  }

  async function trash(id) {
    try {
      await apiDelete(`/api/documents/${id}`)
    } catch (err) {
      window.alert(`Could not move to trash: ${err.message}`)
      return
    }
    await advance()
  }
  async function notDupe() {
    try {
      await apiJson('/api/insights/duplicates/dismiss', {
        method: 'POST',
        body: JSON.stringify({ a, b }),
      })
    } catch (err) {
      window.alert(`Could not dismiss this pair: ${err.message}`)
      return
    }
    await advance()
  }

  return (
    <div className="compare-view">
      <header className="compare-bar">
        <button className="ghost" onClick={() => navigate('/insights')}>
          ← Back
        </button>
        <button
          className={locked ? 'compare-lock is-on' : 'compare-lock'}
          onClick={toggleLock}
          title={
            locked
              ? 'Scrolling is locked together — unlock to realign missing pages'
              : 'Scroll each side freely to align, then lock to scroll together'
          }
        >
          {locked ? '🔒 Scroll locked' : '🔓 Scroll free'}
        </button>
        {remaining != null && (
          <span className="compare-count">{remaining} to review</span>
        )}
        <div className="compare-acts">
          <button className="ghost" onClick={() => trash(a)}>
            Trash left
          </button>
          <button className="ghost" onClick={() => trash(b)}>
            Trash right
          </button>
          <button className="ghost" onClick={notDupe}>
            Not a dupe
          </button>
        </div>
      </header>
      <div className="compare-titles">
        <span title={titles.a}>{titles.a || '…'}</span>
        <span title={titles.b}>{titles.b || '…'}</span>
      </div>
      <div className="compare-panes">
        <ComparePane ref={aRef} docId={a} onScroll={onScrollA} />
        <ComparePane ref={bRef} docId={b} onScroll={onScrollB} />
      </div>
    </div>
  )
}
