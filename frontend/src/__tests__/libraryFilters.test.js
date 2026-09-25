import { describe, expect, it } from 'vitest'
import {
  FILTER_PARAMS,
  bulkFilter,
  hasAnyFilter,
  listQuery,
  readFilters,
  serializeView,
} from '../libraryFilters'

// Every filter the UI knows, all set at once.
const EVERYTHING = {
  status: 'ready',
  tag: 'tag-1',
  correspondent: 'corr-1',
  doctype: 'type-1',
  engine: 'apple',
  from: '2024-01-01',
  to: '2024-12-31',
  expiring: '1',
  non_pdfa: '1',
  untagged: '1',
  sort: 'title',
  q: 'invoice',
  view: 'list',
}

describe('saved views', () => {
  it('round-trips every filter, including untagged, expiring and non_pdfa', () => {
    const original = new URLSearchParams(EVERYTHING)
    const stored = serializeView(original)
    const reopened = new URLSearchParams(stored)

    expect(readFilters(reopened)).toEqual(readFilters(original))
    expect(readFilters(reopened)).toMatchObject({
      untagged: true,
      expiring: true,
      nonPdfa: true,
    })
    for (const key of FILTER_PARAMS) expect(reopened.get(key)).toBe(EVERYTHING[key])
    expect(reopened.get('sort')).toBe('title')
    expect(reopened.get('q')).toBe('invoice')
    expect(reopened.get('view')).toBe('list')
  })

  it('does not carry parameters that are not part of the view', () => {
    const stored = serializeView(new URLSearchParams({ tag: 'tag-1', shared: '1', foo: 'bar' }))
    expect(stored).toBe('tag=tag-1')
  })
})

describe('list and bulk scopes agree', () => {
  it('sends the same filters to the list and the bulk endpoints', () => {
    const f = readFilters(new URLSearchParams(EVERYTHING))
    expect(hasAnyFilter(f)).toBe(true)

    const list = new URLSearchParams(listQuery(f, { sort: 'newest', offset: 0, limit: 100 }))
    const bulk = bulkFilter(f)

    expect(bulk).toEqual({
      status_filter: 'ready',
      tag_id: 'tag-1',
      correspondent_id: 'corr-1',
      doc_type_id: 'type-1',
      engine: 'apple',
      date_from: '2024-01-01',
      date_to: '2024-12-31',
      expiring: true,
      non_pdfa: true,
      untagged: true,
    })
    expect(list.get('status_filter')).toBe(bulk.status_filter)
    expect(list.get('tag_id')).toBe(bulk.tag_id)
    expect(list.get('correspondent_id')).toBe(bulk.correspondent_id)
    expect(list.get('doc_type_id')).toBe(bulk.doc_type_id)
    expect(list.get('engine')).toBe(bulk.engine)
    expect(list.get('date_from')).toBe(bulk.date_from)
    expect(list.get('date_to')).toBe(bulk.date_to)
    expect(list.get('expiring')).toBe('true')
    expect(list.get('non_pdfa')).toBe('true')
    expect(list.get('untagged')).toBe('true')
  })

  it('maps the processing bucket to the whole in-flight queue in both', () => {
    const f = readFilters(new URLSearchParams({ status: 'processing' }))
    expect(bulkFilter(f).status_filter).toBe('pending,processing')
    const list = new URLSearchParams(listQuery(f, { sort: 'newest', offset: 0, limit: 100 }))
    expect(list.get('status_filter')).toBe('pending,processing')
  })

  it('reports no filters for a bare library URL', () => {
    expect(hasAnyFilter(readFilters(new URLSearchParams({ sort: 'title', view: 'list' })))).toBe(
      false,
    )
  })
})
