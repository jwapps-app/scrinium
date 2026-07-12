import { useCallback, useEffect, useState } from 'react'
import { apiJson } from '../api'
import { flattenTagTree } from './Shell'

export default function TagsSection({ standalone = false }) {
  const [tags, setTags] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [newName, setNewName] = useState('')
  const [newParent, setNewParent] = useState('')
  const [renaming, setRenaming] = useState(null) // tag id
  const [renameText, setRenameText] = useState('')

  const load = useCallback(() => {
    apiJson('/api/tags').then(setTags).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function patch(id, body) {
    setError('')
    try {
      await apiJson(`/api/tags/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
      load()
      window.dispatchEvent(new Event('library-changed'))
    } catch (e) {
      setError(e.message)
    }
  }

  async function create(e) {
    e.preventDefault()
    if (!newName.trim()) return
    setError('')
    try {
      await apiJson('/api/tags', {
        method: 'POST',
        body: JSON.stringify({
          name: newName.trim(),
          parent_id: newParent || null,
        }),
      })
      setNewName('')
      setNewParent('')
      load()
      window.dispatchEvent(new Event('library-changed'))
    } catch (e) {
      setError(e.message)
    }
  }

  async function remove(tag) {
    if (
      !window.confirm(
        `Delete tag “${tag.name}”? Documents keep their other tags; child tags move to the top level.`,
      )
    )
      return
    setError('')
    try {
      await apiJson(`/api/tags/${tag.id}`, { method: 'DELETE' })
      load()
      window.dispatchEvent(new Event('library-changed'))
    } catch (e) {
      setError(e.message)
    }
  }

  const tree = flattenTagTree(tags)

  return (
    <section className={standalone ? 'tags-standalone' : 'settings-section'}>
      {!standalone && <h2>Tags</h2>}
      {notice && <p className="notice">{notice}</p>}
      <p className="settings-help">
        Tags can nest: give a tag a parent and it appears indented beneath it,
        like folders. Tagging a document with a child automatically applies
        the whole chain — tag something “Horse Structures” and it also gets
        “Animal Houses” and “Construction”. Dropping folders into the watch
        directory builds this tree by itself.
      </p>

      {error && <p className="error">{error}</p>}

      {tree.length > 0 && (
        <ul className="rule-list">
          {tree.map((t) => (
            <li
              key={t.id}
              className="rule-row"
              style={t.depth ? { marginLeft: `${t.depth * 1.1}rem` } : undefined}
            >
              <div className="rule-main">
                {renaming === t.id ? (
                  <input
                    value={renameText}
                    autoFocus
                    onChange={(e) => setRenameText(e.target.value)}
                    onBlur={() => {
                      setRenaming(null)
                      if (renameText.trim() && renameText !== t.name)
                        patch(t.id, { name: renameText.trim() })
                    }}
                    onKeyDown={(e) => e.key === 'Enter' && e.target.blur()}
                  />
                ) : (
                  <strong
                    onClick={() => {
                      setRenaming(t.id)
                      setRenameText(t.name)
                    }}
                    title="Click to rename"
                  >
                    {t.depth > 0 && <span className="tree-tick">└ </span>}
                    {t.name}
                  </strong>
                )}
                <span className="rule-detail">
                  {t.count} document{t.count === 1 ? '' : 's'}
                </span>
              </div>
              <span className="tag-color-cell">
                <input
                  type="color"
                  value={t.color || '#8a8a8a'}
                  onChange={(e) => patch(t.id, { color: e.target.value })}
                  title="Tag color"
                />
                {t.color && (
                  <button
                    className="ghost side-x"
                    onClick={() => patch(t.id, { clear_color: true })}
                    title="Clear color"
                  >
                    ×
                  </button>
                )}
              </span>
              <select
                value={t.parent_id || ''}
                onChange={(e) =>
                  patch(
                    t.id,
                    e.target.value
                      ? { parent_id: e.target.value }
                      : { clear_parent: true },
                  )
                }
                title="Parent tag"
              >
                <option value="">(top level)</option>
                {tags
                  .filter((p) => p.id !== t.id)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      under {p.name}
                    </option>
                  ))}
              </select>
              <button className="ghost danger" onClick={() => remove(t)}>
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      {tags.some((t) => t.count === 0) && (
        <p>
          <button
            className="ghost"
            onClick={async () => {
              if (!window.confirm('Assign colors to every tag? Root tags get distinct colors; subtags become shades of their parent. You can still adjust any tag afterwards.')) return
              try {
                const result = await apiJson('/api/tags/auto-color', { method: 'POST' })
                setNotice(`Colored ${result.colored} tags.`)
                load()
                window.dispatchEvent(new Event('library-changed'))
              } catch (err) {
                setError(err.message)
              }
            }}
          >
            Auto-color
          </button>
          <button
            className="ghost"
            onClick={async () => {
              setError('')
              try {
                const result = await apiJson('/api/tags/unused', {
                  method: 'DELETE',
                })
                load()
                window.dispatchEvent(new Event('library-changed'))
                if (result.removed === 0)
                  setError('No unused tags to remove (tags with children stay until the children go).')
              } catch (e) {
                setError(e.message)
              }
            }}
          >
            Delete unused tags (0 documents)
          </button>
        </p>
      )}

      <form className="rule-form" onSubmit={create}>
        <div className="rule-form-row">
          <input
            placeholder="New tag name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <select value={newParent} onChange={(e) => setNewParent(e.target.value)}>
            <option value="">(top level)</option>
            {tags.map((p) => (
              <option key={p.id} value={p.id}>
                under {p.name}
              </option>
            ))}
          </select>
          <button type="submit">Add tag</button>
        </div>
      </form>
    </section>
  )
}
