import { useEffect, useState } from 'react'
import { apiJson } from '../api'

function Step({ done, number, title, children }) {
  return (
    <li className={`setup-step ${done ? 'done' : ''}`}>
      <span className="setup-marker">{done ? '✓' : number}</span>
      <div className="setup-body">
        <strong>{title}</strong>
        {children}
      </div>
    </li>
  )
}

function Commands({ text }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="setup-commands">
      <pre>{text}</pre>
      <button
        type="button"
        className="ghost"
        onClick={() => {
          navigator.clipboard.writeText(text).then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          })
        }}
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

export default function SidecarSetup({ connected }) {
  const [open, setOpen] = useState(false)
  const [setup, setSetup] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open || setup) return
    apiJson('/api/settings/sidecar-setup').then(setSetup).catch((e) => setError(e.message))
  }, [open, setup])

  function downloadPlist() {
    const blob = new Blob([setup.plist], { type: 'application/xml' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = setup.plist_path.split('/').pop()
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <div className="setup-wrap">
      <button type="button" className="ghost" onClick={() => setOpen(!open)}>
        {open ? 'Hide setup guide' : 'Set up the Apple Vision helper'}
      </button>
      {error && <p className="error">{error}</p>}
      {open && setup && (
        <ol className="setup-steps">
          <Step number={1} done={connected} title="Build and install the helper (on the Mac)">
            <Commands text={setup.build_commands} />
          </Step>
          <Step number={2} done={connected} title="Start it on login">
            <p className="settings-help">
              Save this file as <code>{setup.plist_path}</code> (port{' '}
              {setup.port} is already baked in), then load it:
            </p>
            <button type="button" className="ghost" onClick={downloadPlist}>
              Download plist
            </button>
            <Commands text={setup.load_commands} />
          </Step>
          <Step number={3} done={setup.configured} title="Point the server at the helper">
            <p className="settings-help">
              Add to the server environment and restart the api and worker
              containers:
            </p>
            <Commands text={setup.server_env} />
          </Step>
          <Step number={4} done={connected} title="Connected">
            <p className="settings-help">
              {connected
                ? 'The helper is answering — server-side documents now get Apple-quality OCR.'
                : 'Waiting for the helper to answer the health check…'}
            </p>
          </Step>
        </ol>
      )}
    </div>
  )
}
