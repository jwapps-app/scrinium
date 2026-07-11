import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiJson } from '../api'
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
  const [error, setError] = useState('')

  useEffect(() => {
    apiJson('/api/insights').then(setData).catch((e) => setError(e.message))
    apiJson('/api/insights/duplicates').then(setDupes).catch(() => {})
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
                      </li>
                    ))}
                  </ul>
                )}
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
