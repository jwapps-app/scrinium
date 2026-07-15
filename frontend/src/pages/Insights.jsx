import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch, apiJson } from '../api'
import Shell from '../components/Shell'

function fmtBytes(n) {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${Math.round(n / 1024)} KB`
}

function monthLabel(ym) {
  const [y, m] = ym.split('-')
  return new Date(+y, +m - 1, 1).toLocaleDateString(undefined, {
    month: 'short',
  })
}

function Bars({ items, labelKey, max, color, linkTo }) {
  return (
    <div className="bars">
      {items.map((item) => (
        <div key={item[labelKey]} className="bar-row">
          <span className="bar-label" title={item[labelKey]}>
            {linkTo ? (
              <Link to={linkTo(item)}>{item[labelKey]}</Link>
            ) : (
              item[labelKey]
            )}
          </span>
          <span className="bar-track">
            <span
              className="bar-fill"
              style={{
                width: `${Math.max(2, (item.count / max) * 100)}%`,
                background: item.color || color || 'var(--accent)',
              }}
            />
          </span>
          <span className="bar-count">{item.count.toLocaleString()}</span>
        </div>
      ))}
    </div>
  )
}

export default function Insights() {
  const [data, setData] = useState(null)
  const [dupes, setDupes] = useState(null)
  const [storage, setStorage] = useState(null)
  const [compressing, setCompressing] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    apiJson('/api/insights').then(setData).catch((e) => setError(e.message))
    apiJson('/api/insights/duplicates').then(setDupes).catch(() => {})
    apiJson('/api/documents/downsample-candidates').then(setStorage).catch(() => {})
  }, [])

  const maxMonthly = data ? Math.max(1, ...data.monthly.map((m) => m.count)) : 1

  return (
    <Shell>
      <div className="library">
        <h1 className="page-title">Insights</h1>
        {error && <p className="error">{error}</p>}
        {data && (
          <>
            <div className="stat-cards">
              <div className="stat-card">
                <span className="stat-value">
                  {data.documents.toLocaleString()}
                </span>
                <span className="stat-label">documents</span>
              </div>
              <div className="stat-card">
                <span className="stat-value">{data.pages.toLocaleString()}</span>
                <span className="stat-label">pages</span>
              </div>
              <div className="stat-card">
                <span className="stat-value">{fmtBytes(data.storage_bytes)}</span>
                <span className="stat-label">on disk</span>
              </div>
            </div>

            {data.monthly.length > 0 && (
              <section className="insight-section">
                <h2>Added per month</h2>
                <div className="month-chart">
                  {data.monthly.map((m) => (
                    <div key={m.month} className="month-col" title={`${m.month}: ${m.count}`}>
                      <span
                        className="month-bar"
                        style={{ height: `${Math.max(3, (m.count / maxMonthly) * 100)}%` }}
                      />
                      <span className="month-label">{monthLabel(m.month)}</span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            <div className="insight-grid">
              {data.correspondents.length > 0 && (
                <section className="insight-section">
                  <h2>Top correspondents</h2>
                  <Bars
                    items={data.correspondents}
                    labelKey="name"
                    max={Math.max(...data.correspondents.map((c) => c.count))}
                  />
                </section>
              )}
              {data.doc_types.length > 0 && (
                <section className="insight-section">
                  <h2>Document types</h2>
                  <Bars
                    items={data.doc_types}
                    labelKey="name"
                    max={Math.max(...data.doc_types.map((c) => c.count))}
                  />
                </section>
              )}
              {data.tags.length > 0 && (
                <section className="insight-section">
                  <h2>Top tags</h2>
                  <Bars
                    items={data.tags}
                    labelKey="name"
                    max={Math.max(...data.tags.map((c) => c.count))}
                  />
                </section>
              )}
              {data.engines.length > 0 && (
                <section className="insight-section">
                  <h2>OCR engines</h2>
                  <Bars
                    items={data.engines}
                    labelKey="name"
                    max={Math.max(...data.engines.map((c) => c.count))}
                  />
                </section>
              )}
            </div>

            {dupes && (dupes.pairs.length > 0 || dupes.pending_fingerprint > 0) && (
              <section className="insight-section">
                <h2>Possible duplicates</h2>
                {dupes.pending_fingerprint > 0 && (
                  <p className="settings-help">
                    Still fingerprinting {dupes.pending_fingerprint.toLocaleString()}{' '}
                    documents — check back shortly for full coverage.
                  </p>
                )}
                {dupes.pairs.length === 0 ? (
                  <p className="settings-help">
                    No near-duplicates among {dupes.fingerprinted.toLocaleString()}{' '}
                    fingerprinted documents.
                  </p>
                ) : (
                  <ul className="dup-list">
                    {dupes.pairs.map((p, i) => (
                      <li key={i} className="dup-pair">
                        <span className="dup-similarity">{p.similarity}%</span>
                        <span className="dup-docs">
                          <Link to={`/doc/${p.a.id}`}>{p.a.title}</Link>
                          <span className="dup-vs">≈</span>
                          <Link to={`/doc/${p.b.id}`}>{p.b.title}</Link>
                        </span>
                        <span className="dup-actions">
                          <Link
                            className="ghost-link"
                            to={`/compare/${p.a.id}/${p.b.id}`}
                            title="Open both side by side"
                          >
                            Compare
                          </Link>
                          <button
                            className="ghost"
                            title={`Move “${p.a.title}” to the trash`}
                            onClick={async () => {
                              await apiFetch(`/api/documents/${p.a.id}`, { method: 'DELETE' })
                              setDupes({ ...dupes, pairs: dupes.pairs.filter((x) => x !== p) })
                            }}
                          >
                            Trash left
                          </button>
                          <button
                            className="ghost"
                            title={`Move “${p.b.title}” to the trash`}
                            onClick={async () => {
                              await apiFetch(`/api/documents/${p.b.id}`, { method: 'DELETE' })
                              setDupes({ ...dupes, pairs: dupes.pairs.filter((x) => x !== p) })
                            }}
                          >
                            Trash right
                          </button>
                          <button
                            className="ghost"
                            title="These are different documents — stop suggesting this pair"
                            onClick={async () => {
                              await apiJson('/api/insights/duplicates/dismiss', {
                                method: 'POST',
                                body: JSON.stringify({ a: p.a.id, b: p.b.id }),
                              })
                              setDupes({ ...dupes, pairs: dupes.pairs.filter((x) => x !== p) })
                            }}
                          >
                            Not a dupe
                          </button>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}

            {storage?.enabled && storage.count > 0 && (
              <section className="insight-section">
                <h2>Reclaim space</h2>
                <p className="settings-help">
                  Scanned pages above {storage.target_dpi} DPI carry more
                  resolution than a document library needs. Downsampling caps
                  them at {storage.target_dpi} DPI — originals are never touched,
                  each archive is only replaced when the result is genuinely
                  smaller, and it runs at the lowest priority behind new work.
                  {storage.non_pdfa > 0 && (
                    <>
                      {' '}
                      <a href="/?non_pdfa=1">
                        {storage.non_pdfa.toLocaleString()} archives aren’t
                        PDF/A.
                      </a>
                    </>
                  )}
                </p>
                <button
                  disabled={compressing}
                  onClick={async () => {
                    if (
                      !window.confirm(
                        `Shrink oversized archives to ${storage.target_dpi} DPI? Runs in the background — new documents and OCR always go first.`,
                      )
                    )
                      return
                    setCompressing(true)
                    let queued = 0
                    try {
                      for (let i = 0; i < 1000; i++) {
                        const r = await apiJson(
                          '/api/documents/downsample-archives',
                          { method: 'POST' },
                        )
                        queued += r.queued
                        if (r.queued === 0 || r.remaining === 0) break
                      }
                      setStorage({ ...storage, count: 0 })
                      window.alert(
                        `${queued.toLocaleString()} archives queued for downsampling.`,
                      )
                      window.dispatchEvent(new Event('library-changed'))
                    } catch (err) {
                      setError(err.message)
                    } finally {
                      setCompressing(false)
                    }
                  }}
                >
                  {compressing ? 'Queueing…' : 'Reclaim space'}
                </button>
              </section>
            )}

            {data.low_yield?.length > 0 && (
              <section className="insight-section">
                <h2>Weak OCR — worth a better scan?</h2>
                <p className="settings-help">
                  These finished OCR but yielded almost no text per page —
                  usually a very low-quality scan or a mostly-image document.
                </p>
                <ul className="dup-list">
                  {data.low_yield.map((d) => (
                    <li key={d.id} className="dup-pair">
                      <span className="dup-similarity">
                        {d.chars_per_page}&thinsp;ch/p
                      </span>
                      <span className="dup-docs">
                        <Link to={`/doc/${d.id}`}>{d.title}</Link>
                        <span className="settings-hint">{d.pages} pp</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </div>
    </Shell>
  )
}
