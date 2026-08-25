import { useEffect, useRef, useState } from 'react'
import { loadPdf } from '../pdfjs'

const THUMB_WIDTH = 132
const RENDER_MARGIN = 600

/**
 * Full-screen page editor: thumbnails of every page, tap to select, then
 * rotate / delete / extract. Thumbnails render lazily from scroll position
 * so a 1,000-page book opens instantly (same approach as PdfViewer).
 */
export default function PageOrganizer({ url, busy, onAction, onClose }) {
  const scrollerRef = useRef(null)
  const [count, setCount] = useState(0)
  const [selected, setSelected] = useState(() => new Set())
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    const scroller = scrollerRef.current
    const grid = scroller.querySelector('.po-grid')
    grid.innerHTML = ''
    const st = { pdf: null, slots: [], rendered: new Set(), rendering: new Set() }

    async function renderSlot(i) {
      if (st.rendered.has(i) || st.rendering.has(i) || !st.pdf) return
      st.rendering.add(i)
      try {
        const page = await st.pdf.getPage(i + 1)
        if (cancelled) return
        const viewport = page.getViewport({ scale: 1 })
        const scale = THUMB_WIDTH / viewport.width
        const scaled = page.getViewport({ scale: scale * window.devicePixelRatio })
        const canvas = document.createElement('canvas')
        canvas.width = scaled.width
        canvas.height = scaled.height
        canvas.style.width = `${THUMB_WIDTH}px`
        await page.render({ canvasContext: canvas.getContext('2d'), viewport: scaled })
          .promise
        if (cancelled) return
        const slot = st.slots[i]
        slot.replaceChildren(canvas, slot.lastChild) // keep the number badge
        st.rendered.add(i)
      } finally {
        st.rendering.delete(i)
      }
    }

    function renderVisible() {
      if (!st.slots.length) return
      const top = scroller.scrollTop - RENDER_MARGIN
      const bottom = scroller.scrollTop + scroller.clientHeight + RENDER_MARGIN
      st.slots.forEach((slot, i) => {
        if (slot.offsetTop + slot.offsetHeight >= top && slot.offsetTop <= bottom) {
          renderSlot(i)
        }
      })
    }

    ;(async () => {
      try {
        const pdf = await loadPdf(url)
        if (cancelled) return
        st.pdf = pdf
        setCount(pdf.numPages)
        const first = await pdf.getPage(1)
        const vp = first.getViewport({ scale: 1 })
        const ratio = vp.height / vp.width
        for (let i = 0; i < pdf.numPages; i++) {
          const slot = document.createElement('div')
          slot.className = 'po-slot'
          slot.dataset.page = i + 1
          slot.style.width = `${THUMB_WIDTH}px`
          slot.style.height = `${Math.round(THUMB_WIDTH * ratio)}px`
          const badge = document.createElement('span')
          badge.className = 'po-num'
          badge.textContent = i + 1
          slot.appendChild(badge)
          grid.appendChild(slot)
          st.slots.push(slot)
        }
        renderVisible()
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    })()

    let throttle = null
    const onScroll = () => {
      if (throttle) return
      throttle = setTimeout(() => {
        throttle = null
        renderVisible()
      }, 120)
    }
    scroller.addEventListener('scroll', onScroll)
    return () => {
      cancelled = true
      scroller.removeEventListener('scroll', onScroll)
      if (st.pdf) st.pdf.destroy()
    }
  }, [url])

  function toggle(page) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(page)) next.delete(page)
      else next.add(page)
      return next
    })
  }

  const pages = [...selected].sort((a, b) => a - b)
  const none = pages.length === 0

  function act(action, extra = {}) {
    onAction(action, pages, extra)
  }

  return (
    <div className="page-organizer">
      <div className="po-toolbar">
        <span className="po-count">
          {none ? `${count} pages — tap to select` : `${pages.length} selected`}
        </span>
        <button className="ghost" disabled={none || busy} onClick={() => act('rotate', { degrees: -90 })} title="Rotate selected left">
          ⟲
        </button>
        <button className="ghost" disabled={none || busy} onClick={() => act('rotate', { degrees: 90 })} title="Rotate selected right">
          ⟳
        </button>
        <button
          className="ghost"
          disabled={none || busy}
          onClick={() => {
            const title = window.prompt('Title for the new document:')
            if (title !== null) act('extract', { title: title.trim() || null })
          }}
          title="Copy selected pages into a new document (this one is untouched)"
        >
          Extract
        </button>
        <button
          className="ghost danger"
          disabled={none || busy}
          onClick={() => {
            if (window.confirm(`Delete ${pages.length} page(s) from this document? The remaining pages will be re-OCR'd.`)) {
              act('delete')
            }
          }}
        >
          Delete
        </button>
        <span className="po-spacer" />
        <button
          className="ghost"
          onClick={() =>
            setSelected(
              selected.size === count
                ? new Set()
                : new Set(Array.from({ length: count }, (_, i) => i + 1)),
            )
          }
        >
          {selected.size === count ? 'None' : 'All'}
        </button>
        <button onClick={onClose} disabled={busy}>
          {busy ? 'Working…' : 'Done'}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <div
        className="po-scroller"
        ref={scrollerRef}
        onClick={(e) => {
          const slot = e.target.closest('.po-slot')
          if (slot) toggle(Number(slot.dataset.page))
        }}
      >
        <div className="po-grid po-selectable" data-selected={pages.join(',')} />
      </div>
      <style>{`
        ${pages.map((p) => `.po-slot[data-page="${p}"]`).join(',') || '.po-none'} {
          outline: 3px solid var(--accent);
          outline-offset: -3px;
          border-radius: 4px;
        }
        ${pages.map((p) => `.po-slot[data-page="${p}"] .po-num`).join(',') || '.po-none'} {
          background: var(--accent);
          color: #fff;
        }
      `}</style>
    </div>
  )
}
