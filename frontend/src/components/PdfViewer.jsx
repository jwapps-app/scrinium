import { useEffect, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

const RENDER_MARGIN = 900 // px beyond the viewport to render ahead

/**
 * Lazy PDF viewer: only pages near the viewport render, so a 1,000-page
 * archive opens instantly. Visibility is computed from scroll position
 * (not IntersectionObserver — its callbacks don't fire in hidden documents,
 * e.g. background tabs). `highlightTerms` are literal strings from the
 * server's within-document search, drawn as overlay marks; `focusPage`
 * scrolls that page into view when it changes. `fitMode` ('width'|'page')
 * and `zoom` (multiplier) control page size; the current page is preserved
 * across size changes.
 */
export default function PdfViewer({
  url,
  highlightTerms = [],
  focusPage = null,
  fitMode = 'width',
  zoom = 1,
}) {
  const containerRef = useRef(null)
  const stateRef = useRef(null)
  // Survives rebuilds so zoom/fit changes restore the page you were on.
  const anchorPageRef = useRef(null)
  const [error, setError] = useState('')
  const [ready, setReady] = useState(false)

  const termsKey = highlightTerms.join(' ')

  useEffect(() => {
    let cancelled = false
    const container = containerRef.current
    // Non-null only when this run replaces a previous instance (size change).
    // Computed synchronously from the outgoing slots — scroll handlers are
    // throttled (heavily in background tabs), so their anchor can be stale.
    let restorePage = null
    if (container.childElementCount > 0) {
      const readingLine = window.scrollY + 120
      const current = [...container.children].find(
        (slot) => slot.offsetTop + slot.offsetHeight > readingLine,
      )
      restorePage = current ? Number(current.dataset.page) : anchorPageRef.current
    }
    container.innerHTML = ''
    const st = {
      pdf: null,
      scale: 1,
      rendered: new Set(),
      rendering: new Set(),
      textCache: new Map(),
      slots: [],
      terms: highlightTerms,
      throttle: null,
    }
    stateRef.current = st

    async function renderPage(n) {
      if (st.rendered.has(n) || st.rendering.has(n) || !st.pdf) return
      st.rendering.add(n)
      try {
        const page = await st.pdf.getPage(n)
        if (cancelled) return
        const viewport = page.getViewport({ scale: st.scale })
        const dpr = Math.min(window.devicePixelRatio || 1, 2)
        const canvas = document.createElement('canvas')
        canvas.width = Math.floor(viewport.width * dpr)
        canvas.height = Math.floor(viewport.height * dpr)
        canvas.style.width = `${viewport.width}px`
        canvas.style.height = `${viewport.height}px`
        canvas.className = 'pdf-page'
        const slot = st.slots[n - 1]
        slot.style.height = `${viewport.height}px`
        slot.appendChild(canvas)
        await page.render({
          canvasContext: canvas.getContext('2d'),
          viewport: page.getViewport({ scale: st.scale * dpr }),
        }).promise
        const textContent = await page.getTextContent()
        st.textCache.set(n, { textContent, viewport })
        st.rendered.add(n)
        drawHighlights(n)
        // Estimated slot heights drift as real pages land; once the focused
        // page itself has rendered, snap to its true position exactly once.
        if (st.focusTarget === n) {
          st.focusTarget = null
          slot.scrollIntoView({ block: 'start' })
        }
      } catch (err) {
        console.warn(`pdf page ${n} render failed:`, err)
      } finally {
        st.rendering.delete(n)
      }
    }

    function renderVisible() {
      if (!st.slots.length) return
      const top = window.scrollY - RENDER_MARGIN
      const bottom = window.scrollY + window.innerHeight + RENDER_MARGIN
      let anchored = false
      for (let i = 0; i < st.slots.length; i++) {
        const slot = st.slots[i]
        const y = slot.offsetTop
        if (y > bottom) break
        if (y + slot.offsetHeight >= top) {
          renderPage(i + 1)
          // First slot still under the reading position = current page.
          if (!anchored && y + slot.offsetHeight > window.scrollY + 120) {
            anchorPageRef.current = i + 1
            container.dataset.anchor = i + 1
            anchored = true
          }
        }
      }
    }
    st.renderVisible = renderVisible

    function onScroll() {
      if (st.throttle) return
      st.throttle = setTimeout(() => {
        st.throttle = null
        renderVisible()
      }, 100)
    }
    st.onScroll = onScroll

    function drawHighlights(n) {
      const slot = st.slots[n - 1]
      const cached = st.textCache.get(n)
      if (!slot || !cached) return
      slot.querySelectorAll('.pdf-hl').forEach((el) => el.remove())
      if (!st.terms.length) return
      const { textContent, viewport } = cached
      const lowered = st.terms.map((t) => t.toLowerCase())
      for (const item of textContent.items) {
        if (!item.str) continue
        const hay = item.str.toLowerCase()
        for (let t = 0; t < lowered.length; t++) {
          let idx = hay.indexOf(lowered[t])
          while (idx !== -1) {
            const tx = pdfjsLib.Util.transform(viewport.transform, item.transform)
            const fontHeight = Math.hypot(tx[2], tx[3])
            const itemWidth = item.width * viewport.scale
            const mark = document.createElement('div')
            mark.className = 'pdf-hl'
            mark.style.left = `${tx[4] + (idx / item.str.length) * itemWidth}px`
            mark.style.top = `${tx[5] - fontHeight}px`
            mark.style.width = `${(lowered[t].length / item.str.length) * itemWidth}px`
            mark.style.height = `${fontHeight * 1.15}px`
            slot.appendChild(mark)
            idx = hay.indexOf(lowered[t], idx + 1)
          }
        }
      }
    }
    st.drawHighlights = drawHighlights

    async function load() {
      try {
        const pdf = await pdfjsLib.getDocument(url).promise
        if (cancelled) return
        st.pdf = pdf
        const first = await pdf.getPage(1)
        if (cancelled) return
        const containerWidth = container.clientWidth || 800
        const base = first.getViewport({ scale: 1 })
        if (fitMode === 'page') {
          const chromeHeight =
            document.querySelector('.viewer-chrome')?.offsetHeight || 100
          const availHeight = Math.max(window.innerHeight - chromeHeight - 32, 300)
          st.scale = Math.min(availHeight / base.height, containerWidth / base.width)
        } else {
          st.scale = containerWidth / base.width
        }
        st.scale *= zoom
        const width = base.width * st.scale
        const estHeight = base.height * st.scale

        for (let n = 1; n <= pdf.numPages; n++) {
          const slot = document.createElement('div')
          slot.className = 'pdf-page-slot'
          slot.dataset.page = n
          slot.style.width = `${width}px`
          slot.style.height = `${estHeight}px`
          container.appendChild(slot)
          st.slots.push(slot)
        }
        window.addEventListener('scroll', onScroll, { passive: true })
        window.addEventListener('resize', onScroll, { passive: true })
        // Rebuild from a size change: return to the page being read.
        if (restorePage && st.slots[restorePage - 1]) {
          st.slots[restorePage - 1].scrollIntoView({ block: 'start' })
        }
        renderVisible()
        if (!cancelled) setReady(true)
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to open PDF')
      }
    }
    load()

    return () => {
      cancelled = true
      window.removeEventListener('scroll', st.onScroll)
      window.removeEventListener('resize', st.onScroll)
      if (st.throttle) clearTimeout(st.throttle)
      st.pdf?.destroy()
    }
  }, [url, fitMode, zoom]) // eslint-disable-line react-hooks/exhaustive-deps

  // Re-draw highlights on already-rendered pages when the terms change.
  useEffect(() => {
    const st = stateRef.current
    if (!st) return
    st.terms = highlightTerms
    for (const n of st.rendered) st.drawHighlights(n)
  }, [termsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // Scroll to the focused page. Instant, not smooth: rendering nearby pages
  // shifts layout, and Chrome cancels an in-flight smooth scroll on layout
  // change. A follow-up correction lands exactly after heights settle.
  useEffect(() => {
    const st = stateRef.current
    if (!ready || !st || !focusPage) return
    const slot = st.slots[focusPage - 1]
    if (!slot) return
    st.focusTarget = st.rendered.has(focusPage) ? null : focusPage
    slot.scrollIntoView({ block: 'start' })
    st.renderVisible()
  }, [focusPage, ready])

  return (
    <div className="pdf-viewer">
      {error && <p className="error">{error}</p>}
      <div ref={containerRef} className="pdf-pages" />
    </div>
  )
}
