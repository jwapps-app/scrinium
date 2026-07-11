import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'
import Menu from '../components/Menu'
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
  ['newest', 'Newest added'],
  ['oldest', 'Oldest added'],
  ['docdate', 'Document date'],
  ['title', 'Title A–Z'],
  ['updated', 'Recently updated'],
]

function displayDate(d) {
  const raw = d.doc_date || d.created_at
  return new Date(d.doc_date ? raw + 'T00:00:00' : raw).toLocaleDateString()
}

export default function Library() {
  const [params, setParams] = useSearchParams()
  const [docs, setDocs] = useState([])
  const [total, setTotal] = useState(0)
  const [tags, setTags] = useState([])
  const [docTypes, setDocTypes] = useState([])
  const [results, setResults] = useState(null)
  const [query, setQuery] = useState(params.get('q') || '')
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [selecting, setSelecting] = useState(false)
  const [selected, setSelected] = useState(() => new Set())
  const [wholeFilter, setWholeFilter] = useState(false)
  const [bulkBusy, setBulkBusy] = useState(false)
  const fileInput = useRef(null)

  const status = params.get('status')
  const tag = params.get('tag')
  const correspondent = params.get('correspondent')
  const doctype = params.get('doctype')
  const engine = params.get('engine')
  const sort = params.get('sort') || 'newest'
  const from = params.get('from')
  const to = params.get('to')
  // Density is a preference, not a filter: remembered across visits, but a
  // saved view's explicit ?view= still wins.
  const view =
    params.get('view') || localStorage.getItem('library_view') || 'grid'

  function setView(v) {
    if (v) localStorage.setItem('library_view', v)
    else localStorage.removeItem('library_view')
    setParam('view', v)
  }
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
      if (correspondent) search.set('correspondent_id', correspondent)
      if (doctype) search.set('doc_type_id', doctype)
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
  }, [status, tag, correspondent, doctype, engine, sort, from, to])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    apiJson('/api/tags').then(setTags).catch(() => {})
    apiJson('/api/doc-types').then(setDocTypes).catch(() => {})
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
  const inTrash = status === 'trash'
  const hasFilters = status || tag || correspondent || doctype || engine || from || to

  async function saveCurrentView() {
    const name = window.prompt('Name this view:')
    if (!name?.trim()) return
    const keep = new URLSearchParams()
    for (const key of ['status', 'tag', 'correspondent', 'doctype', 'engine', 'from', 'to', 'sort', 'q', 'view']) {
      const value = params.get(key)
      if (value) keep.set(key, value)
    }
    try {
      await apiJson('/api/views', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), params: keep.toString() }),
      })
      window.dispatchEvent(new Event('library-changed'))
    } catch (err) {
      setError(err.message)
    }
  }

  function toggleSelected(id) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function exitSelection() {
    setSelecting(false)
    setSelected(new Set())
    setWholeFilter(false)
  }

  async function bulk(action, extra = {}) {
    const count = wholeFilter ? total : selected.size
    if (count === 0) return
    if (action === 'delete' && !window.confirm(`Move ${count} document(s) to the trash?`))
      return
    if (
      action === 'purge' &&
      !window.confirm(`Permanently delete ${count} document(s) and their files? This cannot be undone.`)
    )
      return
    setError('')
    setBulkBusy(true)
    try {
      if (wholeFilter && (tag || inTrash)) {
        // Server-side: acts on everything carrying the tag; deletes are
        // chunked, so repeat until the server says nothing remains.
        let done = 0
        for (;;) {
          const result = await apiJson('/api/documents/bulk', {
            method: 'POST',
            body: JSON.stringify(
              inTrash
                ? { filter_trash: true, action, ...extra }
                : { filter_tag_id: tag, action, ...extra },
            ),
          })
          done += result.processed
          load()
          window.dispatchEvent(new Event('library-changed'))
          if (!result.remaining || result.processed === 0) break
        }
      } else {
        const result = await apiJson('/api/documents/bulk', {
          method: 'POST',
          body: JSON.stringify({ ids: [...selected], action, ...extra }),
        })
        if (result.skipped > 0) setError(`${result.skipped} document(s) were skipped.`)
      }
      exitSelection()
      load()
      window.dispatchEvent(new Event('library-changed'))
    } catch (err) {
      setError(err.message)
    } finally {
      setBulkBusy(false)
    }
  }

  // In selection mode, card/row clicks toggle instead of navigating.
  function selectionClick(e, id) {
    if (!selecting) return
    e.preventDefault()
    toggleSelected(id)
  }

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

          <select
            value={doctype || ''}
            onChange={(e) => setParam('doctype', e.target.value)}
          >
            <option value="">Any type</option>
            {docTypes.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
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
              onClick={() => setView(null)}
              title="Grid — as many tiles as fit"
            >
              ▦
            </button>
            <button
              className={view === 'grid3' ? '' : 'ghost'}
              onClick={() => setView('grid3')}
              title="3 tiles across"
            >
              3×
            </button>
            <button
              className={view === 'grid4' ? '' : 'ghost'}
              onClick={() => setView('grid4')}
              title="4 tiles across"
            >
              4×
            </button>
            <button
              className={view === 'list' ? '' : 'ghost'}
              onClick={() => setView('list')}
              title="List view"
            >
              ☰
            </button>
          </div>

          {hasFilters && !inTrash && (
            <button className="ghost" onClick={saveCurrentView} title="Save this filter combination to the sidebar">
              Save view
            </button>
          )}
          <button
            className="ghost"
            onClick={() => (selecting ? exitSelection() : setSelecting(true))}
          >
            {selecting ? 'Cancel' : 'Select'}
          </button>
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

        {selecting && (
          <div className="bulk-bar">
            <strong>
              {wholeFilter ? `all ${total} in filter` : `${selected.size} selected`}
            </strong>
            <button
              className="ghost"
              onClick={() => {
                setWholeFilter(false)
                setSelected(new Set(docs.map((d) => d.id)))
              }}
            >
              Select shown ({docs.length})
            </button>
            {tag && total > 0 && (
              <button
                className={wholeFilter ? '' : 'ghost'}
                onClick={() => setWholeFilter(!wholeFilter)}
                title="Act on every document with this tag, not just the ones loaded"
              >
                Entire filter ({total})
              </button>
            )}
            {inTrash && total > 0 && (
              <button
                className={wholeFilter ? '' : 'ghost'}
                onClick={() => setWholeFilter(!wholeFilter)}
                title="Act on everything in the trash"
              >
                Entire trash ({total})
              </button>
            )}
            <span className="bulk-spacer" />
            {bulkBusy && <span className="doc-meta">working…</span>}
            {inTrash ? (
              <>
                <button
                  className="ghost"
                  disabled={bulkBusy || (!wholeFilter && selected.size === 0)}
                  onClick={() => bulk('restore')}
                >
                  Restore
                </button>
                <button
                  className="ghost danger"
                  disabled={bulkBusy || (!wholeFilter && selected.size === 0)}
                  onClick={() => bulk('purge')}
                >
                  Delete forever
                </button>
              </>
            ) : (
              <button
                className="ghost"
                disabled={bulkBusy || (!wholeFilter && selected.size === 0)}
                onClick={() => bulk('reprocess', { mode: 'skip' })}
              >
                Re-OCR
              </button>
            )}
            {!inTrash && (
              <>
                <Menu
                  label="Tag"
                  className="ghost"
                  items={tags.map((t) => ({
                    label: t.name,
                    onClick: () => bulk('add_tags', { tag_ids: [t.id] }),
                  }))}
                />
                <Menu
                  label="Untag"
                  className="ghost"
                  items={tags.map((t) => ({
                    label: t.name,
                    onClick: () => bulk('remove_tags', { tag_ids: [t.id] }),
                  }))}
                />
                <button
                  className="ghost danger"
                  disabled={bulkBusy || (!wholeFilter && selected.size === 0)}
                  onClick={() => bulk('delete')}
                >
                  Delete
                </button>
              </>
            )}
          </div>
        )}

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
              {inTrash ? ' in the trash' : ''}
              {tagName ? ` tagged ${tagName}` : ''}
              {hasFilters && (
                <button className="ghost clear-filters" onClick={() => setParams({})}>
                  Clear filters
                </button>
              )}
            </h2>

            {inTrash && total > 0 && (
              <p className="settings-help">
                Items here delete permanently after the retention period, or
                immediately via “Delete forever”.
              </p>
            )}
            {docs.length === 0 && (
              <p className="empty">
                {inTrash
                  ? 'The trash is empty.'
                  : hasFilters
                    ? 'Nothing matches these filters.'
                    : 'Drop a PDF here or hit Upload to get started.'}
              </p>
            )}

            {view !== 'list' ? (
              <div
                className={`card-grid${
                  view === 'grid3' ? ' cols-3' : view === 'grid4' ? ' cols-4' : ''
                }`}
              >
                {docs.map((d) => (
                  <Link
                    to={`/doc/${d.id}`}
                    key={d.id}
                    className={`card ${selected.has(d.id) ? 'selected' : ''}`}
                    onClick={(e) => selectionClick(e, d.id)}
                  >
                    {selecting && (
                      <span
                        className={`pick ${selected.has(d.id) ? 'picked' : ''}`}
                      >
                        {selected.has(d.id) ? '✓' : ''}
                      </span>
                    )}
                    <Thumb id={d.id} className="thumb-card" />
                    <div className="card-body">
                      <span className="card-title">{d.title}</span>
                      <span className="card-meta">
                        {d.correspondent_name ? `${d.correspondent_name} · ` : ''}
                        {d.page_count ? `${d.page_count} pp · ` : ''}
                        {displayDate(d)}
                      </span>
                      <StatusChip status={d.status} progress={d.progress} phase={d.phase} />
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
                    <Link
                      to={`/doc/${d.id}`}
                      className={`doc-row ${selected.has(d.id) ? 'selected' : ''}`}
                      onClick={(e) => selectionClick(e, d.id)}
                    >
                      {selecting && (
                        <span
                          className={`pick ${selected.has(d.id) ? 'picked' : ''}`}
                        >
                          {selected.has(d.id) ? '✓' : ''}
                        </span>
                      )}
                      <Thumb id={d.id} className="thumb-row" />
                      <span className="doc-title">{d.title}</span>
                      {d.tags.map((t) => (
                        <span key={t.id} className="chip chip-tag">
                          {t.name}
                        </span>
                      ))}
                      <span className="doc-meta">
                        {d.correspondent_name ? `${d.correspondent_name} · ` : ''}
                        {d.page_count ? `${d.page_count} pp · ` : ''}
                        {displayDate(d)}
                      </span>
                      {d.status === 'processing' && d.progress != null && (
                        <ProgressBar value={d.progress} />
                      )}
                      <StatusChip status={d.status} progress={d.progress} phase={d.phase} />
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
