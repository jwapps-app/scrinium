import { useState, useRef, useLayoutEffect } from 'react'
import { createPortal } from 'react-dom'

/** Minimal dropdown: trigger button + item list, closes on backdrop click.
 *
 * The popup renders in a portal on document.body so it can't be trapped by an
 * ancestor that creates a containing block for fixed positioning (a `transform`
 * or `backdrop-filter` parent — e.g. the sticky viewer header — would otherwise
 * anchor the mobile bottom-sheet to that box instead of the viewport, clipping
 * it off-screen). Desktop position is measured from the trigger; on phones the
 * popup is a bottom sheet (see .menu-pop @media in styles.css) and ignores it. */
export default function Menu({ label, className = '', items }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef(null)
  const [coords, setCoords] = useState(null)
  const visible = items.filter(Boolean)

  useLayoutEffect(() => {
    if (open && btnRef.current && window.innerWidth > 800) {
      const r = btnRef.current.getBoundingClientRect()
      // Right-aligned under the trigger, viewport-relative (position: fixed).
      setCoords({ top: r.bottom + 4, right: window.innerWidth - r.right })
    } else {
      setCoords(null) // mobile: let the bottom-sheet CSS place it
    }
  }, [open])

  if (visible.length === 0) return null

  return (
    <div className="menu-wrap">
      <button ref={btnRef} className={className} onClick={() => setOpen(!open)}>
        {label}
      </button>
      {open &&
        createPortal(
          <>
            <div className="menu-backdrop" onClick={() => setOpen(false)} />
            <div className="menu-pop" style={coords || undefined}>
              {visible.map((item, i) => (
                <button
                  key={`${item.label}-${i}`}
                  className={`menu-item ${item.danger ? 'danger' : ''}`}
                  onClick={() => {
                    setOpen(false)
                    item.onClick()
                  }}
                >
                  <span>{item.label}</span>
                  {item.hint && <span className="menu-hint">{item.hint}</span>}
                </button>
              ))}
            </div>
          </>,
          document.body
        )}
    </div>
  )
}
