import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { loadPdf } from '../pdfjs'
import { apiFetch } from '../api'

const RENDER_MARGIN = 700

/**
 * One document pane for the side-by-side compare view. Unlike PdfViewer this
 * scrolls inside its OWN container (not the window), so two panes coexist.
 * Pages are pre-sized from their real heights and rendered lazily; the parent
 * drives scroll sync through the imperative handle (getPosition / scrollTo).
 */
const ComparePane = forwardRef(function ComparePane({ docId, onScroll }, ref) {
  const scrollerRef = useRef(null)
  const stateRef = useRef({ pdf: null, offsets: [], heights: [], slots: [], rendered: new Set() })
  const urlRef = useRef(null)
  // True while WE are setting scrollTop, so the resulting scroll event is
  // recognized as our own echo and not fed back to the parent as a user scroll.
  const programmaticRef = useRef(false)
  // Always call the latest onScroll: the scroll listener is bound once (per
  // docId), but the parent swaps this callback when the lock toggles.
  const onScrollRef = useRef(onScroll)
  onScrollRef.current = onScroll
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useImperativeHandle(ref, () => ({
    // Reading position as a continuous page coordinate (page index + fraction).
    getPosition() {
      const st = stateRef.current
      const scroller = scrollerRef.current
      if (!scroller || !st.offsets.length) return { page: 0, fraction: 0 }
      const y = scroller.scrollTop
      let page = st.offsets.findIndex(
        (top, i) => y < (st.offsets[i + 1] ?? Infinity),
      )
      if (page < 0) page = st.offsets.length - 1
      const within = (y - st.offsets[page]) / (st.heights[page] || 1)
      return { page, fraction: Math.min(Math.max(within, 0), 1) }
    },
    scrollToPosition({ page, fraction }) {
      const st = stateRef.current
      const scroller = scrollerRef.current
      if (!scroller || !st.offsets.length) return
      const p = Math.min(Math.max(page, 0), st.offsets.length - 1)
      const target = st.offsets[p] + (fraction || 0) * (st.heights[p] || 0)
      if (Math.abs(scroller.scrollTop - target) < 1) return // already there
      programmaticRef.current = true
      scroller.scrollTop = target
    },
    pageCount() {
      return stateRef.current.offsets.length
    },
  }))

  useEffect(() => {
    let cancelled = false
    const scroller = scrollerRef.current
    const st = stateRef.current

    async function build() {
      try {
        const resp = await apiFetch(`/api/documents/${docId}/file`)
        if (!resp.ok) throw new Error('Could not load document')
        const blob = await resp.blob()
        if (cancelled) return
        urlRef.current = URL.createObjectURL(blob)
        const pdf = await loadPdf(urlRef.current)
        if (cancelled) return
        st.pdf = pdf

        const width = scroller.clientWidth || 400
        scroller.innerHTML = ''
        let top = 0
        for (let n = 1; n <= pdf.numPages; n++) {
          const page = await pdf.getPage(n)
          if (cancelled) return
          const base = page.getViewport({ scale: 1 })
          const scale = width / base.width
          const h = base.height * scale
          const slot = document.createElement('div')
          slot.className = 'compare-page'
          slot.style.height = `${h}px`
          slot.dataset.page = n
          scroller.appendChild(slot)
          st.slots.push(slot)
          st.offsets.push(top)
          st.heights.push(h)
          top += h
        }
        setLoading(false)
        renderVisible()
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }

    async function renderPage(n) {
      const st2 = stateRef.current
      if (st2.rendered.has(n)) return
      const slot = st2.slots[n - 1]
      if (!slot) return
      st2.rendered.add(n)
      try {
        const page = await st2.pdf.getPage(n)
        const width = slot.clientWidth
        const base = page.getViewport({ scale: 1 })
        const scale = width / base.width
        const viewport = page.getViewport({ scale: scale * (window.devicePixelRatio || 1) })
        const canvas = document.createElement('canvas')
        canvas.width = viewport.width
        canvas.height = viewport.height
        canvas.style.width = '100%'
        canvas.style.height = '100%'
        slot.appendChild(canvas)
        await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise
      } catch {
        st2.rendered.delete(n)
      }
    }

    function renderVisible() {
      const st2 = stateRef.current
      if (!st2.slots.length) return
      const y = scroller.scrollTop - RENDER_MARGIN
      const bottom = scroller.scrollTop + scroller.clientHeight + RENDER_MARGIN
      for (let i = 0; i < st2.slots.length; i++) {
        const t = st2.offsets[i]
        if (t > bottom) break
        if (t + st2.heights[i] >= y) renderPage(i + 1)
      }
    }

    function handleScroll() {
      renderVisible()
      if (programmaticRef.current) {
        programmaticRef.current = false // our own echo — don't loop it back
        return
      }
      onScrollRef.current?.()
    }
    scroller.addEventListener('scroll', handleScroll, { passive: true })
    build()
    return () => {
      cancelled = true
      scroller.removeEventListener('scroll', handleScroll)
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
      stateRef.current = { pdf: null, offsets: [], heights: [], slots: [], rendered: new Set() }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId])

  return (
    <div className="compare-pane">
      {error && <p className="error">{error}</p>}
      {loading && !error && <div className="compare-loading">Loading…</div>}
      <div className="compare-scroller" ref={scrollerRef} />
    </div>
  )
})

export default ComparePane
