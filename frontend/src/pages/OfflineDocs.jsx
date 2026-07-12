import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Shell from '../components/Shell'
import { listOffline, removeOffline } from '../offline'

/** Documents stored on this device — readable with the server dark. */
export default function OfflineDocs() {
  const [docs, setDocs] = useState([])

  const load = () => listOffline().then(setDocs).catch(() => {})
  useEffect(() => {
    load()
  }, [])

  return (
    <Shell>
      <div className="library">
        <h1 className="page-title">Offline documents</h1>
        <p className="settings-help">
          Stored in this browser — they open even when the server is
          unreachable. Use “Keep offline” in any document&apos;s ⋯ menu to add
          more. The device may reclaim the space if the app goes unused for a
          long time, so treat this as a go-bag, not the archive.
        </p>
        {docs.length === 0 && (
          <p className="settings-help">Nothing stored offline yet.</p>
        )}
        <ul className="doc-list">
          {docs.map((d) => (
            <li key={d.id}>
              <Link to={`/doc/${d.id}`} className="doc-row">
                <span className="doc-title">{d.title}</span>
                <span className="doc-meta">
                  {d.page_count ? `${d.page_count} pp · ` : ''}
                  saved {new Date(d.saved_at).toLocaleDateString()}
                </span>
              </Link>
              <button
                className="ghost danger side-x"
                onClick={async () => {
                  await removeOffline(d.id)
                  load()
                }}
                title="Remove the offline copy"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      </div>
    </Shell>
  )
}
