/**
 * Custom line icons for the sidebar. Single stroke weight, 24×24 grid, drawn
 * with currentColor so CSS controls the color (burgundy by default, white on
 * the active row). Keep new glyphs in the same visual style.
 */
const PATHS = {
  // Bar chart — Insights
  insights: (
    <>
      <path d="M4 4v16h16" />
      <path d="M8 20v-6" />
      <path d="M13 20V9" />
      <path d="M18 20v-9" />
    </>
  ),
  // Stacked layers — the whole collection
  docs: (
    <>
      <path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z" />
      <path d="M3 12l9 4.5L21 12" />
      <path d="M3 16.5 12 21l9-4.5" />
    </>
  ),
  // Circle-check — Completed
  check: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 12.5l2.5 2.5L16 9" />
    </>
  ),
  // Circular arrow — Processing
  processing: (
    <>
      <path d="M20 12a8 8 0 1 1-2.3-5.6" />
      <path d="M20 4v4h-4" />
    </>
  ),
  // Warning triangle — Needs attention
  attention: (
    <>
      <path d="M12 4 2.5 20h19L12 4Z" />
      <path d="M12 10v4" />
      <circle cx="12" cy="17.2" r="0.6" fill="currentColor" stroke="none" />
    </>
  ),
  // Trash can — Trash
  trash: (
    <>
      <path d="M4 7h16" />
      <path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
      <path d="M6.5 7l1 12.5a1 1 0 0 0 1 .9h7a1 1 0 0 0 1-.9L18 7" />
      <path d="M10 11v6M14 11v6" />
    </>
  ),
  // Clock — Expiring soon
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5.2l3.2 2" />
    </>
  ),
  // Eye — To review
  review: (
    <>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="2.8" />
    </>
  ),
  // Download tray — Offline
  offline: (
    <>
      <path d="M12 3v11" />
      <path d="M8 11l4 4 4-4" />
      <path d="M5 20h14" />
    </>
  ),
  // Sliders — Settings
  settings: (
    <>
      <path d="M4 7h9M17 7h3" />
      <path d="M4 12h3M11 12h9" />
      <path d="M4 17h9M17 17h3" />
      <circle cx="15" cy="7" r="2" />
      <circle cx="9" cy="12" r="2" />
      <circle cx="15" cy="17" r="2" />
    </>
  ),
  // Door arrow — Sign out
  signout: (
    <>
      <path d="M14 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4" />
      <path d="M4 12h11" />
      <path d="M8 8l-4 4 4 4" />
    </>
  ),
}

export default function Icon({ name, className = '' }) {
  const glyph = PATHS[name]
  if (!glyph) return null
  return (
    <svg
      className={`icon ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {glyph}
    </svg>
  )
}
