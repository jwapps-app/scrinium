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
