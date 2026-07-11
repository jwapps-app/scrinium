// Human, deliberately rounded — "halfway accurate" is the goal, not precision.
export function formatEta(seconds) {
  if (seconds == null) return null
  if (seconds < 45) return '<1m'
  const mins = Math.round(seconds / 60)
  if (mins < 60) return `~${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) {
    const rem = mins % 60
    return rem ? `~${hours}h ${rem}m` : `~${hours}h`
  }
  const days = Math.floor(hours / 24)
  const remHours = hours % 24
  return remHours ? `~${days}d ${remHours}h` : `~${days}d`
}

export default function ProgressBar({ value, label = false }) {
  const pct = Math.round(Math.min(Math.max(value, 0), 1) * 100)
  return (
    <div className="pbar-wrap">
      <div className="pbar">
        <div className="pbar-fill" style={{ width: `${pct}%` }} />
      </div>
      {label && <span className="pbar-label">{pct}%</span>}
    </div>
  )
}
