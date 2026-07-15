import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'
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

  useEffect(() => {
    apiJson(`/api/documents/${a}`)
      .then((d) => setTitles((t) => ({ ...t, a: d.title })))
      .catch(() => {})
    apiJson(`/api/documents/${b}`)
      .then((d) => setTitles((t) => ({ ...t, b: d.title })))
      .catch(() => {})
  }, [a, b])

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
    await apiFetch(`/api/documents/${id}`, { method: 'DELETE' })
    navigate('/insights')
  }
  async function notDupe() {
    await apiJson('/api/insights/duplicates/dismiss', {
      method: 'POST',
      body: JSON.stringify({ a, b }),
    })
    navigate('/insights')
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
