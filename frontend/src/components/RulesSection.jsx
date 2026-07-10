import { useCallback, useEffect, useState } from 'react'
import { apiJson } from '../api'

const EMPTY = { name: '', match_type: 'contains', pattern: '', tag_name: '', set_title: '' }

export default function RulesSection() {
  const [rules, setRules] = useState([])
  const [tags, setTags] = useState([])
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      const [ruleData, tagData] = await Promise.all([
        apiJson('/api/rules'),
        apiJson('/api/tags'),
      ])
      setRules(ruleData)
      setTags(tagData)
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
      await apiJson('/api/rules', {
        method: 'POST',
        body: JSON.stringify({
          name: form.name,
          match_type: form.match_type,
          pattern: form.pattern,
          tag_id: tagId,
          set_title: form.set_title.trim() || null,
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
    setNotice('')
    try {
      const result = await apiJson('/api/classify/run', { method: 'POST' })
      setNotice(
        `Classified ${result.documents_examined} documents; ${result.documents_changed} changed.`,
      )
    } catch (err) {
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
                </span>
              </div>
              <button className="ghost" onClick={() => toggleRule(r)}>
                {r.enabled ? 'Disable' : 'Enable'}
              </button>
              <button className="ghost danger" onClick={() => deleteRule(r)}>
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

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
          <button type="submit">Add rule</button>
          <button type="button" className="ghost" onClick={classifyAll}>
            Classify all documents
          </button>
        </div>
      </form>
    </section>
  )
}
