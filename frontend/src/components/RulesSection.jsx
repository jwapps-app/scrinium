import { useCallback, useEffect, useState } from 'react'
import { apiJson } from '../api'
import { useIsAdmin } from '../session'

const EMPTY = { name: '', match_type: 'contains', pattern: '', tag_name: '', set_title: '', correspondent_name: '', doc_type_name: '' }

export default function RulesSection() {
  // Rules rewrite titles and tags across the whole library, so only the owner
  // may change them; everyone can see what is configured.
  const isAdmin = useIsAdmin()
  const [rules, setRules] = useState([])
  const [tags, setTags] = useState([])
  const [correspondents, setCorrespondents] = useState([])
  const [docTypes, setDocTypes] = useState([])
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      const [ruleData, tagData, corrData, typeData] = await Promise.all([
        apiJson('/api/rules'),
        apiJson('/api/tags'),
        apiJson('/api/correspondents'),
        apiJson('/api/doc-types'),
      ])
      setRules(ruleData)
      setTags(tagData)
      setCorrespondents(corrData)
      setDocTypes(typeData)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const tagName = (id) => tags.find((t) => t.id === id)?.name

  function set(field) {
    return (e) => setForm({ ...form, [field]: e.target.value })
  }

  async function addRule(e) {
    e.preventDefault()
    setError('')
    try {
      let tagId = null
      if (form.tag_name.trim()) {
        const tag = await apiJson('/api/tags', {
          method: 'POST',
          body: JSON.stringify({ name: form.tag_name.trim() }),
        })
        tagId = tag.id
      }
      let correspondentId = null
      if (form.correspondent_name.trim()) {
        const corr = await apiJson('/api/correspondents', {
          method: 'POST',
          body: JSON.stringify({ name: form.correspondent_name.trim() }),
        })
        correspondentId = corr.id
      }
      let docTypeId = null
      if (form.doc_type_name.trim()) {
        const dtype = await apiJson('/api/doc-types', {
          method: 'POST',
          body: JSON.stringify({ name: form.doc_type_name.trim() }),
        })
        docTypeId = dtype.id
      }
      await apiJson('/api/rules', {
        method: 'POST',
        body: JSON.stringify({
          name: form.name,
          match_type: form.match_type,
          pattern: form.pattern,
          tag_id: tagId,
          set_title: form.set_title.trim() || null,
          correspondent_id: correspondentId,
          doc_type_id: docTypeId,
        }),
      })
      setForm(EMPTY)
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function toggleRule(rule) {
    try {
      await apiJson(`/api/rules/${rule.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !rule.enabled }),
      })
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function deleteRule(rule) {
    try {
      await apiJson(`/api/rules/${rule.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function classifyAll() {
    setError('')
    setNotice('Classifying…')
    try {
      // The pass runs in the background now — on a large library it reads
      // every document's text and outlives any request — so poll for it.
      await apiJson('/api/classify/run', { method: 'POST' })
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 2000))
        const s = await apiJson('/api/classify/run')
        if (s.state === 'done') {
          setNotice(`Classified ${s.examined} documents; ${s.changed} changed.`)
          break
        }
        if (s.state === 'failed') {
          setNotice('')
          setError(s.error || 'Classification failed')
          break
        }
        setNotice(`Classifying… ${s.examined ?? 0} of ${s.total ?? '?'}`)
      }
    } catch (err) {
      setNotice('')
      setError(err.message)
    }
  }

  return (
    <section className="settings-section">
      <h2>Classification rules</h2>
      <p className="settings-help">
        Rules run only when you ask (per document, or all at once). A rule
        matches against a document&apos;s text, filename, and title; matches
        add the tag and can set the title. Same input, same result — nothing
        learns or drifts behind your back.
      </p>

      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      {rules.length > 0 && (
        <ul className="rule-list">
          {rules.map((r) => (
            <li key={r.id} className={`rule-row ${r.enabled ? '' : 'disabled'}`}>
              <div className="rule-main">
                <strong>{r.name}</strong>
                <span className="rule-detail">
                  {r.match_type === 'regex' ? 'regex' : 'contains'} “{r.pattern}”
                  {r.tag_id && tagName(r.tag_id) ? ` → tag ${tagName(r.tag_id)}` : ''}
                  {r.set_title ? ` → title “${r.set_title}”` : ''}
                  {r.correspondent_id
                    ? ` → from ${correspondents.find((c) => c.id === r.correspondent_id)?.name || '…'}`
                    : ''}
                  {r.doc_type_id
                    ? ` → type ${docTypes.find((t) => t.id === r.doc_type_id)?.name || '…'}`
                    : ''}
                </span>
                {r.error && (
                  <span className="error">
                    Auto-disabled: {r.error}. Edit the pattern to try again.
                  </span>
                )}
              </div>
              {isAdmin && (
                <>
                  <button className="ghost" onClick={() => toggleRule(r)}>
                    {r.enabled ? 'Disable' : 'Enable'}
                  </button>
                  <button className="ghost danger" onClick={() => deleteRule(r)}>
                    Delete
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {!isAdmin && (
        <p className="settings-help">
          Rules apply to the whole library, so only the owner can change them.
        </p>
      )}
      {isAdmin && (
      <form className="rule-form" onSubmit={addRule}>
        <input
          placeholder="Rule name (e.g. Utility bills)"
          value={form.name}
          onChange={set('name')}
          required
        />
        <div className="rule-form-row">
          <select value={form.match_type} onChange={set('match_type')}>
            <option value="contains">contains</option>
            <option value="regex">regex</option>
          </select>
          <input
            placeholder="Pattern (e.g. pacific utility)"
            value={form.pattern}
            onChange={set('pattern')}
            required
          />
        </div>
        <div className="rule-form-row">
          <input
            placeholder="Tag to add (optional)"
            value={form.tag_name}
            onChange={set('tag_name')}
            list="existing-tags"
          />
          <datalist id="existing-tags">
            {tags.map((t) => (
              <option key={t.id} value={t.name} />
            ))}
          </datalist>
          <input
            placeholder="Set title to (optional)"
            value={form.set_title}
            onChange={set('set_title')}
          />
        </div>
        <div className="rule-form-row">
          <input
            placeholder="Set correspondent (optional)"
            value={form.correspondent_name}
            onChange={set('correspondent_name')}
            list="existing-correspondents"
          />
          <datalist id="existing-correspondents">
            {correspondents.map((c) => (
              <option key={c.id} value={c.name} />
            ))}
          </datalist>
          <input
            placeholder="Set type (optional)"
            value={form.doc_type_name}
            onChange={set('doc_type_name')}
            list="existing-doctypes"
          />
          <datalist id="existing-doctypes">
            {docTypes.map((t) => (
              <option key={t.id} value={t.name} />
            ))}
          </datalist>
        </div>
        <div className="rule-form-row">
          <button type="submit">Add rule</button>
          <button type="button" className="ghost" onClick={classifyAll}>
            Classify all documents
          </button>
        </div>
      </form>
      )}
    </section>
  )
}
