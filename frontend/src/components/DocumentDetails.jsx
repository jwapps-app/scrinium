import { useEffect, useState } from 'react'
import { apiJson } from '../api'

/**
 * The document's metadata strip: date, correspondent, type, tags, and
 * custom fields — all editable in place, saving on change.
 */
export default function DocumentDetails({ doc, onChange }) {
  const [correspondents, setCorrespondents] = useState([])
  const [docTypes, setDocTypes] = useState([])
  const [fields, setFields] = useState([])
  const [error, setError] = useState('')
  // "Copy tags from…": pick an existing document and mirror its tags here.
  const [copyOpen, setCopyOpen] = useState(false)
  const [copyQuery, setCopyQuery] = useState('')
  const [copyResults, setCopyResults] = useState([])

  useEffect(() => {
    apiJson('/api/correspondents').then(setCorrespondents).catch(() => {})
    apiJson('/api/doc-types').then(setDocTypes).catch(() => {})
    apiJson('/api/custom-fields').then(setFields).catch(() => {})
  }, [])

  // Debounced title search for the copy-from picker. Title-only (not
  // full-text) so typing a title doesn't surface every document that merely
  // mentions it.
  useEffect(() => {
    if (!copyOpen) return
    const q = copyQuery.trim()
    if (!q) {
      setCopyResults([])
      return
    }
    let cancelled = false
    const timer = setTimeout(() => {
      apiJson(`/api/documents?title_q=${encodeURIComponent(q)}&limit=8`)
        .then((r) => {
          if (!cancelled) setCopyResults(r.items.filter((d) => d.id !== doc.id))
        })
        .catch(() => {
          if (!cancelled) setCopyResults([])
        })
    }, 250)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [copyQuery, copyOpen, doc.id])

  function closeCopy() {
    setCopyOpen(false)
    setCopyQuery('')
    setCopyResults([])
  }

  async function copyTagsFrom(sourceId) {
    setError('')
    try {
      const updated = await apiJson(`/api/documents/${doc.id}/copy-tags`, {
        method: 'POST',
        body: JSON.stringify({ source_id: sourceId }),
      })
      onChange(updated)
      window.dispatchEvent(new Event('library-changed'))
      closeCopy()
    } catch (err) {
      setError(err.message)
    }
  }

  async function patch(body) {
    setError('')
    try {
      const updated = await apiJson(`/api/documents/${doc.id}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      })
      onChange(updated)
      window.dispatchEvent(new Event('library-changed'))
    } catch (err) {
      setError(err.message)
    }
  }

  async function pickEntity(kind, value) {
    // value is an id, '' (clear), or '__new__'
    const isCorrespondent = kind === 'correspondent'
    if (value === '__new__') {
      const name = window.prompt(
        isCorrespondent ? 'New correspondent name:' : 'New document type:',
      )
      if (!name?.trim()) return
      const created = await apiJson(
        isCorrespondent ? '/api/correspondents' : '/api/doc-types',
        { method: 'POST', body: JSON.stringify({ name: name.trim() }) },
      )
      if (isCorrespondent) setCorrespondents((c) => [...c, created])
      else setDocTypes((t) => [...t, created])
      await patch(
        isCorrespondent
          ? { correspondent_id: created.id }
          : { doc_type_id: created.id },
      )
      return
    }
    if (value === '') {
      await patch(isCorrespondent ? { clear_correspondent: true } : { clear_doc_type: true })
    } else {
      await patch(isCorrespondent ? { correspondent_id: value } : { doc_type_id: value })
    }
  }

  return (
    <div className="doc-details">
      <label className="detail">
        <span className="detail-label">Date</span>
        <input
          type="date"
          value={doc.doc_date || ''}
          onChange={(e) =>
            patch(
              e.target.value
                ? { doc_date: e.target.value }
                : { clear_doc_date: true },
            )
          }
          title="The document's own date (extracted automatically, editable)"
        />
      </label>

      <label className="detail">
        <span className="detail-label">Expires</span>
        <input
          type="date"
          defaultValue={doc.expires_on || ''}
          onBlur={(e) => {
            const current = doc.expires_on || ''
            if (e.target.value !== current) {
              patch(e.target.value ? { expires_on: e.target.value } : { clear_expires: true })
            }
          }}
        />
      </label>

      <label className="detail">
        <span className="detail-label">From</span>
        <select
          value={doc.correspondent_id || ''}
          onChange={(e) => pickEntity('correspondent', e.target.value)}
        >
          <option value="">—</option>
          {correspondents.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
          <option value="__new__">+ New correspondent…</option>
        </select>
      </label>

      <label className="detail">
        <span className="detail-label">Type</span>
        <select
          value={doc.doc_type_id || ''}
          onChange={(e) => pickEntity('doctype', e.target.value)}
        >
          <option value="">—</option>
          {docTypes.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
          <option value="__new__">+ New type…</option>
        </select>
      </label>

      {fields.map((field) => (
        <label className="detail" key={field.id}>
          <span className="detail-label">{field.name}</span>
          <input
            type={
              field.kind === 'date'
                ? 'date'
                : field.kind === 'number' || field.kind === 'money'
                  ? 'number'
                  : field.kind === 'url'
                    ? 'url'
                    : 'text'
            }
            step={field.kind === 'money' ? '0.01' : undefined}
            defaultValue={doc.custom_values?.[field.id] || ''}
            onBlur={(e) => {
              const current = doc.custom_values?.[field.id] || ''
              if (e.target.value !== current)
                patch({ custom_values: { [field.id]: e.target.value } })
            }}
          />
        </label>
      ))}

      <span className="detail detail-tags">
        {doc.tags.map((t) => (
          <span
            key={t.id}
            className="chip chip-tag"
            style={
              t.color
                ? {
                    background: `color-mix(in srgb, ${t.color} 22%, transparent)`,
                    borderColor: t.color,
                    color: t.color,
                  }
                : undefined
            }
          >
            {t.name}
          </span>
        ))}
        {copyOpen ? (
          <span className="tag-copy">
            <input
              autoFocus
              placeholder="Find a document by title…"
              value={copyQuery}
              onChange={(e) => setCopyQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') closeCopy()
              }}
            />
            <button type="button" className="ghost" onClick={closeCopy}>
              Cancel
            </button>
            {copyResults.length > 0 && (
              <span className="tag-copy-results">
                {copyResults.map((d) => (
                  <button
                    key={d.id}
                    type="button"
                    className="tag-copy-hit"
                    onClick={() => copyTagsFrom(d.id)}
                  >
                    <span className="tag-copy-title">{d.title}</span>
                    <span className="tag-copy-tags">
                      {d.tags.length
                        ? d.tags.map((t) => t.name).join(' · ')
                        : 'no tags'}
                    </span>
                  </button>
                ))}
              </span>
            )}
          </span>
        ) : (
          <button type="button" className="ghost" onClick={() => setCopyOpen(true)}>
            Copy tags from…
          </button>
        )}
      </span>

      <label className="detail detail-notes">
        <span className="detail-label">Notes</span>
        <textarea
          rows={1}
          placeholder="Add a note…"
          defaultValue={doc.notes || ''}
          onBlur={(e) => {
            if (e.target.value !== (doc.notes || '')) patch({ notes: e.target.value })
          }}
        />
      </label>

      {error && <span className="error">{error}</span>}
    </div>
  )
}
