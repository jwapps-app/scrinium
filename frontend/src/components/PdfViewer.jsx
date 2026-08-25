import { useEffect, useRef, useState } from 'react'
import { pdfjsLib, loadPdf } from '../pdfjs'
import { TextLayer } from 'pdfjs-dist'

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
  storageKey = null,
  onOutline = null,
  annotations = [],
  onSelectText = null,
  onPositionChange = null,
  resumePage = null,
}) {
  const containerRef = useRef(null)
  const stateRef = useRef(null)
  // The parsed PDF, kept across zoom/fit rebuilds: re-parsing a large blob on
  // every zoom click is very expensive. Destroyed only when url changes.
  const pdfRef = useRef({ url: null, pdf: null })
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
      annots: annotations,
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
        // Selectable/copyable text over the canvas. Errors here cost only
        // selection, never the page render.
        try {
          const layer = document.createElement('div')
          layer.className = 'pdf-text-layer'
          layer.style.setProperty('--scale-factor', st.scale)
          slot.appendChild(layer)
          await new TextLayer({
            textContentSource: textContent,
            container: layer,
            viewport,
          }).render()
        } catch (err) {
          console.warn(`text layer for page ${n} failed:`, err)
        }
        st.rendered.add(n)
        drawHighlights(n)
        drawAnnotations(n)
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
            // Remember where reading stopped; resume on next open.
            if (storageKey && st.slots.length > 3) {
              try {
                localStorage.setItem(
                  `readpos:${storageKey}`,
                  JSON.stringify({ page: i + 1, total: st.slots.length }),
                )
              } catch { /* storage full/blocked: not worth breaking scroll */ }
              if (onPositionChange) onPositionChange(i + 1)
            }
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

    function drawAnnotations(n) {
      const slot = st.slots[n - 1]
      if (!slot) return
      slot.querySelectorAll('.pdf-annot').forEach((el) => el.remove())
      for (const a of st.annots || []) {
        if (a.page !== n) continue
        for (const r of a.rects) {
          const mark = document.createElement('div')
          mark.className = 'pdf-annot'
          mark.style.left = `${r.x * 100}%`
          mark.style.top = `${r.y * 100}%`
          mark.style.width = `${r.w * 100}%`
          mark.style.height = `${r.h * 100}%`
          if (a.color) mark.style.background = `color-mix(in srgb, ${a.color} 35%, transparent)`
          slot.appendChild(mark)
        }
      }
    }
    st.drawAnnotations = drawAnnotations

    async function load() {
      try {
        let pdf = pdfRef.current.url === url ? pdfRef.current.pdf : null
        if (!pdf) {
          pdf = await loadPdf(url)
          if (cancelled) {
            pdf.destroy()
            return
          }
          pdfRef.current = { url, pdf }
        }
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
        container.addEventListener('mouseup', st.onMouseUp)
        // Rebuild from a size change: return to the page being read.
        // Otherwise, a fresh open resumes where reading last stopped.
        let target = restorePage
        if (!target && !focusPage && resumePage > 1 && resumePage <= pdf.numPages) {
          target = resumePage
        }
        if (!target && storageKey && !focusPage) {
          try {
            const saved = JSON.parse(localStorage.getItem(`readpos:${storageKey}`))
            if (saved?.page > 1 && saved.page <= pdf.numPages) target = saved.page
          } catch { /* no saved position */ }
        }
        if (target && st.slots[target - 1]) {
          st.focusTarget = target
          st.slots[target - 1].scrollIntoView({ block: 'start' })
        }
        renderVisible()
        if (!cancelled) setReady(true)

        // Table of contents, if the PDF carries one. Flattened with depth
        // so the caller can indent; page numbers resolved best-effort.
        if (onOutline) {
          try {
            const outline = await pdf.getOutline()
            if (outline?.length && !cancelled) {
              const flat = []
              async function walk(items, depth) {
                for (const item of items) {
                  if (flat.length >= 200) return
                  let page = null
                  try {
                    let dest = item.dest
                    if (typeof dest === 'string') dest = await pdf.getDestination(dest)
                    if (Array.isArray(dest) && dest[0]) {
                      page = (await pdf.getPageIndex(dest[0])) + 1
                    }
                  } catch { /* unresolvable entry */ }
                  if (item.title) flat.push({ title: item.title, page, depth })
                  if (item.items?.length && depth < 2) await walk(item.items, depth + 1)
                }
              }
              await walk(outline, 0)
              if (!cancelled && flat.some((i) => i.page)) onOutline(flat)
            }
          } catch { /* no outline */ }
        }
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to open PDF')
      }
    }
    load()

    st.onMouseUp = () => {
      if (!onSelectText) return
      setTimeout(() => {
        const sel = window.getSelection()
        const quote = sel?.toString().trim()
        if (!quote || quote.length < 3) {
          onSelectText(null)
          return
        }
        const range = sel.getRangeAt(0)
        const slot = range.startContainer.parentElement?.closest('.pdf-page-slot')
        if (!slot) return
        const page = Number(slot.dataset.page)
        const slotBox = slot.getBoundingClientRect()
        const rects = []
        for (const r of range.getClientRects()) {
          if (r.width < 2 || r.height < 2) continue
          rects.push({
            x: (r.left - slotBox.left) / slotBox.width,
            y: (r.top - slotBox.top) / slotBox.height,
            w: r.width / slotBox.width,
            h: r.height / slotBox.height,
          })
          if (rects.length >= 150) break
        }
        if (!rects.length) return
        const last = range.getClientRects()[range.getClientRects().length - 1]
        onSelectText({
          page,
          quote: quote.slice(0, 2000),
          rects,
          anchorX: last.right,
          anchorY: last.bottom,
        })
      }, 0)
    }

    return () => {
      cancelled = true
      window.removeEventListener('scroll', st.onScroll)
      window.removeEventListener('resize', st.onScroll)
      container.removeEventListener('mouseup', st.onMouseUp)
      if (st.throttle) clearTimeout(st.throttle)
    }
  }, [url, fitMode, zoom]) // eslint-disable-line react-hooks/exhaustive-deps

  // Destroy the parsed PDF only when the document itself changes (or on
  // unmount) — zoom/fit rebuilds above reuse it.
  useEffect(
    () => () => {
      pdfRef.current.pdf?.destroy()
      pdfRef.current = { url: null, pdf: null }
    },
    [url],
  )

  // Re-draw highlights on already-rendered pages when the terms change.
  useEffect(() => {
    const st = stateRef.current
    if (!st) return
    st.terms = highlightTerms
    for (const n of st.rendered) st.drawHighlights(n)
  }, [termsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // Re-draw saved highlights when the annotation list changes.
  useEffect(() => {
    const st = stateRef.current
    if (!st) return
    st.annots = annotations
    if (st.drawAnnotations) {
      for (const n of st.rendered) st.drawAnnotations(n)
    }
  }, [annotations]) // eslint-disable-line react-hooks/exhaustive-deps

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
