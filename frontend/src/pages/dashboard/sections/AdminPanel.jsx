import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  processUnread,
  getGoogleConnectUrl,
  disconnectGoogle,
  updateDriveRoot,
  getDriveRootFolders,
  updateNonEntityFolders,
  startGmailWatch,
  renewGmailWatch,
  stopGmailWatch,
  getUsers,
  createUser,
  updateUser,
  deleteUser,
} from '../../../api/client'
import {
  useFolderRules,
  useApiUsage,
  useGoogleStatus,
  useDriveRootStatus,
  useGmailWatchStatus,
} from '../../../hooks/useRresData'
import { tokenUtils } from '../../../utils/tokenUtils'
import { formatDateTime } from '../../../utils/datetime'

const SUBFOLDER_RULE_LABELS = {
  none: 'No subfolder',
  by_year: 'By year',
  by_bank: 'By bank',
  by_lender: 'By lender',
  by_credit_card: 'By credit card',
}

export default function SettingsSection() {
  const [activeTab, setActiveTab] = useState('inbox')

  // Cached: folder rules are shared with the Review tab's cache; API usage caches 60s.
  const { data: folderRules, isLoading: rulesLoading, error: rulesError } = useFolderRules()
  const { data: usageData, isLoading: usageLoading, error: usageError } = useApiUsage()

  const loading = rulesLoading || usageLoading
  const error = rulesError?.message || usageError?.message || ''
  const apiUsage = Array.isArray(usageData?.usage) ? usageData.usage : []

  if (loading) return <div className="section-loading">Loading settings…</div>

  const level2Folders = folderRules?.rules?.level_2_folders
  const hasDescriptions = Array.isArray(level2Folders) && level2Folders.some((f) => f.description)

  return (
    <section className="dashboard-section">
      <header className="section-header">
        <div>
          <p className="section-desc">Run the inbox, review the filing rulebook, and check API usage.</p>
        </div>
        <div className="section-actions">
          {activeTab === 'rules' && folderRules?.version != null && (
            <span className="badge badge-muted">rulebook v{folderRules.version}</span>
          )}
        </div>
      </header>

      <nav className="admin-nav">
        <button className={`admin-nav-btn ${activeTab === 'inbox' ? 'active' : ''}`} onClick={() => setActiveTab('inbox')}>
          Inbox
        </button>
        <button className={`admin-nav-btn ${activeTab === 'rules' ? 'active' : ''}`} onClick={() => setActiveTab('rules')}>
          Folder rules
        </button>
        <button className={`admin-nav-btn ${activeTab === 'usage' ? 'active' : ''}`} onClick={() => setActiveTab('usage')}>
          API usage
        </button>
        <button className={`admin-nav-btn ${activeTab === 'integrations' ? 'active' : ''}`} onClick={() => setActiveTab('integrations')}>
          Integrations
        </button>
        {tokenUtils.getIsAdmin() && (
          <button className={`admin-nav-btn ${activeTab === 'users' ? 'active' : ''}`} onClick={() => setActiveTab('users')}>
            Users
          </button>
        )}
      </nav>

      <div className="section-content">
        {error && <div className="section-error">{error}</div>}

        {activeTab === 'inbox' && <InboxRunner />}

        {activeTab === 'rules' &&
          (Array.isArray(level2Folders) && level2Folders.length > 0 ? (
            <div className="table" style={{ '--table-cols': hasDescriptions ? '1.5fr 1fr 2fr' : '1.5fr 1fr' }}>
              <div className="table-header">
                <div className="table-cell">Subfolder</div>
                <div className="table-cell">Level 3</div>
                {hasDescriptions && <div className="table-cell">Description</div>}
              </div>
              {level2Folders.map((folder) => (
                <div key={folder.name} className="table-row">
                  <div className="table-cell">{folder.name}</div>
                  <div className="table-cell">
                    {SUBFOLDER_RULE_LABELS[folder.subfolder_rule] || folder.subfolder_rule || '—'}
                  </div>
                  {hasDescriptions && <div className="table-cell">{folder.description || '—'}</div>}
                </div>
              ))}
            </div>
          ) : folderRules?.rules ? (
            <div className="response-box">
              <pre>{JSON.stringify(folderRules.rules, null, 2)}</pre>
            </div>
          ) : (
            <div className="empty-state">
              <div className="empty-state-text">No folder rules configured</div>
            </div>
          ))}

        {activeTab === 'usage' &&
          (apiUsage.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-text">No API usage recorded</div>
              <div className="empty-state-hint">Calls are tallied per provider, per day.</div>
            </div>
          ) : (
            <div className="table" style={{ '--table-cols': '2fr 1fr 1fr' }}>
              <div className="table-header">
                <div className="table-cell">Provider</div>
                <div className="table-cell">Date</div>
                <div className="table-cell">Calls</div>
              </div>
              {apiUsage.map((usage, index) => (
                <div key={index} className="table-row">
                  <div className="table-cell">{usage.provider}</div>
                  <div className="table-cell">{usage.date}</div>
                  <div className="table-cell">{usage.call_count}</div>
                </div>
              ))}
            </div>
          ))}

        {activeTab === 'integrations' && <IntegrationsPanel />}

        {activeTab === 'users' && tokenUtils.getIsAdmin() && <UsersPanel />}
      </div>
    </section>
  )
}

function IntegrationsPanel() {
  const queryClient = useQueryClient()
  const { data: google, isLoading: gLoading, error: gError } = useGoogleStatus()
  const { data: driveRoot, isLoading: dLoading, error: dError } = useDriveRootStatus()
  const { data: watch, isLoading: wLoading, error: wError } = useGmailWatchStatus()

  const loading = gLoading || dLoading || wLoading
  const error = gError?.message || dError?.message || wError?.message || ''

  // After a connect/disconnect/watch action, refetch all three statuses.
  function load() {
    return queryClient.invalidateQueries({ queryKey: ['google'] })
  }

  if (loading) return <div className="section-loading">Loading integrations…</div>

  return (
    <div className="settings-block">
      {error && <div className="section-error">{error}</div>}
      <GoogleAccountCard google={google} onChanged={load} />
      <DriveRootCard driveRoot={driveRoot} connected={google?.connected} onChanged={load} />
      <FolderSkipCard driveRoot={driveRoot} connected={google?.connected} />
      <GmailWatchCard watch={watch} connected={google?.connected} onChanged={load} />
    </div>
  )
}

// Lets the reviewer choose which top-level Drive folders are client entities and which are
// operational/noise folders to skip during entity import (e.g. "Unmatched", "RRES UPLOADS").
function FolderSkipCard({ driveRoot, connected }) {
  const [folders, setFolders] = useState([])
  const [skipped, setSkipped] = useState({}) // name -> bool
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [savedNote, setSavedNote] = useState('')

  const configured = !!driveRoot?.configured

  async function load() {
    if (!connected || !configured) return
    try {
      setLoading(true)
      setError('')
      setSavedNote('')
      const data = await getDriveRootFolders()
      const list = Array.isArray(data?.folders) ? data.folders : []
      setFolders(list)
      setSkipped(Object.fromEntries(list.map((f) => [f.name, !!f.skipped])))
    } catch (err) {
      setError(err.message || 'Could not load Drive folders')
    } finally {
      setLoading(false)
    }
  }

  // Reload whenever the Drive root changes (e.g. after switching drives).
  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, configured, driveRoot?.drive_root_id])

  async function save() {
    try {
      setBusy(true)
      setError('')
      const skipList = folders.map((f) => f.name).filter((name) => skipped[name])
      const result = await updateNonEntityFolders(skipList)
      const imp = result?.entity_import
      if (imp?.error) {
        setError(`Entity sync failed: ${imp.error}`)
      } else if (imp) {
        const dupNote = imp.duplicates ? `, ${imp.duplicates} duplicate-name folder(s) skipped` : ''
        setSavedNote(`Saved. Entities synced: ${imp.created || 0} added, ${imp.updated || 0} updated, ${imp.deactivated || 0} deactivated${dupNote}.`)
      } else {
        setSavedNote('Saved.')
      }
    } catch (err) {
      setError(err.message || 'Failed to save skipped folders')
    } finally {
      setBusy(false)
    }
  }

  if (!connected || !configured) return null

  const skipCount = folders.filter((f) => skipped[f.name]).length

  return (
    <div className="settings-block" style={{ padding: 'var(--space-4)', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)' }}>
      <div className="settings-row" style={{ padding: 0, background: 'none', border: 'none' }}>
        <div>
          <div className="settings-row-title">Folders to skip (not client entities)</div>
          <div className="settings-row-desc">
            Top-level folders under the Drive root. Untick a folder to treat it as a client entity;
            tick it to skip it during sync (e.g. operational folders like Unmatched or RRES UPLOADS).
          </div>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={load} disabled={loading || busy}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {error && <div className="section-error">{error}</div>}

      {folders.length === 0 ? (
        <div className="settings-row-desc">{loading ? 'Loading folders…' : 'No top-level folders found under the Drive root.'}</div>
      ) : (
        <ul className="folder-skip-list">
          {folders.map((f) => (
            <li key={f.id} className="folder-skip-item">
              <label>
                <input
                  type="checkbox"
                  checked={!!skipped[f.name]}
                  onChange={(e) => setSkipped((prev) => ({ ...prev, [f.name]: e.target.checked }))}
                />
                <span>{f.name}</span>
              </label>
              {skipped[f.name] ? (
                <span className="badge badge-muted">Skipped</span>
              ) : (
                <span className="badge badge-success">Entity</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {savedNote && <div className="settings-row-desc">{savedNote}</div>}

      <div className="section-actions">
        <button className="btn btn-primary" onClick={save} disabled={busy || loading || folders.length === 0}>
          {busy ? 'Saving…' : `Save skipped folders${skipCount ? ` (${skipCount})` : ''}`}
        </button>
      </div>
    </div>
  )
}

function GoogleAccountCard({ google, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function connect() {
    try {
      setBusy(true)
      setError('')
      const { auth_url } = await getGoogleConnectUrl()
      if (auth_url) {
        window.location.href = auth_url
      } else {
        throw new Error('No auth URL returned')
      }
    } catch (err) {
      setError(err.message || 'Failed to start Google connection')
      setBusy(false)
    }
  }

  async function disconnect() {
    if (!window.confirm('Disconnect the Google account? Email filing and Drive access will stop until reconnected.')) return
    try {
      setBusy(true)
      setError('')
      await disconnectGoogle()
      await onChanged()
    } catch (err) {
      setError(err.message || 'Failed to disconnect Google account')
    } finally {
      setBusy(false)
    }
  }

  const connected = !!google?.connected
  const email = google?.email

  return (
    <div className="settings-row">
      <div>
        <div className="settings-row-title">
          Google account{' '}
          {connected ? (
            <span className="badge badge-success">Connected</span>
          ) : (
            <span className="badge badge-danger">Not connected</span>
          )}
        </div>
        <div className="settings-row-desc">
          {connected
            ? `Filing as ${email || 'unknown account'}. Used for Gmail (read/file) and Drive (filing destination).`
            : 'Connect the client Gmail account (e.g. file@...) that receives emails to file and owns the Drive destination.'}
          {google?.requires_reconnect && connected === false && google?.token_file_exists && (
            <> The saved token is no longer valid — reconnect to restore access.</>
          )}
        </div>
        {error && <div className="section-error">{error}</div>}
      </div>
      <div className="section-actions">
        <button className="btn btn-primary" onClick={connect} disabled={busy}>
          {connected ? 'Reconnect Google' : 'Connect Google'}
        </button>
        {connected && (
          <button className="btn btn-danger" onClick={disconnect} disabled={busy}>
            Disconnect
          </button>
        )}
      </div>
    </div>
  )
}

function DriveRootCard({ driveRoot, connected, onChanged }) {
  const [folderInput, setFolderInput] = useState('')
  const [nameInput, setNameInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [importResult, setImportResult] = useState(null)

  useEffect(() => {
    setFolderInput(driveRoot?.drive_root_id || '')
    setNameInput(driveRoot?.drive_root_name || '')
  }, [driveRoot])

  async function save() {
    if (!folderInput.trim()) {
      setError('Enter a Drive folder URL or ID.')
      return
    }
    try {
      setBusy(true)
      setError('')
      setImportResult(null)
      const result = await updateDriveRoot({
        folder_url_or_id: folderInput.trim(),
        drive_root_name: nameInput.trim() || null,
        validate_access: true,
      })
      if (result?.entity_import) setImportResult(result.entity_import)
      await onChanged()
    } catch (err) {
      setError(err.message || 'Failed to save Drive root folder')
    } finally {
      setBusy(false)
    }
  }

  const configured = !!driveRoot?.configured
  const accessible = !!driveRoot?.accessible

  return (
    <div className="settings-block" style={{ padding: 'var(--space-4)', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)' }}>
      <div className="settings-row" style={{ padding: 0, background: 'none', border: 'none' }}>
        <div>
          <div className="settings-row-title">
            Drive filing root{' '}
            {configured ? (
              accessible ? (
                <span className="badge badge-success">Accessible</span>
              ) : (
                <span className="badge badge-warn">Not accessible</span>
              )
            ) : (
              <span className="badge badge-muted">Not configured</span>
            )}
          </div>
          <div className="settings-row-desc">
            The Google Drive folder where filed documents are organized by entity. Requires a connected Google account.
            {driveRoot?.access_error && <> {driveRoot.access_error}</>}
          </div>
        </div>
      </div>

      <div className="review-form">
        <div>
          <label className="settings-row-desc" htmlFor="drive-folder-input">Folder URL or ID</label>
          <input
            id="drive-folder-input"
            className="form-control"
            type="text"
            placeholder="https://drive.google.com/drive/folders/..."
            value={folderInput}
            onChange={(e) => setFolderInput(e.target.value)}
          />
        </div>
        <div>
          <label className="settings-row-desc" htmlFor="drive-name-input">Display name (optional)</label>
          <input
            id="drive-name-input"
            className="form-control"
            type="text"
            placeholder="RRES - Books"
            value={nameInput}
            onChange={(e) => setNameInput(e.target.value)}
          />
        </div>
      </div>

      {error && <div className="section-error">{error}</div>}
      {importResult && (
        importResult.error ? (
          <div className="section-error">Entity import failed: {importResult.error}</div>
        ) : (
          <div className="settings-row-desc">
            Entities synced: {importResult.created || 0} added, {importResult.updated || 0} updated
            {importResult.duplicates ? `, ${importResult.duplicates} duplicate-name folder(s) skipped` : ''}.
          </div>
        )
      )}

      <div className="section-actions">
        <button className="btn btn-primary" onClick={save} disabled={busy || !connected}>
          {busy ? 'Saving…' : 'Save & validate'}
        </button>
        {!connected && <span className="settings-row-desc">Connect Google first.</span>}
        {driveRoot?.folder?.name && (
          <span className="settings-row-desc">Currently: {driveRoot.folder.name}</span>
        )}
      </div>
    </div>
  )
}

const LABEL_FILTER_OPTIONS = ['INCLUDE', 'EXCLUDE']

function GmailWatchCard({ watch, connected, onChanged }) {
  const [topic, setTopic] = useState('')
  const [labelIds, setLabelIds] = useState('INBOX')
  const [filterBehavior, setFilterBehavior] = useState('INCLUDE')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (watch?.topic_name) setTopic(watch.topic_name)
    if (watch?.label_ids?.length) setLabelIds(watch.label_ids.join(', '))
    if (watch?.label_filter_behavior) setFilterBehavior(watch.label_filter_behavior)
  }, [watch])

  function buildPayload() {
    const payload = {}
    if (topic.trim()) payload.topic_name = topic.trim()
    const ids = labelIds.split(',').map((s) => s.trim()).filter(Boolean)
    if (ids.length) payload.label_ids = ids
    if (filterBehavior) payload.label_filter_behavior = filterBehavior
    return payload
  }

  async function start() {
    try {
      setBusy(true)
      setError('')
      await startGmailWatch(buildPayload())
      await onChanged()
    } catch (err) {
      setError(err.message || 'Failed to start Gmail watch')
    } finally {
      setBusy(false)
    }
  }

  async function renew() {
    try {
      setBusy(true)
      setError('')
      await renewGmailWatch(buildPayload())
      await onChanged()
    } catch (err) {
      setError(err.message || 'Failed to renew Gmail watch')
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (!window.confirm('Stop the Gmail Pub/Sub watch? Incoming emails will only be picked up by the manual "Process unread" run.')) return
    try {
      setBusy(true)
      setError('')
      await stopGmailWatch()
      await onChanged()
    } catch (err) {
      setError(err.message || 'Failed to stop Gmail watch')
    } finally {
      setBusy(false)
    }
  }

  const active = !!watch?.active

  return (
    <div className="settings-block" style={{ padding: 'var(--space-4)', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)' }}>
      <div className="settings-row" style={{ padding: 0, background: 'none', border: 'none' }}>
        <div>
          <div className="settings-row-title">
            Gmail Pub/Sub watch{' '}
            {active ? <span className="badge badge-success">Active</span> : <span className="badge badge-muted">Inactive</span>}
          </div>
          <div className="settings-row-desc">
            Push notifications that trigger automatic email processing. Requires a Pub/Sub topic in the same Google Cloud
            project as the OAuth client, with the webhook pointed at <code>/webhooks/gmail/pubsub</code>.
          </div>
        </div>
      </div>

      {watch?.email_address && (
        <div className="summary-chips">
          <span>Watching: {watch.email_address}</span>
          {watch.expiration_at && <span>Expires: {formatDateTime(watch.expiration_at)}</span>}
          {watch.last_notification_at && <span>Last notification: {formatDateTime(watch.last_notification_at)}</span>}
          {watch.last_successful_sync_at && <span>Last sync: {formatDateTime(watch.last_successful_sync_at)}</span>}
        </div>
      )}
      {watch?.last_error && <div className="section-error">{watch.last_error}</div>}

      <div className="review-form">
        <div>
          <label className="settings-row-desc" htmlFor="pubsub-topic-input">Pub/Sub topic name</label>
          <input
            id="pubsub-topic-input"
            className="form-control"
            type="text"
            placeholder="projects/YOUR_PROJECT_ID/topics/rres-gmail-events"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
          />
        </div>
        <div>
          <label className="settings-row-desc" htmlFor="pubsub-labels-input">Label IDs (comma-separated)</label>
          <input
            id="pubsub-labels-input"
            className="form-control"
            type="text"
            placeholder="INBOX"
            value={labelIds}
            onChange={(e) => setLabelIds(e.target.value)}
          />
        </div>
        <div>
          <label className="settings-row-desc" htmlFor="pubsub-filter-select">Label filter behavior</label>
          <select
            id="pubsub-filter-select"
            className="form-control"
            value={filterBehavior}
            onChange={(e) => setFilterBehavior(e.target.value)}
          >
            {LABEL_FILTER_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>{opt}</option>
            ))}
          </select>
        </div>
      </div>

      {error && <div className="section-error">{error}</div>}

      <div className="section-actions">
        <button className="btn btn-primary" onClick={start} disabled={busy || !connected}>
          {active ? 'Restart watch' : 'Start watch'}
        </button>
        <button className="btn btn-secondary" onClick={renew} disabled={busy || !connected || !active}>
          Renew
        </button>
        {active && (
          <button className="btn btn-danger" onClick={stop} disabled={busy}>
            Stop
          </button>
        )}
        {!connected && <span className="settings-row-desc">Connect Google first.</span>}
      </div>
    </div>
  )
}

function InboxRunner() {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)

  async function run() {
    try {
      setLoading(true)
      setError('')
      setResult(null)
      setResult(await processUnread({}))
    } catch (err) {
      setError(err.message || 'Failed to process unread emails')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="settings-block">
      <div className="settings-row">
        <div>
          <div className="settings-row-title">Process unread emails</div>
          <div className="settings-row-desc">Scan the inbox now and file any unread emails the assistant is confident about.</div>
        </div>
        <button className="btn btn-primary" onClick={run} disabled={loading}>
          {loading ? 'Processing…' : 'Run now'}
        </button>
      </div>

      {error && <div className="section-error">{error}</div>}

      {result && (
        <div className="stats-grid stats-grid--inline">
          <div className="stat-card">
            <span className="stat-label">Processed</span>
            <span className="stat-value">{result.processed_count ?? 0}</span>
          </div>
          <div className="stat-card">
            <span className="stat-label">Skipped</span>
            <span className="stat-value">{result.skipped_count ?? 0}</span>
          </div>
          <div className="stat-card">
            <span className="stat-label">Waiting (API limit)</span>
            <span className="stat-value">{result.waiting_count ?? 0}</span>
          </div>
        </div>
      )}
    </div>
  )
}

function UsersPanel() {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [resetId, setResetId] = useState(null)
  const [resetPassword, setResetPassword] = useState('')

  async function load() {
    try {
      setError('')
      const data = await getUsers()
      setUsers(Array.isArray(data) ? data : [])
    } catch (err) {
      setError(err.message || 'Failed to load users')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    ;(async () => {
      await load()
    })()
  }, [])

  async function handleCreate(e) {
    e.preventDefault()
    if (!email.trim()) {
      setError('Enter an email address.')
      return
    }
    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    try {
      setBusy(true)
      setError('')
      await createUser(email.trim(), password)
      setEmail('')
      setPassword('')
      setConfirmPassword('')
      await load()
    } catch (err) {
      setError(err.message || 'Failed to create user')
    } finally {
      setBusy(false)
    }
  }

  async function toggleActive(user) {
    if (user.active) {
      if (!window.confirm(`Deactivate ${user.email}? They will no longer be able to sign in.`)) return
    }
    try {
      setBusy(true)
      setError('')
      await updateUser(user.id, { active: !user.active })
      await load()
    } catch (err) {
      setError(err.message || 'Failed to update user')
    } finally {
      setBusy(false)
    }
  }

  async function submitReset(user) {
    if (resetPassword.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    try {
      setBusy(true)
      setError('')
      await updateUser(user.id, { password: resetPassword })
      setResetId(null)
      setResetPassword('')
      await load()
    } catch (err) {
      setError(err.message || 'Failed to reset password')
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete(user) {
    if (!window.confirm(`Delete ${user.email}? This cannot be undone.`)) return
    try {
      setBusy(true)
      setError('')
      await deleteUser(user.id)
      await load()
    } catch (err) {
      setError(err.message || 'Failed to delete user')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div className="section-loading">Loading users…</div>

  return (
    <div className="settings-block">
      {error && <div className="section-error">{error}</div>}

      <div className="table users-table" style={{ '--table-cols': '2fr 1fr 1fr 2.5fr' }}>
        <div className="table-header">
          <div className="table-cell">Email</div>
          <div className="table-cell">Role</div>
          <div className="table-cell">Status</div>
          <div className="table-cell">Actions</div>
        </div>
        {users.map((user) => (
          <div key={user.id} className="table-row">
            <div className="table-cell" data-label="Email">{user.email}</div>
            <div className="table-cell" data-label="Role">
              {user.is_admin ? <span className="badge badge-accent">Admin</span> : <span className="badge badge-muted">User</span>}
            </div>
            <div className="table-cell" data-label="Status">
              {user.active ? <span className="badge badge-success">Active</span> : <span className="badge badge-muted">Inactive</span>}
            </div>
            <div className="table-cell" data-label="Actions">
              {!user.is_admin && (
                <div className="user-actions">
                  {resetId === user.id ? (
                    <div className="user-reset-group">
                      <input
                        className="form-control user-reset-input"
                        type="password"
                        placeholder="New password"
                        value={resetPassword}
                        onChange={(e) => setResetPassword(e.target.value)}
                      />
                      <button className="btn btn-primary btn-sm" onClick={() => submitReset(user)} disabled={busy}>
                        Save
                      </button>
                      <button
                        className="btn btn-secondary btn-sm"
                        onClick={() => {
                          setResetId(null)
                          setResetPassword('')
                        }}
                        disabled={busy}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <>
                      <button className="btn btn-secondary btn-sm" onClick={() => toggleActive(user)} disabled={busy}>
                        {user.active ? 'Deactivate' : 'Reactivate'}
                      </button>
                      <button className="btn btn-secondary btn-sm" onClick={() => setResetId(user.id)} disabled={busy}>
                        Reset password
                      </button>
                      <button className="btn btn-danger btn-sm" onClick={() => handleDelete(user)} disabled={busy}>
                        Delete
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="settings-block" style={{ padding: 'var(--space-4)', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)' }}>
        <div className="settings-row" style={{ padding: 0, background: 'none', border: 'none' }}>
          <div>
            <div className="settings-row-title">Add user</div>
            <div className="settings-row-desc">New users get the same full dashboard access as the admin account, but cannot manage other users.</div>
          </div>
        </div>

        <form className="review-form" onSubmit={handleCreate}>
          <div>
            <label className="settings-row-desc" htmlFor="new-user-email">Email</label>
            <input
              id="new-user-email"
              className="form-control"
              type="email"
              placeholder="person@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div>
            <label className="settings-row-desc" htmlFor="new-user-password">Password</label>
            <input
              id="new-user-password"
              className="form-control"
              type="password"
              placeholder="At least 8 characters"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div>
            <label className="settings-row-desc" htmlFor="new-user-confirm">Confirm password</label>
            <input
              id="new-user-confirm"
              className="form-control"
              type="password"
              placeholder="Repeat password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
            />
          </div>
          <div className="section-actions">
            <button className="btn btn-primary" type="submit" disabled={busy}>
              Add user
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
