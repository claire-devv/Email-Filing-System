import { useState, useEffect } from 'react'
import { FaRegEye, FaGoogleDrive, FaLink, FaDownload, FaCheck } from 'react-icons/fa6'
import { downloadDocumentBlob, getDocuments } from '../../../api/client'
import { useDocuments, useEntities } from '../../../hooks/useRresData'
import { openBlobInNewTab } from '../../../utils/openBlob'
import { formatDate } from '../../../utils/datetime'

const PAGE = 50

function formatSize(bytes) {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// "Jane Doe <jane@acme.com>" -> "Jane Doe" (falls back to the raw address).
function senderName(raw) {
  if (!raw) return 'Unknown sender'
  const name = raw.replace(/<[^>]*>/g, '').replace(/"/g, '').trim()
  return name || raw
}

// Where a document came from, for the secondary line. Uploads have no sender, so show the source
// label instead of "Unknown sender". Emails show the sender.
function originLabel(doc) {
  if (doc.sender) return senderName(doc.sender)
  if (doc.source === 'client_uploads') return 'Client upload'
  if (doc.source === 'rres_uploads') return 'RRES upload'
  return senderName(doc.sender)
}

// "RRES - Books / ABC Company / Bank Statements / Chase" -> "Bank Statements / Chase"
// (drops the Drive root and the entity segment — entity has its own column).
function shortFolder(path) {
  if (!path) return '—'
  const parts = path.split(' / ')
  return parts.length > 2 ? parts.slice(2).join(' / ') : parts.length > 1 ? parts.slice(1).join(' / ') : path
}

export default function DocumentsSection({ initialEntity = '' }) {
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [entity, setEntity] = useState(initialEntity)
  const [appended, setAppended] = useState([])
  const [loadingMore, setLoadingMore] = useState(false)
  const [pageError, setPageError] = useState('')

  // Debounce the search box into the query key so we don't refetch per keystroke.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(query.trim()), 250)
    return () => clearTimeout(id)
  }, [query])

  // Reset manual pagination whenever the filter changes.
  useEffect(() => {
    setAppended([])
  }, [debouncedQuery, entity])

  // Cached entity list for the filter dropdown (shared with the Entities tab cache).
  const { data: entitiesData } = useEntities('')
  const entities = Array.isArray(entitiesData) ? entitiesData : []

  // Cached, debounced documents query. keepPreviousData avoids an empty-table flash.
  const { data, isLoading, isFetching, error, refetch } = useDocuments({
    q: debouncedQuery,
    entity,
    limit: PAGE,
    offset: 0,
  })
  const firstPage = Array.isArray(data?.items) ? data.items : []
  const docs = [...firstPage, ...appended]
  const total = data?.total ?? docs.length
  const loadError = pageError || (error ? error.message || 'Failed to load documents' : '')

  async function loadMore() {
    setLoadingMore(true)
    try {
      const res = await getDocuments({ q: debouncedQuery, entity, limit: PAGE, offset: docs.length })
      const page = Array.isArray(res?.items) ? res.items : []
      setAppended((prev) => [...prev, ...page])
    } catch (err) {
      setPageError(err.message || 'Failed to load more documents')
    } finally {
      setLoadingMore(false)
    }
  }

  function handleRefresh() {
    setAppended([])
    setPageError('')
    refetch()
  }

  if (isLoading && docs.length === 0) return <div className="section-loading">Loading documents…</div>

  return (
    <section className="dashboard-section">
      <header className="section-header">
        <div>
          <p className="section-desc">
            Every PDF the assistant has filed. Files are stored in Google Drive — open, share, or
            download them from here.
          </p>
        </div>
        <div className="section-actions">
          <input
            className="form-control input-search"
            placeholder="Search files, emails, senders…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select className="form-control" value={entity} onChange={(e) => setEntity(e.target.value)}>
            <option value="">All entities</option>
            {entities.map((ent) => (
              <option key={ent.id} value={ent.entity_name}>
                {ent.entity_name}
              </option>
            ))}
          </select>
          <button
            className="btn btn-secondary btn-sm"
            onClick={handleRefresh}
            disabled={isFetching}
          >
            Refresh
          </button>
        </div>
      </header>

      <div className="section-content">
        {loadError && <div className="section-error">{loadError}</div>}

        {docs.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-text">No documents found</div>
            <div className="empty-state-hint">
              {query || entity ? 'Try a different search or filter.' : 'Filed PDFs will appear here.'}
            </div>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table documents-table">
              <thead>
                <tr>
                  <th className="col-time">Filed</th>
                  <th>Document</th>
                  <th className="col-entity">Entity</th>
                  <th className="col-folder">Folder</th>
                  <th className="col-num">Size</th>
                  <th className="col-doc-actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((doc) => (
                  <DocumentRow key={doc.id} doc={doc} onError={setPageError} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {docs.length < total && (
          <div className="load-more">
            <button className="btn btn-secondary" onClick={loadMore} disabled={loadingMore}>
              {loadingMore ? 'Loading…' : `Load more (${docs.length} of ${total})`}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

function DocumentRow({ doc, onError }) {
  const [copied, setCopied] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [opening, setOpening] = useState(false)

  async function openPreview() {
    // Large filed documents can take several seconds to fetch from Drive before the new tab
    // opens (nothing to show until the full file is in hand) -- a spinner keeps that from
    // looking like a frozen/broken button.
    try {
      setOpening(true)
      await openBlobInNewTab(() => downloadDocumentBlob(doc.id))
    } catch (err) {
      onError(err.message || 'Could not open the file')
    } finally {
      setOpening(false)
    }
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(doc.drive_link)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      onError('Could not copy the link — your browser blocked clipboard access.')
    }
  }

  async function download() {
    try {
      setDownloading(true)
      const blob = await downloadDocumentBlob(doc.id)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      // doc.filename already carries the correct extension for whatever the file actually is
      // (.pdf, .xlsx, etc.) -- forcing a ".pdf" suffix here renamed spreadsheets to "*.xlsx.pdf".
      link.download = doc.filename || 'document'
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 10000)
    } catch (err) {
      onError(err.message || 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

  const subLine = [originLabel(doc), doc.subject].filter(Boolean).join(' — ')

  return (
    <tr className="data-table-row data-table-row--static">
      <td className="cell-muted col-time">{formatDate(doc.filed_at)}</td>
      <td className="cell-strong">
        <div className="cell-ellipsis" title={doc.filename}>
          {doc.filename}
          {doc.kind === 'combined_package' && <span className="badge badge-muted doc-badge">email package</span>}
          {doc.status === 'duplicate' && <span className="badge badge-muted doc-badge">duplicate</span>}
        </div>
        {subLine && <div className="doc-sub cell-ellipsis" title={subLine}>{subLine}</div>}
      </td>
      <td className="cell-muted col-entity">{doc.entity || '—'}</td>
      <td className="cell-muted col-folder">
        <div className="cell-ellipsis" title={doc.folder_path || ''}>
          {shortFolder(doc.folder_path)}
        </div>
      </td>
      <td className="cell-muted col-num">{formatSize(doc.size_bytes)}</td>
      <td className="col-doc-actions">
        <div className="doc-actions">
          {/* Hidden for now — large-file open-in-new-tab was unreliable; "Open in Drive"
              and "Download" below remain available. Re-enable by removing this guard. */}
          {false && (
            <button
              type="button"
              className="btn btn-ghost btn-sm btn-icon"
              title={opening ? 'Opening…' : 'Open in new tab'}
              onClick={openPreview}
              disabled={opening}
            >
              <FaRegEye className={opening ? 'icon-spin' : undefined} />
            </button>
          )}
          {doc.drive_link && (
            <a className="btn btn-ghost btn-sm btn-icon" href={doc.drive_link} target="_blank" rel="noreferrer" title="Open in Drive">
              <FaGoogleDrive />
            </a>
          )}
          {doc.drive_link && (
            <button type="button" className="btn btn-ghost btn-sm btn-icon" title={copied ? 'Copied!' : 'Copy link'} onClick={copyLink}>
              {copied ? <FaCheck /> : <FaLink />}
            </button>
          )}
          <button type="button" className="btn btn-ghost btn-sm btn-icon" title={downloading ? 'Downloading…' : 'Download'} onClick={download} disabled={downloading}>
            <FaDownload />
          </button>
        </div>
      </td>
    </tr>
  )
}
