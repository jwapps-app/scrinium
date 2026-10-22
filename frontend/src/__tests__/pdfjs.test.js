import { describe, expect, it, vi } from 'vitest'

// pdf.js 6 dropped destroy() from the document object. The viewer kept
// calling it on unmount, the TypeError fired inside React's cleanup, and
// leaving any document took the whole app to a blank page until a reload.
vi.mock('pdfjs-dist', () => {
  const task = {
    destroy: vi.fn(() => Promise.resolve()),
    promise: null,
  }
  const doc = { loadingTask: task, getPage: vi.fn() }
  task.promise = Promise.resolve(doc)
  return {
    GlobalWorkerOptions: {},
    getDocument: vi.fn(() => task),
  }
})

import { closePdf, loadPdf } from '../pdfjs'

describe('closePdf', () => {
  it('tears down through the loading task, which is what pdf.js 6 still has', async () => {
    const pdf = await loadPdf('/x.pdf')
    expect(typeof pdf.destroy).toBe('undefined')
    closePdf(pdf)
    expect(pdf.loadingTask.destroy).toHaveBeenCalledTimes(1)
  })

  it('is safe on nothing and on a document whose task is already gone', () => {
    expect(() => closePdf(null)).not.toThrow()
    expect(() => closePdf(undefined)).not.toThrow()
    expect(() => closePdf({})).not.toThrow()
    const failing = { loadingTask: { destroy: () => Promise.reject(new Error('gone')) } }
    expect(() => closePdf(failing)).not.toThrow()
    const throwing = { loadingTask: { destroy: () => { throw new Error('gone') } } }
    expect(() => closePdf(throwing)).not.toThrow()
  })
})
