import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'
import ProgressBar from '../components/ProgressBar'
import Shell from '../components/Shell'
import StatusChip from '../components/StatusChip'
import Thumb from '../components/Thumb'

// Search snippets arrive with [[match]] markers (see backend ts_headline
// config); render the matches as <mark> without trusting any HTML.
function Snippet({ text }) {
  const parts = text.split(/\[\[(.*?)\]\]/g)
  return (
    <p className="snippet">
      {parts.map((part, i) => (i % 2 === 1 ? <mark key={i}>{part}</mark> : part))}
    </p>
  )
}

const ENGINES = ['tesseract', 'apple', 'native']
const SORTS = [
  ['newest', 'Newest'],
  ['oldest', 'Oldest'],
  ['title', 'Title A–Z'],
  ['updated', 'Recently updated'],
]

export default function Library() {
  const [params, setParams] = useSearchParams()
  const [docs, setDocs] = useState([])
  const [total, setTotal] = useState(0)
  const [tags, setTags] = useState([])
  const [results, setResults] = useState(null)
  const [query, setQuery] = useState(params.get('q') || '')
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const fileInput = useRef(null)

  const status = params.get('status')
  const tag = params.get('tag')
  const engine = params.get('engine')
  const sort = params.get('sort') || 'newest'
  const from = params.get('from')
  const to = params.get('to')
  const view = params.get('view') || 'grid'
  const q = params.get('q')

  function setParam(key, value) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next, { replace: true })
  }

  const load = useCallback(async () => {
    try {
      const search = new URLSearchParams()
      if (status === 'processing') {
        // The sidebar's "Processing" bucket covers the whole in-flight queue.
        search.set('status_filter', 'pending,processing')
      } else if (status) {
        search.set('status_filter', status)
      }
      if (tag) search.set('tag_id', tag)
      if (engine) search.set('engine', engine)
      if (from) search.set('date_from', from)
      if (to) search.set('date_to', to)
      search.set('sort', sort)
      search.set('limit', '100')
      const data = await apiJson(`/api/documents?${search.toString()}`)
      setDocs(data.items)
      setTotal(data.total)
    } catch (err) {
      setError(err.message)
    }
  }, [status, tag, engine, sort, from, to])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    apiJson('/api/tags').then(setTags).catch(() => {})
  }, [])

  // Poll while anything is still working its way through the pipeline.
  useEffect(() => {
    const busy = docs.some((d) => d.status === 'pending' || d.status === 'processing')
    if (!busy) return
    const t = setInterval(() => {
      load()
      window.dispatchEvent(new Event('library-changed'))
    }, 2500)
    return () => clearInterval(t)
  }, [docs, load])

  // Run a search whenever ?q= is present.
  useEffect(() => {
    if (!q) {
      setResults(null)
      return
    }
    setQuery(q)
    apiJson(`/api/search?q=${encodeURIComponent(q)}`)
      .then((data) => setResults(data.results))
      .catch((err) => setError(err.message))
  }, [q])

  async function uploadFiles(files) {
    setUploading(true)
    setError('')
    for (const file of files) {
      const form = new FormData()
      form.append('file', file)
      try {
        const resp = await apiFetch('/api/documents', { method: 'POST', body: form })
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}))
          throw new Error(body.detail || `Upload failed (${resp.status})`)
        }
      } catch (err) {
        setError(`${file.name}: ${err.message}`)
      }
    }
    setUploading(false)
    load()
    window.dispatchEvent(new Event('library-changed'))
  }

  const tagName = tag ? tags.find((t) => t.id === tag)?.name : null
  const hasFilters = status || tag || engine || from || to

  return (
    <Shell>
      <div
        className="library"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault()
          uploadFiles([...e.dataTransfer.files])
        }}
      >
        <div className="toolbar">
          <form
            className="searchbar"
            onSubmit={(e) => {
              e.preventDefault()
              setParam('q', query.trim())
            }}
          >
            <input
              type="search"
              placeholder="Search documents…"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value)
                if (!e.target.value.trim()) setParam('q', null)
              }}
            />
          </form>

          <select value={engine || ''} onChange={(e) => setParam('engine', e.target.value)}>
            <option value="">Any engine</option>
            {ENGINES.map((eng) => (
              <option key={eng} value={eng}>
                {eng}
              </option>
            ))}
          </select>

          <select value={sort} onChange={(e) => setParam('sort', e.target.value)}>
            {SORTS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>

          <input
            type="date"
            value={from || ''}
            onChange={(e) => setParam('from', e.target.value)}
            title="From date"
          />
          <input
            type="date"
            value={to || ''}
            onChange={(e) => setParam('to', e.target.value)}
            title="To date"
          />

          <div className="view-toggle">
            <button
              className={view === 'grid' ? '' : 'ghost'}
              onClick={() => setParam('view', null)}
              title="Grid view"
            >
              ▦
            </button>
            <button
              className={view === 'list' ? '' : 'ghost'}
              onClick={() => setParam('view', 'list')}
              title="List view"
            >
              ☰
            </button>
          </div>

          <button onClick={() => fileInput.current?.click()} disabled={uploading}>
            {uploading ? 'Uploading…' : 'Upload'}
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".pdf,image/*"
            multiple
            hidden
            onChange={(e) => {
              uploadFiles([...e.target.files])
              e.target.value = ''
            }}
          />
        </div>

        {error && <p className="error">{error}</p>}

        {results !== null ? (
          <section>
            <h2>
              {results.length} result{results.length === 1 ? '' : 's'} for “{q}”
            </h2>
            <ul className="doc-list">
              {results.map((r) => (
                <li key={r.id}>
                  <Link
                    to={`/doc/${r.id}?q=${encodeURIComponent(q)}`}
                    className="doc-row"
                  >
                    <Thumb id={r.id} className="thumb-row" />
                    <span className="doc-title">{r.title}</span>
                    <span className="doc-meta">jump to matches →</span>
                    <StatusChip status={r.status} />
                  </Link>
                  <Snippet text={r.snippet} />
                </li>
              ))}
            </ul>
          </section>
        ) : (
          <section>
            <h2>
              {total} document{total === 1 ? '' : 's'}
              {tagName ? ` tagged ${tagName}` : ''}
              {hasFilters && (
                <button className="ghost clear-filters" onClick={() => setParams({})}>
                  Clear filters
                </button>
              )}
            </h2>

            {docs.length === 0 && (
              <p className="empty">
                {hasFilters
                  ? 'Nothing matches these filters.'
                  : 'Drop a PDF here or hit Upload to get started.'}
              </p>
            )}

            {view === 'grid' ? (
              <div className="card-grid">
                {docs.map((d) => (
                  <Link to={`/doc/${d.id}`} key={d.id} className="card">
                    <Thumb id={d.id} className="thumb-card" />
                    <div className="card-body">
                      <span className="card-title">{d.title}</span>
                      <span className="card-meta">
                        {d.page_count ? `${d.page_count} pp · ` : ''}
                        {new Date(d.created_at).toLocaleDateString()}
                      </span>
                      <StatusChip status={d.status} progress={d.progress} />
                      {d.status === 'processing' && d.progress != null && (
                        <ProgressBar value={d.progress} />
                      )}
                    </div>
                  </Link>
                ))}
              </div>
            ) : (
              <ul className="doc-list">
                {docs.map((d) => (
                  <li key={d.id}>
                    <Link to={`/doc/${d.id}`} className="doc-row">
                      <Thumb id={d.id} className="thumb-row" />
                      <span className="doc-title">{d.title}</span>
                      {d.tags.map((t) => (
                        <span key={t.id} className="chip chip-tag">
                          {t.name}
                        </span>
                      ))}
                      <span className="doc-meta">
                        {d.page_count ? `${d.page_count} pp · ` : ''}
                        {new Date(d.created_at).toLocaleDateString()}
                      </span>
                      {d.status === 'processing' && d.progress != null && (
                        <ProgressBar value={d.progress} />
                      )}
                      <StatusChip status={d.status} progress={d.progress} />
                    </Link>
                    {d.status === 'flagged' && d.error && (
                      <p className="error small">{d.error}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
      </div>
    </Shell>
  )
}
