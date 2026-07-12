import { useEffect, useState } from 'react'
import { apiJson } from '../api'

/** One-glance operational status at the top of Settings. */
export default function HealthSection() {
  const [health, setHealth] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const data = await apiJson('/api/settings/health')
        if (!cancelled) setHealth(data)
      } catch {
        /* best-effort */
      }
    }
    load()
    const t = setInterval(load, 15000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  if (!health) return null

  const chips = [
    {
      label: 'Worker',
      value:
        health.worker_alive === null
          ? 'unknown'
          : health.worker_alive
            ? 'running'
            : 'not responding',
      cls:
        health.worker_alive === null
          ? 'chip-pending'
          : health.worker_alive
            ? 'chip-ready'
            : 'chip-flagged',
    },
    {
      label: 'Queue',
      value: health.queue.toLocaleString(),
      cls: health.queue > 0 ? 'chip-processing' : 'chip-ready',
    },
  ]
  if (health.integrity && health.integrity.total > 0) {
    const bad = health.integrity.corrupt.length
    chips.push({
      label: 'Integrity',
      value: bad
        ? `${bad} corrupt!`
        : `${health.integrity.verified.toLocaleString()}/${health.integrity.total.toLocaleString()} verified`,
      cls: bad ? 'chip-flagged' : 'chip-ready',
    })
  }
  if (health.disk) {
    const lowDisk = health.disk.free_gb < 10
    chips.push({
      label: 'Disk',
      value: `${health.disk.free_gb} GB free`,
      cls: lowDisk ? 'chip-flagged' : 'chip-ready',
    })
  }

  return (
    <div className="health-strip">
      {chips.map((c) => (
        <span key={c.label} className="health-chip">
          <span className="health-label">{c.label}</span>
          <span className={`chip ${c.cls}`}>{c.value}</span>
        </span>
      ))}
    </div>
  )
}
