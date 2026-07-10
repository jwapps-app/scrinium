import { useEffect, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { apiJson, setTokens } from '../api'
import { APP_NAME } from '../constants/branding'

// Order tags as a tree: parents first, children indented beneath them.
export function flattenTagTree(tags) {
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
      out.push({ ...tag, depth })
      walk(tag.id, depth + 1)
    }
  }
  walk('root', 0)
  return out
}

export default function Shell({ children }) {
  const [stats, setStats] = useState(null)
  const [tags, setTags] = useState([])
  const [open, setOpen] = useState(false)
  const location = useLocation()
  const [params] = useSearchParams()

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [s, t] = await Promise.all([
          apiJson('/api/documents/stats'),
          apiJson('/api/tags'),
        ])
        if (!cancelled) {
          setStats(s)
          setTags(t)
        }
      } catch {
        /* sidebar data is best-effort */
      }
    }
    load()
    const timer = setInterval(load, 15000)
    const onChange = () => load()
    window.addEventListener('library-changed', onChange)
    return () => {
      cancelled = true
      clearInterval(timer)
      window.removeEventListener('library-changed', onChange)
    }
  }, [])

  useEffect(() => {
    setOpen(false)
  }, [location])

  const onLibrary = location.pathname === '/'
  const activeStatus = onLibrary ? params.get('status') : null
  const activeTag = onLibrary ? params.get('tag') : null

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
        </nav>

        {tags.length > 0 && (
          <div className="side-group">
            <div className="side-title">Tags</div>
            {flattenTagTree(tags).map((t) => (
              <Link
                key={t.id}
                to={`/?tag=${t.id}`}
                className={`side-link ${activeTag === t.id ? 'active' : ''}`}
                style={t.depth ? { paddingLeft: `${0.5 + t.depth * 0.85}rem` } : undefined}
              >
                <span className="side-tag-name">
                  {t.depth > 0 && <span className="tree-tick">└</span>}
                  {t.name}
                </span>
                <span className="side-count">{t.count}</span>
              </Link>
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
