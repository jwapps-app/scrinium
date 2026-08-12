import { useEffect, useRef, useState } from 'react'
import { APP_NAME } from '../constants/branding'

/**
 * Explains the gap between "the deploy started" and "the app works".
 *
 * That gap is mostly database migrations, and it used to present as a total
 * outage: the API was not listening, so every screen failed its fetches and
 * the library fell through to its first-run empty state. Being told to upload
 * your first document, over a library of fourteen thousand, invites exactly
 * the recovery actions that would destroy it — re-run setup, restore an
 * export, recreate the stack.
 *
 * So: poll /api/status, and while it is not ready, cover the app with
 * something that says what is happening and that nothing has been lost.
 */

const POLL_MS = 2000
// Long enough that a normal page load never flashes this, short enough that a
// real outage is explained before anyone starts investigating it themselves.
const GRACE_MS = 1200

export default function StartupGate({ children }) {
  const [status, setStatus] = useState(null)
  const [showing, setShowing] = useState(false)
  // Read inside the poll loop without making the loop depend on it — the
  // effect must set up once, not tear down and restart when the panel appears.
  const showingRef = useRef(false)
  showingRef.current = showing

  useEffect(() => {
    let live = true
    let timer = null
    let graceTimer = null

    async function poll() {
      let next
      try {
        const resp = await fetch('/api/status', { cache: 'no-store' })
        next = resp.ok ? { state: 'ready' } : await resp.json()
      } catch {
        // Nothing answered at all: the container is still coming up, or the
        // browser is offline. Only the first is ours to explain — the app has
        // its own offline handling, and covering it would break reading
        // downloaded documents on a train.
        next = navigator.onLine ? { state: 'unreachable' } : { state: 'ready' }
      }
      if (!live) return
      setStatus(next)
      if (next.state === 'ready') {
        setShowing(false)
        return // stop polling; a ready server does not go back
      }
      if (!graceTimer && !showingRef.current) {
        graceTimer = setTimeout(() => live && setShowing(true), GRACE_MS)
      }
      timer = setTimeout(poll, POLL_MS)
    }

    poll()
    return () => {
      live = false
      clearTimeout(timer)
      clearTimeout(graceTimer)
    }
  }, [])

  return (
    <>
      {children}
      {showing && status && status.state !== 'ready' && (
        <StartupPanel status={status} />
      )}
    </>
  )
}

function StartupPanel({ status }) {
  const failed = status.state === 'failed'
  const { step = 0, total = 0 } = status
  const pct = total > 0 ? Math.round((step / total) * 100) : null

  return (
    <div className="startup-gate" role="alertdialog" aria-live="polite">
      <div className="startup-card">
        <h1>{APP_NAME}</h1>
        <p className="startup-headline">{headline(status)}</p>

        {!failed && (
          <div
            className={`startup-bar${pct === null ? ' indeterminate' : ''}`}
            role="progressbar"
            aria-valuenow={pct ?? undefined}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div className="startup-bar-fill" style={pct === null ? undefined : { width: `${pct}%` }} />
          </div>
        )}

        {total > 0 && !failed && (
          <p className="startup-step">
            Step {Math.min(step + 1, total)} of {total}
            {status.message ? ` — ${status.message}` : ''}
          </p>
        )}

        <p className={failed ? 'startup-error' : 'startup-reassure'}>
          {failed
            ? 'The upgrade did not finish. Your documents have not been touched — ' +
              'originals and archives are files on disk and no migration deletes them. ' +
              'Check the api container log before doing anything else.'
            : 'Your documents are safe. Nothing is being deleted or rebuilt — this is ' +
              'a schema update, and the library comes back exactly as you left it.'}
        </p>

        {failed && status.error && <pre className="startup-detail">{status.error}</pre>}

        <p className="startup-warn">
          Please don’t remove the stack, delete volumes, or re-run setup while
          this is showing. Large upgrades can take several minutes.
        </p>

        {status.elapsed_seconds != null && (
          <p className="startup-elapsed">{formatElapsed(status.elapsed_seconds)}</p>
        )}
      </div>
    </div>
  )
}

function headline(status) {
  switch (status.state) {
    case 'unreachable':
      return 'Starting up…'
    case 'starting':
      return 'Checking the database…'
    case 'migrating':
      return 'Upgrading the database…'
    case 'failed':
      return 'The upgrade stopped'
    default:
      return 'Please wait…'
  }
}

function formatElapsed(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s elapsed`
  const mins = Math.floor(seconds / 60)
  return `${mins}m ${Math.round(seconds % 60)}s elapsed`
}
