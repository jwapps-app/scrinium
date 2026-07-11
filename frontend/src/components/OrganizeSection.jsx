import { useCallback, useEffect, useState } from 'react'
import { apiJson } from '../api'

function EntityManager({ title, endpoint, hint }) {
  const [items, setItems] = useState([])
  const [name, setName] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    apiJson(endpoint).then(setItems).catch((e) => setError(e.message))
  }, [endpoint])

  useEffect(() => {
    load()
  }, [load])

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
      window.dispatchEvent(new Event('library-changed'))
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div className="organize-block">
      <h3>{title}</h3>
      {hint && <p className="settings-help">{hint}</p>}
      {error && <p className="error">{error}</p>}
      {items.length > 0 && (
        <ul className="rule-list">
          {items.map((item) => (
            <li key={item.id} className="rule-row">
              <div className="rule-main">
                <strong
                  onClick={() =>
                    act(async () => {
                      const next = window.prompt(`Rename “${item.name}” to:`, item.name)
                      if (next?.trim() && next !== item.name)
                        await apiJson(`${endpoint}/${item.id}`, {
                          method: 'PATCH',
                          body: JSON.stringify({ name: next.trim() }),
                        })
                    })
                  }
                  title="Click to rename"
                >
                  {item.name}
                </strong>
                {'count' in item && (
                  <span className="rule-detail">
                    {item.count} document{item.count === 1 ? '' : 's'}
                  </span>
                )}
                {'kind' in item && <span className="rule-detail">{item.kind}</span>}
              </div>
              <button
                className="ghost danger"
                onClick={() =>
                  act(async () => {
                    if (
                      window.confirm(
                        `Delete “${item.name}”? Documents keep everything else.`,
                      )
                    )
                      await apiJson(`${endpoint}/${item.id}`, { method: 'DELETE' })
                  })
                }
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="rule-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (!name.trim()) return
          act(async () => {
            await apiJson(endpoint, {
              method: 'POST',
              body: JSON.stringify({ name: name.trim() }),
            })
            setName('')
          })
        }}
      >
        <div className="rule-form-row">
          <input
            placeholder={`New ${title.toLowerCase().replace(/s$/, '')} name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit">Add</button>
        </div>
      </form>
    </div>
  )
}

function CustomFieldsManager() {
  const [fields, setFields] = useState([])
  const [name, setName] = useState('')
  const [kind, setKind] = useState('text')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    apiJson('/api/custom-fields').then(setFields).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="organize-block">
      <h3>Custom fields</h3>
      <p className="settings-help">
        Structured metadata on documents — amounts, due dates, reference
        numbers. Fields you define here appear on every document&apos;s
        details strip.
      </p>
      {error && <p className="error">{error}</p>}
      {fields.length > 0 && (
        <ul className="rule-list">
          {fields.map((f) => (
            <li key={f.id} className="rule-row">
              <div className="rule-main">
                <strong>{f.name}</strong>
                <span className="rule-detail">{f.kind}</span>
              </div>
              <button
                className="ghost danger"
                onClick={async () => {
                  if (!window.confirm(`Delete field “${f.name}” and its values?`)) return
                  await apiJson(`/api/custom-fields/${f.id}`, { method: 'DELETE' })
                  load()
                }}
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="rule-form"
        onSubmit={async (e) => {
          e.preventDefault()
          if (!name.trim()) return
          setError('')
          try {
            await apiJson('/api/custom-fields', {
              method: 'POST',
              body: JSON.stringify({ name: name.trim(), kind }),
            })
            setName('')
            load()
          } catch (err) {
            setError(err.message)
          }
        }}
      >
        <div className="rule-form-row">
          <input
            placeholder="New field name (e.g. Amount due)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {['text', 'number', 'date', 'money', 'url', 'bool'].map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          <button type="submit">Add field</button>
        </div>
      </form>
    </div>
  )
}

export default function OrganizeSection() {
  const [mail, setMail] = useState(null)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    apiJson('/api/settings/mail').then(setMail).catch(() => {})
  }, [])

  return (
    <section className="settings-section">
      <h2>Organize</h2>

      <EntityManager
        title="Correspondents"
        endpoint="/api/correspondents"
        hint="Who a document is from. Assign per document, via rules, or automatically from email senders."
      />
      <EntityManager
        title="Document types"
        endpoint="/api/doc-types"
        hint="What kind of thing it is — invoice, statement, contract…"
      />
      <CustomFieldsManager />

      <div className="organize-block">
        <h3>Document dates</h3>
        <p className="settings-help">
          Dates are read from each document&apos;s text automatically at
          ingest. This backfills documents that were processed before the
          feature existed.
        </p>
        {notice && <p className="notice">{notice}</p>}
        <button
          className="ghost"
          onClick={async () => {
            setNotice('')
            const result = await apiJson('/api/documents/extract-dates', {
              method: 'POST',
            })
            setNotice(
              `Examined ${result.examined} document(s); found dates for ${result.dated}.`,
            )
            window.dispatchEvent(new Event('library-changed'))
          }}
        >
          Extract dates for existing documents
        </button>
      </div>

      <div className="organize-block">
        <h3>Email ingestion</h3>
        {mail?.configured ? (
          <p className="settings-help">
            Watching <code>{mail.folder}</code> on <code>{mail.host}</code> —
            PDF and image attachments from unseen messages are ingested with
            an “Email” tag and the sender as correspondent. Last poll:{' '}
            {mail.last_result || 'not yet run'}.
          </p>
        ) : (
          <p className="settings-help">
            Not configured. Set <code>MAIL_HOST</code>,{' '}
            <code>MAIL_USERNAME</code>, and <code>MAIL_PASSWORD</code> in the
            server environment (an app password for Gmail/Workspace) and
            restart — the worker then polls the mailbox and consumes
            attachments automatically.
          </p>
        )}
      </div>
    </section>
  )
}
