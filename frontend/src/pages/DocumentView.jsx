import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { apiDelete, apiFetch, apiJson, isTextNative } from '../api'
import { invalidateThumb } from '../components/Thumb'
import StatusChip from '../components/StatusChip'
import ProgressBar from '../components/ProgressBar'
import PageOrganizer from '../components/PageOrganizer'
import { isOffline, keepOffline, offlineFile, offlineMeta, removeOffline } from '../offline'
import PdfViewer from '../components/PdfViewer'
import Menu from '../components/Menu'
import DocumentDetails from '../components/DocumentDetails'
import { useIsAdmin } from '../session'

export default function DocumentView() {
  // Permanent deletion is owner only; trashing stays available to all.
  const isAdmin = useIsAdmin()
  const { id } = useParams()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') || ''

  const [doc, setDoc] = useState(null)
  const [fileUrl, setFileUrl] = useState(null)
  const [pageMode, setPageMode] = useState(false)
  const [outline, setOutline] = useState(null)
  const [night, setNight] = useState(
    () => localStorage.getItem('viewer_night') === '1',
  )
  const [focusOverride, setFocusOverride] = useState(null)
  const [annotations, setAnnotations] = useState([])
  const [pendingSelection, setPendingSelection] = useState(null)
  const [showHighlights, setShowHighlights] = useState(false)
  const [related, setRelated] = useState([])
  const [serverPage, setServerPage] = useState(null)
  const [positionReady, setPositionReady] = useState(false)
  const [offlineKept, setOfflineKept] = useState(false)
  const [readerText, setReaderText] = useState(null)
  const positionTimer = useRef(null)
  const [pageBusy, setPageBusy] = useState(false)
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
  // True while mounted; guards post-await object-URL creation in pageOp
  // (the unmount cleanup has already revoked by the time a late await lands).
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    apiJson('/api/settings/ocr').then(setOcrInfo).catch(() => {})
  }, [])

  const load = useCallback(async () => {
    try {
      const data = await apiJson(`/api/documents/${id}`)
      setDoc(data)
      setTitle(data.title)
    } catch (err) {
      // Server unreachable? An offline copy still opens. But a refusal from the
      // server (401/403/404) is not "unreachable" — falling back there served
      // another account's cached document.
      const meta = err?.fromServer ? null : await offlineMeta(id).catch(() => null)
      if (meta) {
        setDoc({ ...meta, status: 'ready', tags: [], custom_values: {}, offline_only: true })
        setTitle(meta.title)
      } else {
        setError(err.message)
      }
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    // Guard against cross-document races: a slow response for the previous
    // document must not land on the one now being viewed.
    let cancelled = false
    apiJson(`/api/documents/${id}/annotations`)
      .then((d) => !cancelled && setAnnotations(d))
      .catch(() => {})
    apiJson(`/api/documents/${id}/related`)
      .then((d) => !cancelled && setRelated(d.related))
      .catch(() => {})
    apiJson(`/api/documents/${id}/position`)
      .then((d) => !cancelled && setServerPage(d.page))
      .catch(() => {})
      .finally(() => !cancelled && setPositionReady(true))
    isOffline(id).then((v) => !cancelled && setOfflineKept(v)).catch(() => {})
    return () => {
      cancelled = true
      if (positionTimer.current) clearTimeout(positionTimer.current)
    }
  }, [id])

  function positionChanged(page) {
    if (positionTimer.current) clearTimeout(positionTimer.current)
    positionTimer.current = setTimeout(() => {
      apiJson(`/api/documents/${id}/position`, {
        method: 'PUT',
        body: JSON.stringify({ page }),
      }).catch(() => {})
    }, 4000)
  }

  async function saveHighlight() {
    const sel = pendingSelection
    if (!sel) return
    setPendingSelection(null)
    window.getSelection()?.removeAllRanges()
    try {
      const created = await apiJson(`/api/documents/${id}/annotations`, {
        method: 'POST',
        body: JSON.stringify({
          page: sel.page,
          quote: sel.quote,
          rects: sel.rects,
        }),
      })
      setAnnotations((prev) => [...prev, created])
    } catch (err) {
      setError(err.message)
    }
  }

  async function toggleOffline() {
    try {
      if (offlineKept) {
        await removeOffline(id)
        setOfflineKept(false)
      } else {
        await keepOffline(doc)
        setOfflineKept(true)
        window.alert('Saved for offline use on this device.')
      }
    } catch (err) {
      setError(err.message)
    }
  }

  // Poll while processing so the viewer (and progress) appear when OCR lands.
  useEffect(() => {
    if (!doc || (doc.status !== 'pending' && doc.status !== 'processing')) return
    const t = setInterval(() => {
      if (!document.hidden) load()
    }, 2500)
    return () => clearInterval(t)
  }, [doc, load])

  // Fetch the file with auth and hand PDF.js a blob URL.
  useEffect(() => {
    if (!doc) return
    if (isTextNative(doc)) {
      apiJson(`/api/documents/${id}/text`)
        .then((d) => setReaderText(d.text))
        .catch(() => setReaderText(''))
      return
    }
    let cancelled = false
    apiFetch(`/api/documents/${id}/file`)
      .then((resp) => {
        if (resp.ok) return resp.blob()
        // Reached the server and it said no: do NOT fall back to the cache.
        const denied = new Error('File unavailable')
        denied.fromServer = true
        throw denied
      })
      .catch(async (err) => {
        if (err?.fromServer) throw err
        const cached = await offlineFile(id).catch(() => null)
        if (cached) return cached.blob()
        throw err
      })
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

  async function pageOp(action, pages, extra = {}) {
    setPageBusy(true)
    setError('')
    try {
      const result = await apiJson(`/api/documents/${id}/pages`, {
        method: 'POST',
        body: JSON.stringify({ action, pages, ...extra }),
      })
      if (action === 'extract') {
        window.dispatchEvent(new Event('library-changed'))
        if (window.confirm('Pages copied into a new document. Open it now?')) {
          navigate(`/doc/${result.new_document_id}`)
          setPageMode(false)
          return
        }
      } else {
        // The document was rebuilt: refetch metadata and the file itself.
        setPageMode(false)
        setFileUrl(null)
        invalidateThumb(id) // pages changed — the cached thumbnail is stale
        await load()
        const resp = await apiFetch(`/api/documents/${id}/file`)
        if (resp.ok && mountedRef.current) {
          const blob = await resp.blob()
          if (urlRef.current) URL.revokeObjectURL(urlRef.current)
          urlRef.current = URL.createObjectURL(blob)
          setFileUrl(urlRef.current)
        }
        window.dispatchEvent(new Event('library-changed'))
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setPageBusy(false)
    }
  }

  async function shareDoc() {
    try {
      const link = await apiJson(`/api/documents/${id}/share`, {
        method: 'POST',
        body: JSON.stringify({ days: 7 }),
      })
      const url = `${window.location.origin}${link.url_path}`
      try {
        await navigator.clipboard.writeText(url)
        window.alert('Share link copied — anyone with it can view this document for 7 days.')
      } catch {
        window.prompt('Copy this link:', url)
      }
    } catch (err) {
      setError(err.message)
    }
  }

  async function unshareDoc() {
    try {
      await apiDelete(`/api/documents/${id}/share`)
      window.alert('All share links for this document are revoked.')
    } catch (err) {
      setError(err.message)
    }
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
    if (!window.confirm('Move this document to the trash?')) return
    try {
      await apiJson(`/api/documents/${id}`, { method: 'DELETE' })
      window.dispatchEvent(new Event('library-changed'))
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
  const focusPage =
    focusOverride ?? (q && matchPages.length ? matchPages[matchIdx]?.page : null)

  // Apple upgrade offer: only when the server is set up for Apple OCR, the
  // helper is answering, and this document was done by a lesser engine.
  const canUpgradeOcr =
    ocrInfo?.engine === 'apple' &&
    ocrInfo?.sidecar?.healthy === true &&
    doc.status === 'ready' &&
    doc.ocr_engine !== 'apple'

  return (
    <div className={`viewer-page ${night ? 'pdf-night' : ''}`}>
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
          <button
            className={night ? '' : 'ghost'}
            onClick={() => {
              const next = !night
              setNight(next)
              localStorage.setItem('viewer_night', next ? '1' : '0')
            }}
            title="Invert page colors for night reading"
          >
            ☾
          </button>
          <button
            className={showHighlights ? '' : 'ghost'}
            onClick={() => setShowHighlights(!showHighlights)}
            title="Highlights and notes"
          >
            ✺{annotations.length > 0 ? ` ${annotations.length}` : ''}
          </button>
          {outline && (
            <Menu
              label="Contents"
              className="ghost"
              items={outline
                .filter((o) => o.page)
                .map((o) => ({
                  label: `${'\u2003'.repeat(o.depth)}${o.title}`,
                  hint: `p. ${o.page}`,
                  onClick: () => setFocusOverride(o.page),
                }))}
            />
          )}
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
              fileUrl && {
                label: 'Edit pages…',
                hint: 'rotate, delete, split',
                onClick: () => setPageMode(true),
              },
              {
                label: offlineKept ? 'Remove offline copy' : 'Keep offline',
                hint: offlineKept ? 'stored on this device' : 'readable without the server',
                onClick: toggleOffline,
              },
              {
                label: 'Copy share link',
                hint: 'public link, 7 days',
                onClick: shareDoc,
              },
              {
                label: 'Revoke share links',
                onClick: unshareDoc,
              },
              {
                label: 'Move to trash',
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

      {doc.deleted_at && (
        <div className="trash-banner">
          <span>
            This document is in the trash — it will be permanently deleted
            after the retention period.
          </span>
          <button
            onClick={async () => {
              setDoc(await apiJson(`/api/documents/${id}/restore`, { method: 'POST' }))
              window.dispatchEvent(new Event('library-changed'))
            }}
          >
            Restore
          </button>
          {isAdmin && (
            <button
              className="ghost danger"
              onClick={async () => {
                if (!window.confirm('Permanently delete this document and its files?')) return
                await apiJson(`/api/documents/${id}/purge`, { method: 'DELETE' })
                window.dispatchEvent(new Event('library-changed'))
                navigate('/')
              }}
            >
              Delete forever
            </button>
          )}
        </div>
      )}

      {!doc.deleted_at && <DocumentDetails doc={doc} onChange={setDoc} />}

      {doc.status === 'flagged' && (
        <p className="error">
          OCR failed — the original is safe. {doc.error}
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      {pageMode && fileUrl && (
        <PageOrganizer
          url={fileUrl}
          busy={pageBusy}
          onAction={pageOp}
          onClose={() => setPageMode(false)}
        />
      )}

      {pendingSelection && (
        <button
          className="hl-float"
          style={{
            left: Math.min(pendingSelection.anchorX, window.innerWidth - 120),
            top: pendingSelection.anchorY + 8,
          }}
          onClick={saveHighlight}
        >
          ✺ Highlight
        </button>
      )}

      {showHighlights && (
        <aside className="hl-drawer">
          <div className="hl-drawer-head">
            <strong>Highlights ({annotations.length})</strong>
            <button className="ghost" onClick={() => setShowHighlights(false)}>
              ×
            </button>
          </div>
          {annotations.length === 0 && (
            <p className="settings-help">
              Select text in the document and hit “Highlight”.
            </p>
          )}
          <ul className="hl-list">
            {annotations.map((a) => (
              <li key={a.id} className="hl-item">
                <blockquote
                  onClick={() => {
                    setFocusOverride(null)
                    requestAnimationFrame(() => setFocusOverride(a.page))
                  }}
                  title={`Jump to page ${a.page}`}
                >
                  {a.quote.length > 220 ? `${a.quote.slice(0, 220)}…` : a.quote}
                </blockquote>
                <textarea
                  rows={1}
                  placeholder="Add a note…"
                  defaultValue={a.note || ''}
                  onBlur={(e) => {
                    if (e.target.value !== (a.note || '')) {
                      apiJson(`/api/annotations/${a.id}`, {
                        method: 'PATCH',
                        body: JSON.stringify({ note: e.target.value }),
                      }).catch(() => {})
                    }
                  }}
                />
                <div className="hl-item-foot">
                  <span className="settings-hint">p. {a.page}</span>
                  <button
                    className="ghost danger side-x"
                    onClick={async () => {
                      try {
                        await apiDelete(`/api/annotations/${a.id}`)
                        setAnnotations((prev) => prev.filter((x) => x.id !== a.id))
                      } catch (err) {
                        setError(err.message)
                      }
                    }}
                  >
                    ×
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </aside>
      )}

      {related.length > 0 && !doc.deleted_at && (
        <p className="related-strip">
          <span className="settings-hint">Shares content with:</span>
          {related.map((r) => (
            <Link key={r.id} to={`/doc/${r.id}`}>
              {r.title} <span className="settings-hint">{r.similarity}%</span>
            </Link>
          ))}
        </p>
      )}

      {readerText !== null && (
        <article className="reader-view">
          {readerText
            ? readerText.split(/\n{2,}/).map((para, i) => <p key={i}>{para}</p>)
            : <p className="settings-help">No text could be extracted — download the original to view it.</p>}
        </article>
      )}

      {readerText === null && fileUrl && positionReady ? (
        <PdfViewer
          url={fileUrl}
          storageKey={id}
          onOutline={setOutline}
          annotations={annotations}
          onSelectText={setPendingSelection}
          onPositionChange={positionChanged}
          resumePage={serverPage}
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
