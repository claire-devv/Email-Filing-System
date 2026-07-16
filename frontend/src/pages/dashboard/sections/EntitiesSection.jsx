import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { updateEntity, createEntity } from '../../../api/client'
import { useEntities } from '../../../hooks/useRresData'
import { formatDate } from '../../../utils/datetime'

export default function EntitiesSection({ onOpenDocuments }) {
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [expandedId, setExpandedId] = useState(null)
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const queryClient = useQueryClient()

  // Debounce the search input into the query key so we don't fire a request per keystroke.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(query.trim()), 200)
    return () => clearTimeout(id)
  }, [query])

  const { data, isLoading, error } = useEntities(debouncedQuery)
  const entities = Array.isArray(data) ? data : []

  // After editing aliases, refresh whichever entities query is active.
  function applyUpdated() {
    queryClient.invalidateQueries({ queryKey: ['entities'] })
  }

  async function submitCreate() {
    const name = newName.trim()
    if (!name) return
    try {
      setCreating(true)
      setCreateError('')
      await createEntity({ entity_name: name })
      queryClient.invalidateQueries({ queryKey: ['entities'] })
      setNewName('')
      setShowCreate(false)
    } catch (err) {
      setCreateError(err.message || 'Could not create entity')
    } finally {
      setCreating(false)
    }
  }

  if (isLoading && entities.length === 0) return <div className="section-loading">Loading entities…</div>

  return (
    <section className="dashboard-section">
      <header className="section-header">
        <div>
          <p className="section-desc">Master client folders the assistant files into, and their aliases.</p>
        </div>
        <div className="section-actions">
          <input
            className="form-control input-search"
            placeholder="Search entities…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="btn btn-primary btn-sm" onClick={() => { setShowCreate((v) => !v); setCreateError('') }}>
            + New entity
          </button>
        </div>
      </header>

      <div className="section-content">
        {showCreate && (
          <div className="entity-create-card">
            <label className="field field--full">
              <span>New client / entity name</span>
              <input
                className="form-control"
                placeholder="e.g. J. Doe - 123 Street LLC"
                value={newName}
                autoFocus
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && submitCreate()}
              />
              <span className="field-hint">Creates the master folder + standard sub-folders in Google Drive. Use the format “F. Last - Entity Name”.</span>
            </label>
            <div className="entity-create-actions">
              <button className="btn btn-primary btn-sm" onClick={submitCreate} disabled={creating || !newName.trim()}>
                {creating ? 'Creating folders…' : 'Create entity'}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => { setShowCreate(false); setNewName(''); setCreateError('') }} disabled={creating}>
                Cancel
              </button>
            </div>
            {createError && <div className="section-error">{createError}</div>}
          </div>
        )}

        {error && <div className="section-error">{error.message || 'Failed to load entities'}</div>}

        {entities.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-text">No entities found</div>
            <div className="empty-state-hint">Try a different search.</div>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Entity</th>
                  <th>Aliases</th>
                  <th className="col-num">Documents</th>
                  <th>Last used</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {entities.map((entity) => (
                  <EntityRow
                    key={entity.id}
                    entity={entity}
                    expanded={expandedId === entity.id}
                    onToggle={() => setExpandedId((id) => (id === entity.id ? null : entity.id))}
                    onUpdated={applyUpdated}
                    onOpenDocuments={onOpenDocuments}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}

function EntityRow({ entity, expanded, onToggle, onUpdated, onOpenDocuments }) {
  const [newAlias, setNewAlias] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const aliases = Array.isArray(entity.aliases) ? entity.aliases : []

  async function addAlias() {
    const alias = newAlias.trim()
    if (!alias) return
    try {
      setBusy(true)
      setError('')
      const updated = await updateEntity(entity.id, { aliases: [...aliases, alias] })
      onUpdated(updated)
      setNewAlias('')
    } catch (err) {
      setError(err.message || 'Could not add alias')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <tr className="data-table-row" onClick={onToggle}>
        <td className="cell-strong">{entity.entity_name}</td>
        <td className="cell-muted">{aliases.length ? `${aliases.length} alias${aliases.length > 1 ? 'es' : ''}` : '—'}</td>
        <td className="cell-muted col-num">
          {onOpenDocuments ? (
            <button
              type="button"
              className="link-btn"
              title={`View documents filed for ${entity.entity_name}`}
              onClick={(e) => {
                e.stopPropagation()
                onOpenDocuments(entity.entity_name)
              }}
            >
              {entity.documents_filed ?? 0}
            </button>
          ) : (
            entity.documents_filed ?? 0
          )}
        </td>
        <td className="cell-muted">{formatDate(entity.last_used_at)}</td>
        <td>
          <span className={`badge ${entity.active ? 'badge-success' : 'badge-muted'}`}>
            {entity.active ? 'Active' : 'Inactive'}
          </span>
        </td>
      </tr>
      {expanded && (
        <tr className="data-table-detail">
          <td colSpan={5}>
            <div className="detail-grid">
              <div>
                <div className="detail-label">Aliases</div>
                {aliases.length === 0 ? (
                  <div className="cell-muted">No aliases yet.</div>
                ) : (
                  <div className="chip-row">
                    {aliases.map((a) => (
                      <span key={a} className="chip">{a}</span>
                    ))}
                  </div>
                )}
                <div className="inline-form">
                  <input
                    className="form-control"
                    placeholder="Add an alias…"
                    value={newAlias}
                    onChange={(e) => setNewAlias(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && addAlias()}
                  />
                  <button className="btn btn-secondary btn-sm" onClick={addAlias} disabled={busy || !newAlias.trim()}>
                    {busy ? 'Adding…' : 'Add'}
                  </button>
                </div>
                {error && <div className="section-error">{error}</div>}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
