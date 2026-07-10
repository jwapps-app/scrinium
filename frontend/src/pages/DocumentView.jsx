import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'
import StatusChip from '../components/StatusChip'
import ProgressBar from '../components/ProgressBar'
import PdfViewer from '../components/PdfViewer'
import Menu from '../components/Menu'

export default function DocumentView() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') || ''

  const [doc, setDoc] = useState(null)
  const [fileUrl, setFileUrl] = useState(null)
  const [error, setError] = useState('')
  const [editingTitle, setEditingTitle] = useState(false)
  const [title, setTitle] = useState('')
  const [notice, setNotice] = useState('')
  const [findText, setFindText] = useState(q)
  const [matches, setMatches] = useState(null) // {pages:[{page,terms,snippet}], terms:[]}
  const [matchIdx, setMatchIdx] = useState(0)
  const [ocrInfo, setOcrInfo] = useState(null)
  const [fitMode, setFitMode] = useState('width')
  const [zoom, setZoom] = useState(1)
  const urlRef = useRef(null)

  useEffect(() => {
    apiJson('/api/settings/ocr').then(setOcrInfo).catch(() => {})
  }, [])

  const load = useCallback(async () => {
    try {
      const data = await apiJson(`/api/documents/${id}`)
      setDoc(data)
      setTitle(data.title)
    } catch (err) {
      setError(err.message)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  // Poll while processing so the viewer (and progress) appear when OCR lands.
  useEffect(() => {
    if (!doc || (doc.status !== 'pending' && doc.status !== 'processing')) return
    const t = setInterval(load, 2500)
    return () => clearInterval(t)
  }, [doc, load])

  // Fetch the file with auth and hand PDF.js a blob URL.
  useEffect(() => {
    if (!doc) return
    let cancelled = false
    apiFetch(`/api/documents/${id}/file`)
      .then((resp) => (resp.ok ? resp.blob() : Promise.reject(new Error('File unavailable'))))
      .then((blob) => {
        if (cancelled) return
        if (urlRef.current) URL.revokeObjectURL(urlRef.current)
        urlRef.current = URL.createObjectURL(blob)
        setFileUrl(urlRef.current)
      })
      .catch((err) => setError(err.message))
    return () => {
      cancelled = true
    }
  }, [doc?.has_archive, id]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    },
    [],
  )

  // Locate the query within the document (server-side, same stemming as search).
  useEffect(() => {
    setFindText(q)
    if (!q.trim()) {
      setMatches(null)
      return
    }
    let cancelled = false
    apiJson(`/api/documents/${id}/search?q=${encodeURIComponent(q)}`)
      .then((data) => {
        if (cancelled) return
        setMatches(data)
        setMatchIdx(0)
      })
      .catch((err) => setError(err.message))
    return () => {
      cancelled = true
    }
  }, [q, id, doc?.status === 'ready']) // eslint-disable-line react-hooks/exhaustive-deps

  function submitFind(e) {
    e.preventDefault()
    const next = new URLSearchParams(params)
    if (findText.trim()) next.set('q', findText.trim())
    else next.delete('q')
    setParams(next, { replace: true })
  }

  async function saveTitle() {
    setEditingTitle(false)
    if (!title.trim() || title === doc.title) return
    try {
      setDoc(await apiJson(`/api/documents/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: title.trim() }),
      }))
    } catch (err) {
      setError(err.message)
    }
  }

  async function classify() {
    setError('')
    setNotice('')
    try {
      const result = await apiJson(`/api/documents/${id}/classify`, {
        method: 'POST',
      })
      setDoc(result.document)
      setTitle(result.document.title)
      if (result.matched_rules.length === 0) {
        setNotice('No rules matched.')
      } else {
        const parts = [`Matched: ${result.matched_rules.join(', ')}`]
        if (result.added_tags.length) parts.push(`tagged ${result.added_tags.join(', ')}`)
        if (result.new_title) parts.push(`title set to “${result.new_title}”`)
        setNotice(parts.join(' — '))
      }
    } catch (err) {
      setError(err.message)
    }
  }

  async function reprocess(mode) {
    try {
      setDoc(await apiJson(`/api/documents/${id}/reprocess`, {
        method: 'POST',
        body: JSON.stringify({ mode }),
      }))
    } catch (err) {
      setError(err.message)
    }
  }

  async function remove() {
    if (!window.confirm('Delete this document and its files?')) return
    try {
      await apiJson(`/api/documents/${id}`, { method: 'DELETE' })
      navigate('/')
    } catch (err) {
      setError(err.message)
    }
  }

  async function download(version) {
    const resp = await apiFetch(
      `/api/documents/${id}/file?version=${version}&disposition=attachment`,
    )
    if (!resp.ok) {
      setError('Download failed')
      return
    }
    const blob = await resp.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download =
      version === 'archive' ? `${doc.title}.pdf` : doc.original_filename
    a.click()
    URL.revokeObjectURL(a.href)
  }

  if (!doc) return <div className="viewer-page">{error && <p className="error">{error}</p>}</div>

  const matchPages = matches?.pages || []
  const focusPage = q && matchPages.length ? matchPages[matchIdx]?.page : null

  // Apple upgrade offer: only when the server is set up for Apple OCR, the
  // helper is answering, and this document was done by a lesser engine.
  const canUpgradeOcr =
    ocrInfo?.engine === 'apple' &&
    ocrInfo?.sidecar?.healthy === true &&
    doc.status === 'ready' &&
    doc.ocr_engine !== 'apple'

  return (
    <div className="viewer-page">
      <div className="viewer-chrome">
      <header className="topbar">
        <Link to="/" className="back">
          ← Library
        </Link>
        {editingTitle ? (
          <input
            className="title-input"
            value={title}
            autoFocus
            onChange={(e) => setTitle(e.target.value)}
            onBlur={saveTitle}
            onKeyDown={(e) => e.key === 'Enter' && saveTitle()}
          />
        ) : (
          <h1 onClick={() => setEditingTitle(true)} title="Click to rename">
            {doc.title}
          </h1>
        )}
        <StatusChip status={doc.status} progress={doc.progress} phase={doc.phase} />
        <form className="findbar" onSubmit={submitFind}>
          <input
            type="search"
            placeholder="Find in document…"
            value={findText}
            onChange={(e) => setFindText(e.target.value)}
          />
        </form>
        <div className="zoom-controls">
          <button
            className={fitMode === 'width' && zoom === 1 ? '' : 'ghost'}
            onClick={() => {
              setFitMode('width')
              setZoom(1)
            }}
            title="Fit page width"
          >
            Width
          </button>
          <button
            className={fitMode === 'page' && zoom === 1 ? '' : 'ghost'}
            onClick={() => {
              setFitMode('page')
              setZoom(1)
            }}
            title="Fit whole page"
          >
            Page
          </button>
          <button
            className="ghost"
            onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.25).toFixed(2)))}
            title="Zoom out"
          >
            −
          </button>
          <span className="zoom-pct">{Math.round(zoom * 100)}%</span>
          <button
            className="ghost"
            onClick={() => setZoom((z) => Math.min(3, +(z + 0.25).toFixed(2)))}
            title="Zoom in"
          >
            +
          </button>
        </div>
        <div className="topbar-actions">
          <Menu
            label="Download"
            items={[
              doc.has_archive && {
                label: 'Searchable',
                hint: 'PDF/A with text layer',
                onClick: () => download('archive'),
              },
              {
                label: 'Original',
                hint: 'untouched file as ingested',
                onClick: () => download('original'),
              },
            ]}
          />
          <Menu
            label="⋯"
            className="ghost"
            items={[
              canUpgradeOcr && {
                label: 'Re-OCR with Apple Vision',
                hint: `currently ${doc.ocr_engine || 'unprocessed'}`,
                onClick: () => reprocess('redo'),
              },
              doc.status === 'flagged' && {
                label: 'Retry OCR',
                onClick: () => reprocess('skip'),
              },
              {
                label: 'Run classification',
                onClick: classify,
              },
              {
                label: 'Delete document',
                danger: true,
                onClick: remove,
              },
            ]}
          />
        </div>
      </header>

      {doc.status === 'processing' && doc.progress != null && (
        <ProgressBar value={doc.progress} label />
      )}

      {q && matches && (
        <div className="match-nav sticky-nav">
          {matchPages.length === 0 ? (
            <span>No matches for “{q}”</span>
          ) : (
            <>
              <span>
                “{q}” on {matchPages.length} page{matchPages.length === 1 ? '' : 's'} —
                showing p. {matchPages[matchIdx].page}
              </span>
              <button
                className="ghost"
                disabled={matchIdx === 0}
                onClick={() => setMatchIdx(matchIdx - 1)}
              >
                ‹ Prev
              </button>
              <span className="match-count">
                {matchIdx + 1}/{matchPages.length}
              </span>
              <button
                className="ghost"
                disabled={matchIdx >= matchPages.length - 1}
                onClick={() => setMatchIdx(matchIdx + 1)}
              >
                Next ›
              </button>
            </>
          )}
        </div>
      )}
      </div>

      {doc.tags.length > 0 && (
        <p className="doc-tags">
          {doc.tags.map((t) => (
            <span key={t.id} className="chip chip-tag">
              {t.name}
            </span>
          ))}
        </p>
      )}

      {doc.status === 'flagged' && (
        <p className="error">
          OCR failed — the original is safe. {doc.error}
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      {fileUrl ? (
        <PdfViewer
          url={fileUrl}
          highlightTerms={matches?.terms || []}
          focusPage={focusPage}
          fitMode={fitMode}
          zoom={zoom}
        />
      ) : (
        <p className="empty">
          {doc.status === 'processing' || doc.status === 'pending'
            ? 'Processing…'
            : 'Loading…'}
        </p>
      )}
    </div>
  )
}
