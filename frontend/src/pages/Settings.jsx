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
    // These barely change — no need to re-count the library every 5s.
    function pollSlow() {
      apiJson('/api/documents/upgradeable')
        .then((d) => !cancelled && setUpgrade(d))
        .catch(() => {})
      apiJson('/api/documents/downsample-candidates')
        .then((d) => !cancelled && setDownsample(d))
        .catch(() => {})
    }
    poll()
    pollSlow()
    const t = setInterval(() => {
      if (!document.hidden) poll()
    }, 5000)
    const ts = setInterval(() => {
      if (!document.hidden) pollSlow()
    }, 30000)
    return () => {
      cancelled = true
      clearInterval(t)
      clearInterval(ts)
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
              <select
                value={downsample ? String(downsample.target_dpi) : '300'}
                disabled={!downsample}
                onChange={async (e) => {
                  const dpi = Number(e.target.value)
                  const prev = downsample
                  setDownsample({ ...downsample, target_dpi: dpi, enabled: dpi > 0 })
                  try {
                    await apiJson('/api/settings/archive-dpi', {
                      method: 'POST',
                      body: JSON.stringify({ dpi }),
                    })
                  } catch (err) {
                    setDownsample(prev)
                    setError(err.message)
                  }
                }}
              >
                <option value="0">Off</option>
                <option value="150">150 DPI</option>
                <option value="200">200 DPI</option>
                <option value="300">300 DPI</option>
                <option value="400">400 DPI</option>
                <option value="600">600 DPI</option>
              </select>
            </div>
            <div className="settings-row">
              <span>Archive format</span>
              <span>
                {/* Only counts archives that were meant to be PDF/A and
                    aren't. Under `auto` a scan is plain PDF on purpose, and
                    counting those would make this number meaningless. */}
                {downsample?.non_pdfa > 0 ? (
                  <a href="/?non_pdfa=1">
                    {downsample.non_pdfa.toLocaleString()} fell short of PDF/A
                  </a>
                ) : (
                  <strong>No PDF/A shortfalls</strong>
                )}
              </span>
            </div>
          </div>
          <p className="settings-help">
            Scanned images are capped at this resolution — plenty for reading,
            search, and print. New documents are capped automatically; to shrink
            the archives you already have, use <strong>Reclaim space</strong> on
            the Insights page.
          </p>
          <p className="settings-help">
            {/* Measured, not hypothetical: on a re-OCR sample every book above
                the cap refused the reduction, because ocrmypdf's JBIG2 output
                at 600 DPI is smaller than a Ghostscript re-render at 300. */}
            Expect some archives to stay above the cap. A reduction is only
            kept when it actually produces a smaller file, and for scanned text
            it often doesn’t — the page images are stored in a compression
            format that re-rendering can’t match, so a lower-resolution copy
            can come out <em>larger</em>. Those documents keep their full
            resolution, which costs nothing and reads better. Each one says
            which case it is under <strong>File details…</strong>.
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
