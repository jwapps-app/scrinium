import { cpSync, mkdirSync } from 'node:fs'

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// pdf.js decodes JPEG 2000 and JBIG2 with OpenJPEG compiled to WebAssembly,
// loaded at runtime from `wasmUrl` rather than bundled. Copy those files into
// public/ so they are served in dev and emitted into dist/ on build. Without
// them every JPX document renders as a blank white page — silently, because
// pdf.js only logs a warning and paints nothing.
mkdirSync('public/pdfjs', { recursive: true })
cpSync('node_modules/pdfjs-dist/wasm', 'public/pdfjs', { recursive: true })

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8010',
    },
  },
})
