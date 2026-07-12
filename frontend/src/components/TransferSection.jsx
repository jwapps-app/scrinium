import { useCallback, useEffect, useState } from 'react'
import { apiJson } from '../api'
import { APP_NAME } from '../constants/branding'

/** Settings card: Paperless import + full-library export. */
export default function TransferSection() {
  const [imp, setImp] = useState(null)
  const [exp, setExp] = useState(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const [i, e] = await Promise.all([
        apiJson('/api/import/paperless'),
        apiJson('/api/export'),
      ])
      setImp(i)
      setExp(e.status)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // Poll while either side is running.
  const busy = imp?.status?.state === 'running' || exp?.state === 'running'
  useEffect(() => {
    if (!busy) return
    const t = setInterval(load, 2500)
    return () => clearInterval(t)
  }, [busy, load])

  async function start(path) {
    setError('')
    try {
      await apiJson(path, { method: 'POST' })
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  function progress(st) {
    if (!st?.state) return null
    if (st.state === 'running')
      return st.total
        ? `working… ${st.done ?? 0}/${st.total}`
        : st.note || 'working…'
    if (st.state === 'failed') return `failed: ${st.error}`
    return null
  }

  const impStatus = imp?.status
  return (
    <section className="settings-section">
      <h2>Import & export</h2>
      {error && <p className="error">{error}</p>}

      <div className="organize-block">
        <div className="organize-head">
          <strong>Import from Paperless-ngx</strong>
          <button
            disabled={!imp?.export_found || impStatus?.state === 'running'}
            onClick={() => start('/api/import/paperless')}
          >
            {impStatus?.state === 'running' ? 'Importing…' : 'Import'}
          </button>
        </div>
        <p className="settings-help">
          Run Paperless&apos;s <code>document_exporter</code>, copy the export
          folder (or a zip of it) into <code>{imp?.import_dir || '…'}</code> on
          the server, then hit Import. Documents, titles, dates, tags (with
          colors), correspondents, types, and notes carry over; duplicates are
          skipped, so re-running is safe.
        </p>
        <p className="settings-help">
          {imp?.export_found
            ? `Found: ${imp.export_found}`
            : 'No export detected yet.'}
          {progress(impStatus) && ` — ${progress(impStatus)}`}
          {impStatus?.state === 'done' &&
            ` Last run: ${impStatus.imported} imported, ${impStatus.skipped} duplicates skipped` +
              (impStatus.failed ? `, ${impStatus.failed} failed.` : '.')}
        </p>
      </div>

      <div className="organize-block">
        <div className="organize-head">
          <strong>Export the whole library</strong>
          <button
            disabled={exp?.state === 'running'}
            onClick={() => start('/api/export')}
          >
            {exp?.state === 'running' ? 'Exporting…' : 'Export'}
          </button>
        </div>
        <p className="settings-help">
          Writes a zip into the server&apos;s data folder with your documents
          in browsable folders built from their tag hierarchy — e.g.{' '}
          <code>originals/Taxes/2023/W2.pdf</code> — plus a parallel{' '}
          <code>searchable/</code> tree of the OCR&apos;d copies and a
          manifest of all metadata. Your documents are never locked into{' '}
          {APP_NAME}.
        </p>
        <p className="settings-help">
          {progress(exp)}
          {exp?.state === 'done' &&
            `Last export: ${exp.total} documents, ${exp.size_mb} MB — ${exp.path}`}
        </p>
      </div>
    </section>
  )
}
