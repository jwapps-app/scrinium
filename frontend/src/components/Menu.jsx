import { useState } from 'react'

/** Minimal dropdown: trigger button + item list, closes on backdrop click. */
export default function Menu({ label, className = '', items }) {
  const [open, setOpen] = useState(false)
  const visible = items.filter(Boolean)
  if (visible.length === 0) return null

  return (
    <div className="menu-wrap">
      <button className={className} onClick={() => setOpen(!open)}>
        {label}
      </button>
      {open && (
        <>
          <div className="menu-backdrop" onClick={() => setOpen(false)} />
          <div className="menu-pop">
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
        </>
      )}
    </div>
  )
}
