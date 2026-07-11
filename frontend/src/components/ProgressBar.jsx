// Human, deliberately rounded — "halfway accurate" is the goal, not precision.
export function formatEta(seconds) {
  if (seconds == null) return null
  if (seconds < 45) return '<1m'
  const mins = Math.round(seconds / 60)
  if (mins < 60) return `~${mins}m`
  const hours = Math.floor(mins / 60)
  const rem = mins % 60
  return rem ? `~${hours}h ${rem}m` : `~${hours}h`
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
