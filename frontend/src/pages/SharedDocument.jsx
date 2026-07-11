import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import PdfViewer from '../components/PdfViewer'
import { APP_NAME } from '../constants/branding'

/** Public viewer for a shared document — no login, token is the key. */
export default function SharedDocument() {
  const { token } = useParams()
  const [meta, setMeta] = useState(null)
  const [fileUrl, setFileUrl] = useState(null)
  const [error, setError] = useState('')
  const urlRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const metaResp = await fetch(`/api/share/${token}`)
        if (!metaResp.ok) throw new Error('This link is invalid or has expired.')
        const m = await metaResp.json()
        if (cancelled) return
        setMeta(m)
        const fileResp = await fetch(`/api/share/${token}/file`)
        if (!fileResp.ok) throw new Error('File unavailable.')
        const blob = await fileResp.blob()
        if (cancelled) return
        urlRef.current = URL.createObjectURL(blob)
        setFileUrl(urlRef.current)
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    })()
    return () => {
      cancelled = true
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    }
  }, [token])

  return (
    <div className="share-page">
      <header className="share-header">
        <span className="share-app">{APP_NAME}</span>
        {meta && <h1>{meta.title}</h1>}
        {meta && (
          <a
            className="button-link"
            href={`/api/share/${token}/file?disposition=attachment`}
          >
            Download
          </a>
        )}
      </header>
      {error && <p className="error share-error">{error}</p>}
      {fileUrl && <PdfViewer url={fileUrl} />}
    </div>
  )
}
