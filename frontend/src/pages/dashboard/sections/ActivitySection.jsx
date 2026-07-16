import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { queryClient } from '../../../lib/queryClient'
import { getActivity } from '../../../api/client'
import { formatDate, formatTimeOfDay, formatDateTime } from '../../../utils/datetime'

const PAGE = 50

const STATUS_BADGES = {
  filed: 'badge-success',
  approved: 'badge-success',
  corrected: 'badge-success',
  pending_review: 'badge-accent',
  rejected: 'badge-muted',
  skipped: 'badge-muted',
  failed: 'badge-danger',
  waiting_api_limit: 'badge-warn',
}

function statusLabel(status) {
  return (status || '').replace(/_/g, ' ')
}

// Short tag for where a filed item came from. "email" is the default and shows nothing.
function sourceLabel(source) {
  if (source === 'client_uploads') return 'Client upload'
  if (source === 'rres_uploads') return 'RRES upload'
  return null
}

function formatDuration(ms) {
  if (ms == null) return null
  return `${(ms / 1000).toFixed(1)}s`
}

function formatSize(bytes) {
  if (bytes == null) return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function artifactLabel(a) {
  if (a.drive_filename) return a.drive_filename
  if (a.kind === 'email_body') return 'Email preview'
  if (a.kind === 'combined_package') return 'Combined PDF'
  return a.original_filename || 'Attachment'
}

function filedTo(item) {
  if (!item.folder_path) return '—'
  const parts = item.folder_path.split(' / ')
  return parts.length > 1 ? parts.slice(1).join(' / ') : item.folder_path
}

// Shared by Home (recent) and the full Activity page.
export function ActivityTable({ rows, expandedId, onToggle }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th className="col-time">Time</th>
            <th>Subject</th>
            <th className="col-entity">Entity</th>
            <th className="col-folder">Filed to</th>
            <th className="col-status">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const open = expandedId === item.id
            const driveLink =
              item.artifacts?.find((a) => a.kind === 'combined_package' && a.drive_link)?.drive_link ||
              item.drive_link ||
              item.artifacts?.find((a) => a.drive_link)?.drive_link
            const folderDriveId =
              item.folder_drive_id ||
              (item.artifacts?.find((a) => a.drive_folder_id && a.kind !== 'combined_package') ||
                item.artifacts?.find((a) => a.drive_folder_id))?.drive_folder_id
            const folderLink = folderDriveId
              ? `https://drive.google.com/drive/folders/${folderDriveId}`
              : null
            return (
              <ActivityRow
                key={item.id}
                item={item}
                open={open}
                driveLink={driveLink}
                folderLink={folderLink}
                onToggle={() => onToggle(item.id)}
              />
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ActivityRow({ item, open, driveLink, folderLink, onToggle }) {
  return (
    <>
      <tr className="data-table-row" onClick={onToggle}>
        <td className="cell-muted col-time">
          <div>{formatDate(item.created_at)}</div>
          <div className="col-time-sub">{formatTimeOfDay(item.created_at)}</div>
          {sourceLabel(item.source) && (
            <div className="source-tag">{sourceLabel(item.source)}</div>
          )}
        </td>
        <td className="cell-strong">{item.subject || '(no subject)'}</td>
        <td className="cell-muted col-entity">{item.entity || '—'}</td>
        <td className="cell-muted col-folder" title={item.folder_path || ''}>{filedTo(item)}</td>
        <td className="col-status">
          <span className={`badge ${STATUS_BADGES[item.status] || 'badge-muted'}`}>
            {statusLabel(item.status)}
          </span>
        </td>
      </tr>
      {open && (
        <tr className="data-table-detail">
          <td colSpan={5}>
            <div className="detail-grid">
              <div>
                <div className="detail-label">{sourceLabel(item.source) ? 'Source' : 'From'}</div>
                <div>{item.sender || sourceLabel(item.source) || '—'}</div>
              </div>
              <div>
                <div className="detail-label">Folder</div>
                <div>{item.folder_path || '—'}</div>
              </div>
              {typeof item.confidence === 'number' && (
                <div>
                  <div className="detail-label">Confidence</div>
                  <div>{item.confidence}%</div>
                </div>
              )}
              {item.received_at && (
                <div>
                  <div className="detail-label">Received</div>
                  <div>{formatDateTime(item.received_at)}</div>
                </div>
              )}
              {formatDuration(item.processing_time_ms) && (
                <div>
                  <div className="detail-label">Processing time</div>
                  <div>{formatDuration(item.processing_time_ms)}</div>
                </div>
              )}
              {item.message && (
                <div className="detail-full">
                  <div className="detail-label">Message</div>
                  <div className="cell-muted">{item.message}</div>
                </div>
              )}
              {item.artifacts?.some((a) => a.drive_file_id) && (
                <div className="detail-full">
                  <div className="detail-label">Files</div>
                  <div className="file-list">
                    {item.artifacts
                      .filter((a) => a.drive_file_id)
                      .map((a) => (
                        <div key={a.id} className="file-line">
                          <span className="file-line-name" title={artifactLabel(a)}>
                            📄 {artifactLabel(a)}
                          </span>
                          {formatSize(a.size_bytes) && (
                            <span className="cell-muted">{formatSize(a.size_bytes)}</span>
                          )}
                          <span className={`badge ${a.status === 'duplicate' ? 'badge-muted' : 'badge-success'}`}>
                            {a.status === 'duplicate' ? 'duplicate' : 'filed'}
                          </span>
                          {a.drive_link && (
                            <a className="btn btn-ghost btn-sm" href={a.drive_link} target="_blank" rel="noreferrer">
                              Open
                            </a>
                          )}
                        </div>
                      ))}
                  </div>
                </div>
              )}
              {(folderLink || driveLink) && (
                <div className="detail-full detail-actions">
                  {folderLink && (
                    <a className="btn btn-secondary btn-sm" href={folderLink} target="_blank" rel="noreferrer">
                      Open folder in Drive
                    </a>
                  )}
                  {driveLink && (
                    <a className="btn btn-ghost btn-sm" href={driveLink} target="_blank" rel="noreferrer">
                      Open file
                    </a>
                  )}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

export default function ActivitySection() {
  const [appendedRows, setAppendedRows] = useState([])
  const [hasMoreAppended, setHasMoreAppended] = useState(false)
  const [expandedId, setExpandedId] = useState(null)
  const [loadingMore, setLoadingMore] = useState(false)

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['activity', 0],
    queryFn: () => getActivity({ limit: PAGE, offset: 0 }),
    staleTime: 25_000,
  })

  const firstPage = Array.isArray(data) ? data : []
  const rows = [...firstPage, ...appendedRows]
  const hasMore = firstPage.length === PAGE && (appendedRows.length === 0 || hasMoreAppended)

  function handleRefresh() {
    setAppendedRows([])
    setHasMoreAppended(false)
    queryClient.invalidateQueries({ queryKey: ['activity', 0] })
  }

  async function loadMore() {
    setLoadingMore(true)
    try {
      const nextOffset = firstPage.length + appendedRows.length
      const page = await getActivity({ limit: PAGE, offset: nextOffset })
      const next = Array.isArray(page) ? page : []
      setAppendedRows((prev) => [...prev, ...next])
      setHasMoreAppended(next.length === PAGE)
    } catch {
      // silent — user can retry with the Refresh button
    } finally {
      setLoadingMore(false)
    }
  }

  if (isLoading && rows.length === 0) return <div className="section-loading">Loading activity…</div>
  if (error && rows.length === 0) return <div className="section-error">{error.message}</div>

  return (
    <section className="dashboard-section">
      <header className="section-header">
        <div>
          <p className="section-desc">Every email the assistant has processed.</p>
        </div>
        <div className="section-actions">
          <button
            className="btn btn-secondary btn-sm"
            onClick={handleRefresh}
            disabled={isLoading || isFetching}
          >
            Refresh
          </button>
        </div>
      </header>

      <div className="section-content">
        {rows.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-text">No activity yet</div>
          </div>
        ) : (
          <ActivityTable
            rows={rows}
            expandedId={expandedId}
            onToggle={(id) => setExpandedId((cur) => (cur === id ? null : id))}
          />
        )}

        {hasMore && (
          <div className="load-more">
            <button className="btn btn-secondary" onClick={loadMore} disabled={loadingMore || isFetching}>
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
