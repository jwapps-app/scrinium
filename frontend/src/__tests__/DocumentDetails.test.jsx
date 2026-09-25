import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import DocumentDetails from '../components/DocumentDetails'

const patches = []

vi.mock('../api', () => ({
  apiJson: vi.fn(async (path, options = {}) => {
    if (options.method === 'PATCH') {
      patches.push({ path, body: JSON.parse(options.body) })
      return { id: path.split('/').pop(), tags: [], custom_values: {} }
    }
    return []
  }),
}))

const docA = { id: 'a', title: 'A', notes: 'note A', tags: [], custom_values: {} }
const docB = { id: 'b', title: 'B', notes: 'note B', tags: [], custom_values: {} }

function strip(doc) {
  // Keyed by document id, as Review and DocumentView render it.
  return <DocumentDetails key={doc.id} doc={doc} onChange={() => {}} />
}

describe('DocumentDetails across documents', () => {
  beforeEach(() => {
    patches.length = 0
  })
  afterEach(cleanup)

  it('shows the next document’s notes and never patches it with the previous text', async () => {
    const { rerender, getByPlaceholderText } = render(strip(docA))
    await act(async () => {})

    const notesA = getByPlaceholderText('Add a note…')
    expect(notesA.value).toBe('note A')
    fireEvent.change(notesA, { target: { value: 'typed while on A' } })
    expect(notesA.value).toBe('typed while on A')

    // Move on without blurring — the case a keyboard-driven flow hits.
    rerender(strip(docB))
    await act(async () => {})

    const notesB = getByPlaceholderText('Add a note…')
    expect(notesB.value).toBe('note B')

    // Leaving the field on B must not save A’s text against B.
    fireEvent.blur(notesB)
    await act(async () => {})

    const toB = patches.filter((p) => p.path === '/api/documents/b')
    expect(toB).toEqual([])
    expect(patches.some((p) => p.body.notes === 'typed while on A' && p.path !== '/api/documents/a')).toBe(false)
  })

  it('saves an edited note to the document it was typed on', async () => {
    const { getByPlaceholderText } = render(strip(docA))
    await act(async () => {})

    const notes = getByPlaceholderText('Add a note…')
    fireEvent.change(notes, { target: { value: 'typed while on A' } })
    fireEvent.blur(notes)
    await act(async () => {})

    expect(patches).toEqual([
      { path: '/api/documents/a', body: { notes: 'typed while on A' } },
    ])
  })
})
