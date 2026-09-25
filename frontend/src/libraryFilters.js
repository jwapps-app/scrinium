/**
 * The library's filter vocabulary, in one place.
 *
 * The URL is the source of truth for what the list shows. Three things have
 * to agree on which parameters count as filters — the list query, the bulk
 * "entire filter" scope, and saved views — and when they were three separate
 * lists a saved "untagged" view reopened as the whole library.
 */

// Parameters that narrow the list. Every one of these is a filter the bulk
// endpoint can express.
export const FILTER_PARAMS = [
  'status',
  'tag',
  'correspondent',
  'doctype',
  'engine',
  'from',
  'to',
  'expiring',
  'non_pdfa',
  'untagged',
]

// Everything a saved view remembers: the filters plus how they are shown.
export const VIEW_PARAMS = [...FILTER_PARAMS, 'sort', 'q', 'view']

/** Read the filter state out of a URLSearchParams. */
export function readFilters(params) {
  return {
    status: params.get('status') || null,
    tag: params.get('tag') || null,
    correspondent: params.get('correspondent') || null,
    doctype: params.get('doctype') || null,
    engine: params.get('engine') || null,
    from: params.get('from') || null,
    to: params.get('to') || null,
    expiring: params.get('expiring') === '1',
    nonPdfa: params.get('non_pdfa') === '1',
    untagged: params.get('untagged') === '1',
  }
}

export function hasAnyFilter(f) {
  return Boolean(
    f.status ||
      f.tag ||
      f.correspondent ||
      f.doctype ||
      f.engine ||
      f.from ||
      f.to ||
      f.expiring ||
      f.nonPdfa ||
      f.untagged,
  )
}

// The sidebar's "Processing" bucket covers the whole in-flight queue.
function statusFilter(status) {
  if (status === 'processing') return 'pending,processing'
  return status || null
}

/** The filter in the shape the bulk, binder and zip endpoints take. It has
 * to mirror listQuery exactly: if the two drift, "all N in filter" acts on a
 * different set than the one on screen. */
export function bulkFilter(f) {
  return {
    status_filter: statusFilter(f.status),
    tag_id: f.tag,
    correspondent_id: f.correspondent,
    doc_type_id: f.doctype,
    engine: f.engine,
    date_from: f.from,
    date_to: f.to,
    expiring: f.expiring,
    non_pdfa: f.nonPdfa,
    untagged: f.untagged,
  }
}

/** Query string for GET /api/documents — one builder for the first page and
 * every "Show more" page, so a later page always belongs to the list above. */
export function listQuery(f, { sort, offset, limit }) {
  const search = new URLSearchParams()
  const status = statusFilter(f.status)
  if (status) search.set('status_filter', status)
  if (f.tag) search.set('tag_id', f.tag)
  if (f.correspondent) search.set('correspondent_id', f.correspondent)
  if (f.doctype) search.set('doc_type_id', f.doctype)
  if (f.engine) search.set('engine', f.engine)
  if (f.expiring) search.set('expiring', 'true')
  if (f.nonPdfa) search.set('non_pdfa', 'true')
  if (f.untagged) search.set('untagged', 'true')
  if (f.from) search.set('date_from', f.from)
  if (f.to) search.set('date_to', f.to)
  search.set('sort', sort)
  search.set('offset', String(offset))
  search.set('limit', String(limit))
  return search.toString()
}

/** What a saved view stores: the current URL reduced to the parameters that
 * describe the view. */
export function serializeView(params) {
  const keep = new URLSearchParams()
  for (const key of VIEW_PARAMS) {
    const value = params.get(key)
    if (value) keep.set(key, value)
  }
  return keep.toString()
}
