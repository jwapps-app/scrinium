/**
 * One place that knows how to load a PDF.
 *
 * The worker URL used to be repeated in three components, and the WASM URL was
 * missing entirely — which silently broke every JPEG 2000 document in the
 * library. pdf.js decodes JPX (and JBIG2) with OpenJPEG compiled to WASM, and
 * without `wasmUrl` it fails with "OpenJPEG failed to initialize", paints
 * nothing, and leaves a transparent canvas. On screen that reads as a blank
 * white page, which looks like a broken file rather than a missing decoder.
 * Roughly a third of this library is JPEG 2000, so it was not a rare case.
 */
import * as pdfjsLib from 'pdfjs-dist'

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

// Copied out of the package by vite.config.js so it is served as a static
// asset; a bare path keeps it working under the nginx root the app ships with.
export const WASM_URL = '/pdfjs/'

/** Start loading a PDF with the decoders wired up, returning the loading
 * task so the caller can destroy() it — that tears down the worker and the
 * document together, including a load still in progress. */
export function openPdf(url) {
  return pdfjsLib.getDocument({ url, wasmUrl: WASM_URL })
}

/** Load a PDF with the decoders wired up. Always use this, never getDocument. */
export function loadPdf(url) {
  return openPdf(url).promise
}

/** Release a document from loadPdf. pdf.js 6 removed destroy() from the
 * document object; the loading task behind it still has one, and that is
 * what tears down the worker. Calling the old method threw on every exit
 * from the viewer, inside React's unmount, which took the whole page down
 * with it. Safe on null and on a document already gone. */
export function closePdf(pdf) {
  const task = pdf?.loadingTask
  if (!task) return
  try {
    const r = task.destroy()
    if (r && typeof r.catch === 'function') r.catch(() => {})
  } catch {
    /* already destroyed */
  }
}

export { pdfjsLib }
export default pdfjsLib
