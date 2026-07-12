import { useEffect, useState } from 'react'
import { apiJson } from '../api'
import { APP_NAME } from '../constants/branding'
import RulesSection from '../components/RulesSection'
import OrganizeSection from '../components/OrganizeSection'
import TransferSection from '../components/TransferSection'
import AccountSection from '../components/AccountSection'
import ShareLinksSection from '../components/ShareLinksSection'
import HealthSection from '../components/HealthSection'
import { Link } from 'react-router-dom'
import Shell from '../components/Shell'
import SidecarSetup from '../components/SidecarSetup'

export default function Settings() {
  const [ocr, setOcr] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const data = await apiJson('/api/settings/ocr')
        if (!cancelled) {
          setOcr(data)
          setError('')
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }
    poll()
    const t = setInterval(poll, 5000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  const sidecar = ocr?.sidecar
  const helperState = !sidecar?.configured
    ? { label: 'Not configured', cls: 'chip-pending' }
    : sidecar.healthy
      ? { label: 'Connected', cls: 'chip-ready' }
      : { label: 'Not detected', cls: 'chip-flagged' }

  return (
    <Shell>
      <div className="library">
        <h1 className="page-title">Settings</h1>

        <HealthSection />

        {error && <p className="error">{error}</p>}

      <section className="settings-section">
        <h2>Server-side OCR</h2>
        <div className="settings-card">
          <div className="settings-row">
            <span>Engine</span>
            <span className="engine-toggle">
              {['tesseract', 'apple'].map((e) => (
                <button
                  key={e}
                  className={ocr?.engine === e ? '' : 'ghost'}
                  disabled={!ocr || (e === 'apple' && !ocr.sidecar?.configured)}
                  title={
                    e === 'apple' && !ocr?.sidecar?.configured
                      ? 'Set APPLE_OCR_URL in the stack env first'
                      : e === ocr?.engine_env
                        ? 'Server default'
                        : undefined
                  }
                  onClick={async () => {
                    try {
                      // Choosing the env default clears the override.
                      const next = e === ocr.engine_env ? '' : e
                      await apiJson('/api/settings/ocr', {
                        method: 'POST',
                        body: JSON.stringify({ engine: next }),
                      })
                      setOcr({ ...ocr, engine: e, engine_override: next })
                    } catch (err) {
                      setError(err.message)
                    }
                  }}
                >
                  {e === 'apple' ? 'Apple Vision' : 'Tesseract'}
                </button>
              ))}
              {ocr?.engine_override && (
                <span className="settings-hint">
                  overriding server default ({ocr.engine_env})
                </span>
              )}
            </span>
          </div>
          <div className="settings-row">
            <span>Languages</span>
            <strong>{ocr ? ocr.languages : '…'}</strong>
          </div>
          <div className="settings-row">
            <span>Apple Vision helper</span>
            <span className={`chip ${helperState.cls}`}>{helperState.label}</span>
          </div>
        </div>
        <p className="settings-help">
          The Apple Vision helper is a small program that runs on a Mac and
          gives {APP_NAME} Apple-quality OCR for documents processed on the
          server. When it isn't running, everything still works — OCR just
          uses the built-in engine instead. Setup: build{' '}
          <code>sidecar/</code> from the repo, run it, and set{' '}
          <code>OCR_ENGINE=apple</code> and <code>APPLE_OCR_URL</code> in the
          server environment.
        </p>
        <SidecarSetup connected={sidecar?.healthy === true} />
        </section>

        <section className="settings-section">
          <h2>Tags</h2>
          <p className="settings-help">
            Rename, recolor, and restructure the tag tree on its own page —
            with a big library it deserves the room.
          </p>
          <Link to="/tags" className="button-link">
            Manage tags →
          </Link>
        </section>

        <OrganizeSection />

        <RulesSection />

        <TransferSection />

        <ShareLinksSection />

        <AccountSection />
      </div>
    </Shell>
  )
}
