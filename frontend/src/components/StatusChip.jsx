const LABELS = {
  pending: 'Queued',
  processing: 'Processing',
  ready: 'Ready',
  flagged: 'Needs attention',
}

export default function StatusChip({ status, progress = null }) {
  let label = LABELS[status] || status
  if (status === 'processing' && progress != null) {
    label = `${label} ${Math.round(progress * 100)}%`
  }
  return <span className={`chip chip-${status}`}>{label}</span>
}
