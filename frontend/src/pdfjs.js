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

/** Load a PDF with the decoders wired up. Always use this, never getDocument. */
export function loadPdf(url) {
  return pdfjsLib.getDocument({ url, wasmUrl: WASM_URL }).promise
}

export { pdfjsLib }
export default pdfjsLib
