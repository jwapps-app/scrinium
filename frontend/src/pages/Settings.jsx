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
  const [upgrade, setUpgrade] = useState(null)
  const [upgrading, setUpgrading] = useState(false)
  const [downsample, setDownsample] = useState(null)
  const [compressing, setCompressing] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const data = await apiJson('/api/settings/ocr')
        apiJson('/api/documents/upgradeable').then(setUpgrade).catch(() => {})
        apiJson('/api/documents/downsample-candidates')
          .then((d) => !cancelled && setDownsample(d))
          .catch(() => {})
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
        {upgrade?.count > 0 && upgrade.apple_configured && ocr?.sidecar?.healthy && (
          <div className="organize-block">
            <div className="organize-head">
              <strong>
                {upgrade.count.toLocaleString()} documents used Tesseract
              </strong>
              <button
                disabled={upgrading}
                onClick={async () => {
                  if (!window.confirm(`Queue ${upgrade.count.toLocaleString()} documents for Apple Vision re-OCR? They run at low priority — new documents always go first.`)) return
                  setUpgrading(true)
                  try {
                    const r = await apiJson('/api/documents/upgrade-ocr', { method: 'POST' })
                    setUpgrade({ ...upgrade, count: 0 })
                    window.alert(`${r.queued.toLocaleString()} documents queued for upgrade.`)
                    window.dispatchEvent(new Event('library-changed'))
                  } catch (err) {
                    setError(err.message)
                  } finally {
                    setUpgrading(false)
                  }
                }}
              >
                Upgrade with Apple Vision
              </button>
            </div>
            <p className="settings-help">
              These finished while the helper was unreachable (Mac asleep).
              Upgrading re-reads them with Apple Vision whenever the queue is
              otherwise idle.
            </p>
          </div>
        )}
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
          <h2>Storage</h2>
          <div className="settings-rows">
            <div className="settings-row">
              <span>Archive resolution cap</span>
              <strong>
                {downsample
                  ? downsample.enabled
                    ? `${downsample.target_dpi} DPI`
                    : 'Downsampling off'
                  : '…'}
              </strong>
            </div>
            <div className="settings-row">
              <span>Archive format</span>
              <span>
                {downsample?.non_pdfa > 0 ? (
                  <a href="/?non_pdfa=1">
                    {downsample.non_pdfa.toLocaleString()} not PDF/A
                  </a>
                ) : (
                  <strong>All PDF/A</strong>
                )}
              </span>
            </div>
          </div>
          {downsample?.enabled && downsample.count > 0 && (
            <div className="organize-block">
              <div className="organize-head">
                <strong>
                  {downsample.count.toLocaleString()} archives to check
                </strong>
                <button
                  disabled={compressing}
                  onClick={async () => {
                    if (
                      !window.confirm(
                        `Shrink oversized archives to ${downsample.target_dpi} DPI? Originals are never touched, and each archive is only replaced when the result is genuinely smaller. Runs at the lowest priority — new documents and OCR always go first.`,
                      )
                    )
                      return
                    setCompressing(true)
                    let queued = 0
                    try {
                      // Batched endpoint: loop until nothing remains eligible.
                      for (let i = 0; i < 1000; i++) {
                        const r = await apiJson(
                          '/api/documents/downsample-archives',
                          { method: 'POST' },
                        )
                        queued += r.queued
                        if (r.queued === 0 || r.remaining === 0) break
                      }
                      setDownsample({ ...downsample, count: 0 })
                      window.alert(
                        `${queued.toLocaleString()} archives queued for downsampling. They shrink in the background as the queue idles.`,
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
              </div>
              <p className="settings-help">
                Caps scanned-image resolution at {downsample.target_dpi} DPI —
                plenty for reading, search, and print. Each job re-checks the
                archive and skips anything already at or below the cap, so this
                is safe to run repeatedly. A bad result is always recoverable by
                re-OCR from the untouched original.
              </p>
            </div>
          )}
          <p className="settings-help">
            New documents are capped at this resolution automatically. The
            cap is set with <code>ARCHIVE_MAX_DPI</code> in the server
            environment (0 disables it).
          </p>
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
