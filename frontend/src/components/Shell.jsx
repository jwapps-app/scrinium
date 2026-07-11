import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
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

export default function Shell({ children }) {
  const [stats, setStats] = useState(null)
  const [tags, setTags] = useState([])
  const [views, setViews] = useState([])
  const [correspondents, setCorrespondents] = useState([])
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

  // Burndown peak for the overall import bar: high-water mark of the queue,
  // reset when it drains. Fills 0→100% as the backlog clears; dips when a
  // fresh wave arrives — exactly the "adjusts as files keep coming" behavior.
  const peakRef = useRef(0)
  const remaining = stats?.processing || 0
  if (remaining === 0) peakRef.current = 0
  else if (remaining > peakRef.current) peakRef.current = remaining
  const burndown = peakRef.current > 0 ? (peakRef.current - remaining) / peakRef.current : 0

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

          {((stats?.running && stats.running.length > 0) || remaining > 0) && (
            <div className="proc-panel">
              {stats?.running?.map((r) => (
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
              {stats?.running_count > 1 && (
                <div className="proc-sub">{stats.running_count} files at once</div>
              )}
              {remaining > 0 && (
                <div className="proc-item proc-overall-item">
                  <div className="proc-line proc-overall">
                    <span>{remaining.toLocaleString()} in queue</span>
                    <span className="proc-eta">
                      {stats?.paused
                        ? 'paused'
                        : formatEta(stats?.queue_eta_seconds) || 'estimating…'}
                    </span>
                  </div>
                  <ProgressBar value={burndown} />
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
    </div>
  )
}
