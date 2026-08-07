const LABELS = {
  pending: 'Queued',
  processing: 'Processing',
  ready: 'Completed',
  flagged: 'Needs attention',
}

const PHASE_LABELS = {
  preparing: 'Preparing',
  ocr: 'Processing',
  finishing: 'Finishing',
  'text-only': 'Reading text',
}

export default function StatusChip({ status, progress = null, phase = null }) {
  // Completed is the normal state — only exceptions deserve a badge.
  if (status === 'ready') return null
  let label = LABELS[status] || status
  if (status === 'processing' && progress != null) {
    label = `${PHASE_LABELS[phase] || 'Processing'} ${Math.round(progress * 100)}%`
  }
  return <span className={`chip chip-${status}`}>{label}</span>
}
