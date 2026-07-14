import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { apiJson, setTokens } from '../api'
import { APP_NAME } from '../constants/branding'
import ProgressBar, { formatEta } from './ProgressBar'

// Order tags as a tree: parents first, children indented beneath them.
// `collapsed` (a Set of tag ids) prunes the descendants of collapsed nodes;
// each item is flagged `hasChildren` so the UI can show a disclosure caret.
export function flattenTagTree(tags, collapsed = null) {
  const byParent = new Map()
  const ids = new Set(tags.map((t) => t.id))
  for (const tag of tags) {
    // Treat tags with a missing parent (filtered out, race) as roots.
    const key = tag.parent_id && ids.has(tag.parent_id) ? tag.parent_id : 'root'
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key).push(tag)
  }
  const out = []
  const walk = (key, depth) => {
    for (const tag of byParent.get(key) || []) {
      const hasChildren = byParent.has(tag.id)
      out.push({ ...tag, depth, hasChildren })
      if (hasChildren && !(collapsed && collapsed.has(tag.id))) {
        walk(tag.id, depth + 1)
      }
    }
  }
  walk('root', 0)
  return out
}

const SHORTCUTS = [
  ['/', 'Focus search'],
  ['g then l', 'Library'],
  ['g then r', 'Review'],
  ['g then i', 'Insights'],
  ['?', 'This help'],
]

export default function Shell({ children }) {
  const [stats, setStats] = useState(null)
  const [tags, setTags] = useState([])
  const [views, setViews] = useState([])
  const [correspondents, setCorrespondents] = useState([])
  const [showHelp, setShowHelp] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    let prefix = null
    let prefixTimer = null
    function onKey(e) {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === '/') {
        e.preventDefault()
        const search = document.querySelector('.searchbar input')
        if (search) search.focus()
        else navigate('/')
      } else if (e.key === '?') {
        setShowHelp((v) => !v)
      } else if (e.key === 'Escape') {
        setShowHelp(false)
      } else if (prefix === 'g') {
        prefix = null
        if (e.key === 'l') navigate('/')
        else if (e.key === 'r') navigate('/review')
        else if (e.key === 'i') navigate('/insights')
        else if (e.key === 's') navigate('/settings')
      } else if (e.key === 'g') {
        prefix = 'g'
        clearTimeout(prefixTimer)
        prefixTimer = setTimeout(() => {
          prefix = null
        }, 800)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])

  const [collapsedTags, setCollapsedTags] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem('collapsed_tags')) || [])
    } catch {
      return new Set()
    }
  })

  function toggleTagCollapse(id) {
    setCollapsedTags((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      localStorage.setItem('collapsed_tags', JSON.stringify([...next]))
      return next
    })
  }
  const [open, setOpen] = useState(false)
  const location = useLocation()
  const [params] = useSearchParams()

  useEffect(() => {
    let cancelled = false
    let timer = null
    async function load() {
      let active = false
      try {
        const [s, t, v, c] = await Promise.all([
          apiJson('/api/documents/stats'),
          apiJson('/api/tags'),
          apiJson('/api/views'),
          apiJson('/api/correspondents'),
        ])
        if (cancelled) return
        setStats(s)
        setTags(t)
        setViews(v)
        setCorrespondents(c)
        active = s.processing > 0 || (s.running && s.running.length > 0)
      } catch {
        /* sidebar data is best-effort */
      }
      if (cancelled) return
      // Poll fast while the queue is active so the live bars stay current;
      // idle otherwise.
      timer = setTimeout(load, active ? 2500 : 15000)
    }
    load()
    const onChange = () => load()
    window.addEventListener('library-changed', onChange)
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
      window.removeEventListener('library-changed', onChange)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Overall progress tracks the CURRENT import wave. `wave_done` is the
  // cumulative count of documents completed since the wave anchored, over
  // `wave_total` (the wave's high-water size) — both server-persisted and
  // derived from durable counts, so a container restart never rewinds it
  // (10 of 100 stays 10 of 100).
  const remaining = stats?.processing || 0
  const done = stats?.wave_done || 0
  const waveTotal = stats?.wave_total || 0
  const overall = waveTotal > 0 ? done / waveTotal : 0
  const overallPct = Math.round(overall * 100)

  // Anti-flicker: between jobs the running list momentarily empties, and at
  // the very end it toggles on/off — both made the whole sidebar jump.
  // Two stabilizers:
  //  - slot memory: while the queue is active, keep as many bar rows as the
  //    recent maximum, padding with "next file…" placeholders;
  //  - linger: once shown, the panel stays until things have been quiet for
  //    a while, then leaves for good.
  const running = stats?.running || []
  const active = running.length > 0 || remaining > 0
  // Real lane count from the worker. Restart orphans can briefly inflate the
  // running list past this; clamp so a transient spike can't pin the panel at
  // "N files at once" for the whole (never-quiet) batch.
  const slots = Math.max(1, stats?.concurrency || 1)
  const slotsRef = useRef(0)
  const quietSinceRef = useRef(null)
  if (active) {
    slotsRef.current = Math.min(
      slots,
      Math.max(slotsRef.current, running.length, 1),
    )
    quietSinceRef.current = null
  } else {
    if (quietSinceRef.current === null) quietSinceRef.current = Date.now()
    if (Date.now() - quietSinceRef.current > 15000) slotsRef.current = 0
  }
  const showPanel = active || slotsRef.current > 0
  const placeholders = Math.max(0, (active ? slotsRef.current : 0) - running.length)

  useEffect(() => {
    setOpen(false)
  }, [location])

  const onLibrary = location.pathname === '/'
  const activeStatus = onLibrary ? params.get('status') : null
  const activeTag = onLibrary ? params.get('tag') : null
  const activeCorrespondent = onLibrary ? params.get('correspondent') : null

  const statusLinks = [
    { label: 'All documents', to: '/', key: null, count: stats?.total },
    { label: 'Completed', to: '/?status=ready', key: 'ready', count: stats?.ready },
    {
      label: 'Processing',
      to: '/?status=processing',
      key: 'processing',
      count: stats?.processing,
    },
    {
      label: 'Needs attention',
      to: '/?status=flagged',
      key: 'flagged',
      count: stats?.flagged,
    },
    { label: 'Trash', to: '/?status=trash', key: 'trash', count: stats?.trash },
  ]

  return (
    <div className="shell">
      <button
        className={`hamburger ghost ${open ? 'hidden' : ''}`}
        onClick={() => setOpen(!open)}
        aria-label="Menu"
      >
        ☰
      </button>
      <aside className={`sidebar ${open ? 'open' : ''}`}>
        <Link to="/" className="brand">
          {APP_NAME}
        </Link>

        <nav className="side-group">
          {statusLinks.map((s) => (
            <Link
              key={s.label}
              to={s.to}
              className={`side-link ${
                onLibrary && activeStatus === s.key && !activeTag ? 'active' : ''
              }`}
            >
              <span>{s.label}</span>
              {s.count != null && <span className="side-count">{s.count}</span>}
            </Link>
          ))}
          {stats?.expiring > 0 && (
            <Link
              to="/?expiring=1&sort=expires"
              className="side-link expiring-link"
            >
              <span>Expiring soon</span>
              <span className="side-count">{stats.expiring}</span>
            </Link>
          )}
          {stats?.review > 0 && (
            <Link
              to="/review"
              className={`side-link review-link ${
                location.pathname === '/review' ? 'active' : ''
              }`}
            >
              <span>To review</span>
              <span className="side-count">{stats.review}</span>
            </Link>
          )}
          {stats && (
            <button
              className={`side-link side-button pause-toggle ${
                stats?.paused ? 'is-paused' : ''
              }`}
              onClick={async () => {
                const next = !stats.paused
                setStats({ ...stats, paused: next }) // optimistic; poll corrects
                try {
                  await apiJson('/api/documents/processing', {
                    method: 'POST',
                    body: JSON.stringify({ paused: next }),
                  })
                  window.dispatchEvent(new Event('library-changed'))
                } catch (err) {
                  setStats({ ...stats, paused: !next })
                  window.alert(`Pause request failed: ${err.message}`)
                }
              }}
              title={
                stats?.paused
                  ? 'Processing is paused — new work waits until you resume'
                  : 'Finish the current file, then hold new work (safe to restart the server or Mac)'
              }
            >
              <span>{stats?.paused ? '▶ Resume processing' : '⏸ Pause processing'}</span>
              {stats?.paused && <span className="side-count">paused</span>}
            </button>
          )}

          {showPanel && (
            <div className="proc-panel">
              {running.map((r) => (
                <div key={r.id} className="proc-item">
                  <div className="proc-line">
                    <span className="proc-title">{r.title}</span>
                    <span className="proc-eta">
                      {r.phase === 'preparing'
                        ? 'preparing…'
                        : r.phase === 'finishing'
                          ? 'finishing…'
                          : formatEta(r.eta_seconds) || ''}
                    </span>
                  </div>
                  <ProgressBar value={r.progress} />
                </div>
              ))}
              {Array.from({ length: placeholders }, (_, i) => (
                <div key={`ph-${i}`} className="proc-item proc-placeholder">
                  <div className="proc-line">
                    <span className="proc-title">next file…</span>
                    <span className="proc-eta" />
                  </div>
                  <ProgressBar value={0} />
                </div>
              ))}
              {!active && (
                <div className="proc-item">
                  <div className="proc-line proc-overall">
                    <span>All caught up</span>
                  </div>
                </div>
              )}
              {active && slotsRef.current > 1 && (
                <div className="proc-sub">{slotsRef.current} files at once</div>
              )}
              {remaining > 0 && (
                <div className="proc-item proc-overall-item">
                  <div className="proc-line proc-overall">
                    <span>
                      {remaining.toLocaleString()} in queue
                      {stats?.queue_pages_remaining > 0 &&
                        ` · ${
                          stats.queue_pages_remaining >= 10000
                            ? `${Math.round(stats.queue_pages_remaining / 1000)}k`
                            : stats.queue_pages_remaining.toLocaleString()
                        } pages`}
                    </span>
                    <span className="proc-eta">
                      {stats?.paused
                        ? 'paused'
                        : formatEta(stats?.queue_eta_seconds) || 'estimating…'}
                    </span>
                  </div>
                  <ProgressBar value={overall} />
                  <div className="proc-sub">
                    {overallPct}% of this batch · {done.toLocaleString()} of{' '}
                    {waveTotal.toLocaleString()}
                  </div>
                </div>
              )}
            </div>
          )}
        </nav>

        {views.length > 0 && (
          <div className="side-group">
            <div className="side-title">Views</div>
            {views.map((v) => (
              <span key={v.id} className="side-view-row">
                <Link to={`/?${v.params}`} className="side-link">
                  <span className="side-tag-name">{v.name}</span>
                </Link>
                <button
                  className="side-x"
                  title="Delete view"
                  onClick={async () => {
                    await apiJson(`/api/views/${v.id}`, { method: 'DELETE' })
                    window.dispatchEvent(new Event('library-changed'))
                  }}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        {correspondents.length > 0 && (
          <div className="side-group">
            <div className="side-title">Correspondents</div>
            {correspondents.map((c) => (
              <Link
                key={c.id}
                to={`/?correspondent=${c.id}`}
                className={`side-link ${activeCorrespondent === c.id ? 'active' : ''}`}
              >
                <span className="side-tag-name">{c.name}</span>
                <span className="side-count">{c.count}</span>
              </Link>
            ))}
          </div>
        )}

        {tags.length > 0 && (
          <div className="side-group">
            <div className="side-title">Tags</div>
            {flattenTagTree(tags, collapsedTags).map((t) => (
              <div
                key={t.id}
                className="side-tag-row"
                style={t.depth ? { marginLeft: `${t.depth * 0.85}rem` } : undefined}
              >
                {t.hasChildren ? (
                  <button
                    className="tag-caret"
                    onClick={() => toggleTagCollapse(t.id)}
                    title={collapsedTags.has(t.id) ? 'Expand' : 'Collapse'}
                  >
                    {collapsedTags.has(t.id) ? '▸' : '▾'}
                  </button>
                ) : (
                  <span className="tag-caret-spacer" />
                )}
                <Link
                  to={`/?tag=${t.id}`}
                  className={`side-link tag-link ${activeTag === t.id ? 'active' : ''}`}
                >
                  <span className="side-tag-name">
                    {t.color && (
                      <span className="tag-dot" style={{ background: t.color }} />
                    )}
                    {t.name}
                  </span>
                  <span className="side-count">
                    {collapsedTags.has(t.id) ? `${t.count} ▸` : t.count}
                  </span>
                </Link>
              </div>
            ))}
          </div>
        )}

        {stats?.recent?.length > 0 && (
          <div className="side-group">
            <div className="side-title">Recent</div>
            {stats.recent.map((d) => (
              <Link key={d.id} to={`/doc/${d.id}`} className="side-link recent">
                {d.title}
              </Link>
            ))}
          </div>
        )}

        <div className="side-group side-bottom">
          <Link
            to="/insights"
            className={`side-link ${location.pathname === '/insights' ? 'active' : ''}`}
          >
            Insights
          </Link>
          <Link
            to="/offline"
            className={`side-link ${location.pathname === '/offline' ? 'active' : ''}`}
          >
            Offline
          </Link>
          <Link
            to="/settings"
            className={`side-link ${location.pathname === '/settings' ? 'active' : ''}`}
          >
            Settings
          </Link>
          <button className="side-link side-button" onClick={() => setTokens(null)}>
            Sign out
          </button>
        </div>
      </aside>
      {open && <div className="scrim" onClick={() => setOpen(false)} />}
      <main className="content">{children}</main>

      {showHelp && (
        <div className="help-overlay" onClick={() => setShowHelp(false)}>
          <div className="help-card" onClick={(e) => e.stopPropagation()}>
            <strong>Keyboard shortcuts</strong>
            <table>
              <tbody>
                {SHORTCUTS.map(([key, what]) => (
                  <tr key={key}>
                    <td><kbd>{key}</kbd></td>
                    <td>{what}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
