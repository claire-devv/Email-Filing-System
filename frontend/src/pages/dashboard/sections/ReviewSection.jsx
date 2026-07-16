import { useState, useEffect, useRef } from 'react'
import { queryClient } from '../../../lib/queryClient'
import {
  getReviewItems,
  getLevel3Options,
  getArtifactFileBlob,
  approveReview,
  correctReview,
  rejectReview,
  fileSplitReview,
} from '../../../api/client'
import { useFolderRules, useActivityStats, useReviewItems } from '../../../hooks/useRresData'
import { tokenUtils } from '../../../utils/tokenUtils'
import Combobox from '../../../components/Combobox'
import { openBlobInNewTab } from '../../../utils/openBlob'
import { useToasts } from '../../../components/Toasts'
import { formatDate, formatDateTime, formatTimeOfDay } from '../../../utils/datetime'

const PAGE = 50
// Must match AUTO_FILE_CONFIDENCE in backend/app/core/config.py
const AUTO_FILE_CONFIDENCE = 80

// Level 2 subfolder_rule -> Level 3 input behaviour. Placeholders show the exact spec format:
// "<Brand> (<last4>)" for bank/card, "<Lender>" for loans, "<YYYY>" for year.
const LEVEL3 = {
  none: null,
  by_year: { label: 'Year', placeholder: '2026' },
  by_bank: { label: 'Bank name', placeholder: 'e.g. Chase Bank (1234)' },
  by_lender: { label: 'Lender', placeholder: 'e.g. Castellan Capital' },
  by_credit_card: { label: 'Card name', placeholder: 'e.g. American Express (4567)' },
}

function confidenceBadge(c) {
  if (c == null) return 'badge-muted'
  if (c >= 80) return 'badge-success'
  if (c >= 60) return 'badge-warn'
  return 'badge-danger'
}

// Prefer the name the file will carry in Drive; fall back to generic labels for
// internal artifacts (the standalone email-body PDF is never filed on its own).
function artifactLabel(a) {
  if (a.drive_filename) return a.drive_filename
  if (a.kind === 'email_body') return 'Email preview'
  if (a.kind === 'combined_package') return 'Combined PDF'
  return a.original_filename || 'Attachment'
}

function attachmentNames(item) {
  return (item.artifacts || [])
    .filter((a) => a.kind === 'attachment')
    .map((a) => a.original_filename || 'Attachment')
}

// "Jane Doe <jane@acme.com>" -> "Jane Doe" (falls back to the raw address).
function senderName(raw) {
  if (!raw) return 'Unknown sender'
  const name = raw.replace(/<[^>]*>/g, '').replace(/"/g, '').trim()
  return name || raw
}

// Short tag for where a review item came from. "email" is the default and shows nothing.
function sourceLabel(source) {
  if (source === 'client_uploads') return 'Client upload'
  if (source === 'rres_uploads') return 'RRES upload'
  return null
}

// "Jane Doe <jane@acme.com>" -> "jane@acme.com"; empty when there's no separate address.
function senderAddress(raw) {
  const match = /<([^>]+)>/.exec(raw || '')
  return match ? match[1] : ''
}

function isUnknown(item) {
  const p = item.proposed || {}
  return p.is_known_entity === false || !p.entity
}

// Plain-language explanation for each backend gate reason (from decision_audit.reasons), so the
// reviewer sees WHY the item was held, not a generic "needs a decision".
const REASON_LABELS = {
  low_confidence: 'The AI wasn’t confident enough to file on its own',
  unknown_entity: "Client folder doesn't exist yet",
  missing_entity: 'No client could be identified',
  invalid_level2: 'The document category needs a human decision',
  missing_required_level3: 'A required sub-folder (e.g. bank / year) is missing',
  too_many_entities: 'This email has documents for more clients than can be split automatically',
  multiple_entities: 'Another client was referenced but has no document of its own — confirm where this files',
  claude_requested_review: 'The AI flagged this as unclear and asked for a human',
  conversion_failure: 'The email couldn’t be converted to PDF',
  partial_conversion_failure: 'An attachment couldn’t be fully read',
  upload_entity_mismatch: 'The document content matches a different client than the folder it was uploaded to',
  unsafe_reject: 'This looked like a non-filing email but couldn’t be safely dismissed',
}

// Plain-language reasons this email is waiting on a human — never raw AI reasoning.
function whyReview(item) {
  const p = item.proposed || {}
  const reasons = []
  // Prefer the actual backend gate reasons so the "why" is specific and accurate.
  const gateReasons = (item.decision_audit || {}).reasons || []
  for (const code of gateReasons) {
    const label = REASON_LABELS[code]
    if (label && !reasons.includes(label)) reasons.push(label)
  }
  // Fallbacks / extra context the gate reasons don't cover.
  if (reasons.length === 0 && isUnknown(item)) reasons.push("Client folder doesn't exist yet")
  if (reasons.length === 0 && typeof p.confidence === 'number' && p.confidence < AUTO_FILE_CONFIDENCE)
    reasons.push(`Confidence below threshold (${p.confidence}%)`)
  if (item.urgent) reasons.push('Marked urgent')
  if (reasons.length === 0) reasons.push('Needs a human decision')
  return reasons
}

// An email carries documents for more than one client when its attachments resolve to more than
// one distinct entity. We read that from the per-attachment classifications (the most reliable
// signal — it works even when one entity is brand-new/unknown, which never appears in
// additional_entities/auto_split_entities). Only offer the split when there are ≥2 attachments to
// route — a single file can't be split.
function multiEntityInfo(item) {
  const audit = item.decision_audit || {}
  const proposed = item.proposed || {}
  const additional = Array.isArray(audit.additional_entities) ? audit.additional_entities : []
  const autoSplit = Array.isArray(audit.auto_split_entities) ? audit.auto_split_entities : []
  const ac = audit.artifact_classifications || {}
  // Agreed-decorative signature/logo images (backend-stamped) carry no entity signal — they
  // must not count as attachments to split, nor contribute their fallback entity to the list.
  const decorativeKeys = new Set(
    (item.artifacts || [])
      .filter((a) => a.decorative)
      .flatMap((a) => [a.original_filename, a.kind].filter(Boolean)),
  )
  const perAttachment = Object.entries(ac)
    .filter(([key]) => key !== 'email_body' && !decorativeKeys.has(key))
    .map(([, v]) => (v && typeof v === 'object' ? v.entity : null))
    .filter(Boolean)
  const attachments = (item.artifacts || []).filter((a) => a.kind === 'attachment' && !a.decorative)
  const entities = [
    ...new Set([proposed.entity, ...additional, ...autoSplit, ...perAttachment].filter(Boolean)),
  ]
  const isMulti = entities.length > 1 && attachments.length > 1
  return { isMulti, entities, attachmentCount: attachments.length }
}

// Mirror of backend _trim_redundant_date: strip month+year and trailing year from a summary
// so the preview matches what _filename() will actually produce in Drive.
function trimRedundantDate(summary) {
  if (!summary) return ''
  let s = summary.replace(/\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:19|20)\d{2}\b/gi, '')
  s = s.replace(/(?<!\d)(?:19|20)\d{2}\b(?!\s*\w)/, '')
  return s.replace(/\s{2,}/g, ' ').replace(/^[\s-,]+|[\s-,]+$/g, '').trim()
}

// Live Drive filename preview: date prefix + trimmed summary. Mirrors backend _filename().
// The prefix comes from the reviewer's Document-date field when set (so the preview tracks
// edits, matching what the backend files), falling back to the artifact's existing date.
function driveFilenamePreview(artifact, fileSummary, documentDate) {
  if (!fileSummary) return ''
  let prefix = ''
  const iso = (documentDate || '').slice(0, 10)
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    prefix = iso.replace(/-/g, '.') + ' - '
  } else {
    // Extract the date prefix from drive_filename ("2026.05.31 - ...") or original_filename
    const src = artifact?.drive_filename || artifact?.original_filename || ''
    const m = src.match(/^(\d{4}\.\d{2}\.\d{2})/)
    if (m) prefix = m[1] + ' - '
  }
  const trimmed = trimRedundantDate(fileSummary)
  return prefix + trimmed + '.pdf'
}

// Derive a filename summary from the original attachment filename when Claude's summary is
// missing or identical across all attachments (which causes Drive name collisions).
// Strips extension, date prefixes like "2026.05.31 - ", and month+year tokens like "May 2026".
function summaryFromFilename(filename) {
  if (!filename) return ''
  let s = filename.replace(/\.pdf$/i, '').replace(/\.docx?$/i, '')
  // Strip leading date prefix "YYYY.MM.DD - "
  s = s.replace(/^\d{4}\.\d{2}\.\d{2}\s*-\s*/, '')
  // Strip "Month YYYY" patterns
  s = s.replace(/\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}\b/gi, '')
  // Strip trailing standalone year (but not leading address numbers like 1322, 2002)
  s = s.replace(/(?<!\d)(?:19|20)\d{2}\b(?!\s*\w)/, '')
  return s.replace(/\s{2,}/g, ' ').replace(/[-\s]+$/, '').trim()
}

// One editable assignment per real attachment, pre-filled from Claude's per-attachment guess.
// When all Claude summaries are identical (collision risk), fall back to the filename-derived
// summary so each row has a distinct, meaningful pre-fill the reviewer can confirm or edit.
function buildSplitRows(item) {
  const audit = item.decision_audit || {}
  const ac = audit.artifact_classifications || {}
  const summaries = audit.artifact_summaries || {}
  const proposed = item.proposed || {}
  // Decorative signature/logo images are never filed standalone (they stay inside the archived
  // email PDF), so they get no assignment row — the backend split validator skips them too.
  const attachments = (item.artifacts || []).filter((a) => a.kind === 'attachment' && !a.decorative)

  // Detect collision: all non-empty Claude summaries are the same string
  const claudeSummaries = attachments
    .map((a) => summaries[a.original_filename || a.kind])
    .filter(Boolean)
  const allIdentical =
    claudeSummaries.length > 1 && new Set(claudeSummaries).size === 1

  return attachments.map((a) => {
    const key = a.original_filename || a.kind
    const c = ac[key] || {}
    const claudeSummary = summaries[key]
    const fileSummary = allIdentical
      ? summaryFromFilename(a.original_filename) || claudeSummary || proposed.file_summary || ''
      : claudeSummary || proposed.file_summary || ''
    return {
      artifactId: a.id,
      artifact: a,
      name: a.original_filename || a.drive_filename || 'Attachment',
      entity: c.entity || proposed.entity || '',
      entityConfidence: typeof c.entity_confidence === 'number' ? c.entity_confidence : null,
      level2: c.level2 || proposed.level2 || '',
      level3: c.level3 || '',
      fileSummary,
    }
  })
}

export default function ReviewSection({ focusId = null, onResolvedAny }) {
  const [appendedItems, setAppendedItems] = useState([])
  const [hasMoreAppended, setHasMoreAppended] = useState(false)
  const [selectedId, setSelectedId] = useState(focusId)
  const [loadingMore, setLoadingMore] = useState(false)
  const listScrollY = useRef(0)

  // Cached reads — folder rules and stats are shared with Dashboard-level hooks (zero extra calls).
  // useReviewItems is used here directly so this component owns its own 30s polling and
  // doesn't silently rely on the parent dashboard mounting the same hook.
  const { data: reviewData, isLoading, error: reviewError, refetch } = useReviewItems('pending', 0)
  const { data: folderRulesData } = useFolderRules()
  const { data: statsData } = useActivityStats()

  const firstPage = Array.isArray(reviewData) ? reviewData : []
  const items = [...firstPage, ...appendedItems]
  const level2Folders = folderRulesData?.rules?.level_2_folders || []
  const errorsToday = statsData?.errors_today || 0
  const hasMore = firstPage.length === PAGE && (appendedItems.length === 0 || hasMoreAppended)

  function openItem(id) {
    listScrollY.current = window.scrollY
    setSelectedId(id)
  }

  // Details always open from the top; Back restores the saved list position.
  useEffect(() => {
    if (selectedId != null) {
      window.scrollTo(0, 0)
    } else {
      window.scrollTo(0, listScrollY.current)
    }
  }, [selectedId])

  function handleResolved(id) {
    // Optimistic cache update: remove resolved item immediately.
    queryClient.setQueryData(['review', 'items', 'pending', 0], (old) =>
      Array.isArray(old) ? old.filter((it) => it.id !== id) : old,
    )
    setAppendedItems((prev) => prev.filter((it) => it.id !== id))
    setSelectedId(null)
    onResolvedAny?.()
    queryClient.invalidateQueries({ queryKey: ['activity', 'stats'] })
  }

  function handleRefresh() {
    setAppendedItems([])
    setHasMoreAppended(false)
    refetch()
  }

  async function loadMore() {
    setLoadingMore(true)
    try {
      const nextOffset = firstPage.length + appendedItems.length
      const data = await getReviewItems('pending', { limit: PAGE, offset: nextOffset })
      const page = Array.isArray(data) ? data : []
      setAppendedItems((prev) => [...prev, ...page])
      setHasMoreAppended(page.length === PAGE)
    } catch {
      // silent — user can retry with Refresh
    } finally {
      setLoadingMore(false)
    }
  }

  const selected = items.find((it) => it.id === selectedId)

  if (isLoading && items.length === 0) return <div className="section-loading">Loading review items…</div>
  if (reviewError && items.length === 0) return <div className="section-error">{reviewError.message}</div>

  if (selected) {
    return (
      <ReviewDetail
        item={selected}
        level2Folders={level2Folders}
        onBack={() => setSelectedId(null)}
        onResolved={handleResolved}
        onRefresh={handleRefresh}
      />
    )
  }

  const unknownCount = items.filter(isUnknown).length
  const lowConfCount = items.filter(
    (it) => !isUnknown(it) && typeof it.proposed?.confidence === 'number' && it.proposed.confidence < 80,
  ).length

  return (
    <section className="dashboard-section">
      <header className="section-header">
        <div>
          <p className="section-desc">
            Emails the assistant wasn't confident about. Open one to approve or correct where it files.
          </p>
        </div>
        <div className="section-actions">
          <button
            className="btn btn-secondary btn-sm"
            onClick={handleRefresh}
            disabled={isLoading}
          >
            Refresh
          </button>
        </div>
      </header>

      {items.length > 0 && (
        <div className="summary-chips">
          <SummaryChip label="Pending" value={items.length} />
          <SummaryChip label="Unknown entities" value={unknownCount} tone={unknownCount ? 'warn' : null} />
          <SummaryChip label="Low confidence" value={lowConfCount} tone={lowConfCount ? 'warn' : null} />
          <SummaryChip label="Errors today" value={errorsToday} tone={errorsToday ? 'danger' : null} />
        </div>
      )}

      <div className="section-content">
        {items.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-text">Nothing needs review 🎉</div>
            <div className="empty-state-hint">
              New items appear here when the assistant needs a human decision.
            </div>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table data-table--clickable">
              <thead>
                <tr>
                  <th className="col-time">Received</th>
                  <th className="col-sender">From</th>
                  <th>Subject</th>
                  <th className="col-files">Files</th>
                  <th className="col-entity">Suggested entity</th>
                  <th className="col-conf">Confidence</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const p = item.proposed || {}
                  const em = item.email || {}
                  const fileCount = attachmentNames(item).length
                  return (
                    <tr key={item.id} className="data-table-row" onClick={() => openItem(item.id)}>
                      <td className="cell-muted col-time">
                        <div>{formatDate(em.received_at)}</div>
                        {em.received_at && <div className="cell-subtime">{formatTimeOfDay(em.received_at)}</div>}
                        {sourceLabel(item.source) && (
                          <div className="source-tag">{sourceLabel(item.source)}</div>
                        )}
                      </td>
                      <td className="cell-muted col-sender" title={em.sender || ''}>
                        {senderName(em.sender)}
                      </td>
                      <td className="cell-strong">
                        {em.subject || '(no subject)'}
                      </td>
                      <td className="col-files cell-muted">
                        {fileCount > 0 ? `📎 ${fileCount}` : '—'}
                      </td>
                      <td className="col-entity">
                        {p.entity ? (
                          p.is_known_entity ? (
                            <span className="cell-muted">{p.entity}</span>
                          ) : (
                            <span className="badge badge-warn" title={p.entity}>
                              NEW ENTITY
                            </span>
                          )
                        ) : (
                          <span className="badge badge-muted">Unknown</span>
                        )}
                      </td>
                      <td className="col-conf">
                        <span className={`badge ${confidenceBadge(p.confidence)}`}>
                          {typeof p.confidence === 'number' ? `${p.confidence}%` : '—'}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {hasMore && (
          <div className="load-more">
            <button className="btn btn-secondary" onClick={loadMore} disabled={loadingMore || isLoading}>
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

function SummaryChip({ label, value, tone }) {
  return (
    <div className={`summary-chip${tone ? ` summary-chip--${tone}` : ''}`}>
      <span className="summary-chip-value">{value}</span>
      <span className="summary-chip-label">{label}</span>
    </div>
  )
}

function ReviewDetail({ item, level2Folders, onBack, onResolved, onRefresh }) {
  const proposed = item.proposed || {}
  const email = item.email || {}
  const { addToast, updateToast } = useToasts()
  const [entity, setEntity] = useState(proposed.entity || '')
  const [entityPicked, setEntityPicked] = useState(false)
  const [level2, setLevel2] = useState(proposed.level2 || '')
  const [level3, setLevel3] = useState(proposed.level3 || '')
  const [level3Options, setLevel3Options] = useState([])
  const [fileSummary, setFileSummary] = useState(proposed.file_summary || '')
  const [documentDate, setDocumentDate] = useState(proposed.document_date || '')
  const [notes, setNotes] = useState('')
  const [learn, setLearn] = useState(true)
  const [showDetails, setShowDetails] = useState(false)
  const [showReject, setShowReject] = useState(false)
  const [rejectReason, setRejectReason] = useState('')

  // Multi-entity split state. When an email carries documents for >1 client, default to the
  // per-attachment split panel; the reviewer can switch to the single-entity form if they'd
  // rather file everything to one client.
  const multi = multiEntityInfo(item)
  const [splitMode, setSplitMode] = useState(multi.isMulti)
  const [splitRows, setSplitRows] = useState(() => buildSplitRows(item))
  const decorativeCount = (item.artifacts || []).filter((a) => a.decorative).length

  function updateRow(index, next) {
    setSplitRows((rows) => rows.map((r, i) => (i === index ? next : r)))
  }

  const ruleFor = (l2) => level2Folders.find((f) => f.name === l2)?.subfolder_rule || 'none'
  // by_year auto-fills on the server, so its Year field is optional; other Level-3 rules require it.
  const level3Required = (l2) => {
    const r = ruleFor(l2)
    return !!LEVEL3[r] && r !== 'by_year'
  }
  const canSplit =
    splitRows.length > 0 &&
    splitRows.every(
      (r) => r.entity && r.level2 && r.fileSummary && (!level3Required(r.level2) || r.level3),
    )

  function onSplit() {
    const label = email.subject || '(no subject)'
    const assignments = splitRows.map((r) => ({
      artifact_id: r.artifactId,
      entity: r.entity,
      level2: r.level2,
      level3: LEVEL3[ruleFor(r.level2)] ? r.level3 || null : null,
      file_summary: r.fileSummary,
    }))
    run(label, () => fileSplitReview(item.id, { assignments, document_date: documentDate || null, reviewed_by: tokenUtils.getUserEmail() || null }))
  }

  async function openArtifact(artifact) {
    try {
      await openBlobInNewTab(() => getArtifactFileBlob(item.id, artifact.id))
    } catch (err) {
      const toastId = addToast('Could not open file')
      updateToast(toastId, { status: 'error', text: 'Could not open file', detail: err.message || 'Failed to load file' })
    }
  }

  const rule = level2Folders.find((f) => f.name === level2)?.subfolder_rule || 'none'
  const level3Cfg = LEVEL3[rule] || null

  // Suggest known Level-3 values (e.g. bank names) for this subfolder.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      if (!LEVEL3[rule]) {
        if (!cancelled) setLevel3Options([])
        return
      }
      try {
        const data = await getLevel3Options(level2, entity)
        if (!cancelled) setLevel3Options(data?.options || [])
      } catch {
        if (!cancelled) setLevel3Options([])
      }
    })()
    return () => {
      cancelled = true
    }
  }, [level2, entity, rule])

  // A picked search result is known; typing a new name (or the proposal's own unknown
  // entity) is new. The backend creates the folder on file for new entities.
  const isKnownEntity = entityPicked || (entity === proposed.entity && !!proposed.is_known_entity)
  const folderChanged =
    entity !== (proposed.entity || '') ||
    level2 !== (proposed.level2 || '') ||
    (level3Cfg ? level3 : '') !== (proposed.level3 || '')
  const detailsChanged =
    fileSummary !== (proposed.file_summary || '') ||
    !!notes ||
    (documentDate || '').slice(0, 10) !== (proposed.document_date || '').slice(0, 10)
  const changed = folderChanged || detailsChanged

  // Two actions only (+ reject). Approve is reserved for an untouched, known-entity
  // suggestion; everything else (an edit, or filing to a new entity) is Save & file,
  // and the backend handles new-entity creation, alias and learning from the values.
  const useApprove = !changed && isKnownEntity
  const primaryLabel = useApprove ? 'Approve & file' : 'Save & file'

  const reasons = whyReview(item)
  const possibleMatch = [proposed.entity, proposed.level2, proposed.level3].filter(Boolean).join(' / ')

  // Kick the action off in the background: return to the list immediately and track
  // progress via a bottom-right toast, so multiple filings can run concurrently.
  function run(label, fn) {
    onResolved(item.id)
    const toastId = addToast(`Filing "${label}"…`)
    fn()
      .then(() => {
        updateToast(toastId, { status: 'done', text: `Filed "${label}"` })
      })
      .catch((err) => {
        updateToast(toastId, {
          status: 'error',
          text: `Failed to file "${label}"`,
          detail: err.message || 'Action failed',
        })
        onRefresh?.()
      })
  }

  function onPrimary() {
    const label = email.subject || '(no subject)'
    const reviewedBy = tokenUtils.getUserEmail() || null
    if (useApprove) {
      run(label, () => approveReview(item.id, reviewedBy))
    } else {
      run(label, () =>
        correctReview(item.id, {
          entity,
          level2,
          level3: level3Cfg ? level3 || null : null,
          file_summary: fileSummary,
          document_date: documentDate || null,
          notes: notes || null,
          learn,
          reviewed_by: reviewedBy,
        }),
      )
    }
  }


  const canFile = !!entity && !!level2 && !!fileSummary && (!level3Required(level2) || !!level3)

  return (
    <section className="dashboard-section review-detail">
      <header className="section-header">
        <div className="review-detail-head">
          <button className="btn btn-ghost btn-sm" onClick={onBack}>
            ← Back
          </button>
          <div>
            <h2 className="section-title">{email.subject || '(no subject)'}</h2>
          </div>
        </div>
        <div className="section-actions">
          {splitMode && multi.isMulti ? (
            (() => {
              const knownSet = new Set(proposed.known_split_entities || [])
              const ac = (item.decision_audit || {}).artifact_classifications || {}
              const newCount = multi.entities.filter((e) => !knownSet.has(e)).length
              // Known entities assigned with low confidence are unreliable — warn the reviewer.
              const lowConfCount = splitRows.filter((r) => {
                const isKnown = knownSet.has(r.entity)
                const lowConf = r.entityConfidence !== null && r.entityConfidence < 80
                return isKnown && lowConf
              }).length
              if (newCount > 0) {
                return <span className="badge badge-warn">{newCount} new {newCount === 1 ? 'entity' : 'entities'}</span>
              } else if (lowConfCount > 0) {
                return <span className="badge badge-warn">Verify assignments</span>
              } else {
                return <span className="badge badge-success">All known</span>
              }
            })()
          ) : isKnownEntity ? (
            <span className="badge badge-success">Known entity</span>
          ) : (
            <span className="badge badge-warn">New entity</span>
          )}
          {item.urgent && <span className="badge badge-danger">Urgent</span>}
        </div>
      </header>

      <div className="section-content review-detail-body">
        {/* Who sent it, when, and what's in it — everything that identifies the email. */}
        <div className="review-email-card">
          <div className="review-email-row">
            <div className="detail-label">{!email.sender && sourceLabel(item.source) ? 'Source' : 'From'}</div>
            {!email.sender && sourceLabel(item.source) ? (
              <div><span className="source-tag">{sourceLabel(item.source)}</span></div>
            ) : (
              <div className="review-sender">
                <span className="review-sender-avatar" aria-hidden="true">
                  {senderName(email.sender).charAt(0).toUpperCase()}
                </span>
                <strong>{senderName(email.sender)}</strong>
                {senderAddress(email.sender) && (
                  <span className="review-sender-address">{senderAddress(email.sender)}</span>
                )}
              </div>
            )}
          </div>
          <div className="review-email-row">
            <div className="detail-label">Received</div>
            <div>{formatDateTime(email.received_at)}</div>
          </div>
          {(() => {
            // Uploads are a single document, not an email -> show only the document artifact (no
            // "Email preview"/"Combined PDF"). Guards pre-fix items that still have those artifacts.
            const isUpload = item.source === 'client_uploads' || item.source === 'rres_uploads'
            const visibleArtifacts = isUpload
              ? (item.artifacts || []).filter((a) => a.kind === 'attachment')
              : (item.artifacts || [])
            if (visibleArtifacts.length === 0) return null
            return (
              <div className="review-email-row">
                <div className="detail-label">{isUpload ? 'Document' : 'Attachments'}</div>
                <div className="chip-row">
                  {visibleArtifacts.map((a) => (
                    <button key={a.id} type="button" className="chip chip--button" onClick={() => openArtifact(a)} title="Open in new tab">
                      📄 {artifactLabel(a)}
                    </button>
                  ))}
                </div>
              </div>
            )
          })()}
        </div>

        {/* Why this needs review — plain language, full reasoning behind a toggle */}
        <div className="why-block">
          <div className="why-head">Why this needs review</div>
          <ul className="why-list">
            {reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          {possibleMatch && (
            <div className="why-match">
              Possible match: <strong>{possibleMatch}</strong>{' '}
              <span className={`badge ${confidenceBadge(proposed.confidence)}`}>
                {typeof proposed.confidence === 'number' ? `${proposed.confidence}%` : '—'}
              </span>
            </div>
          )}
          {proposed.reason && (
            <>
              <button type="button" className="link-btn" onClick={() => setShowDetails((s) => !s)}>
                {showDetails ? 'Hide details ▲' : 'Show details ▼'}
              </button>
              {showDetails && <p className="suggestion-reason">{proposed.reason}</p>}
            </>
          )}
        </div>

        {/* Multi-entity: offer per-attachment split, default on for multi-entity emails. */}
        {multi.isMulti && (
          <div className="split-banner">
            <div className="split-banner-text">
              <strong>Documents for {multi.entities.length} clients in one email</strong>
              <div className="split-banner-entities">
                {multi.entities.map((e) => (
                  <span key={e} className="badge badge-muted" title={e}>
                    {e}
                  </span>
                ))}
              </div>
            </div>
            <button className="btn btn-secondary btn-sm" onClick={() => setSplitMode((s) => !s)}>
              {splitMode ? 'File all to one client instead' : 'Split by attachment'}
            </button>
          </div>
        )}

        {splitMode && multi.isMulti ? (
          /* Per-attachment split panel — flexible for 2, 3, or 4+ entities. */
          <div className="split-panel">
            <p className="split-hint">
              Choose where each attachment files. The email itself is saved to every client's
              Communications folder.
            </p>
            {decorativeCount > 0 && (
              <p className="split-hint split-hint--decorative">
                {decorativeCount} signature/decorative image{decorativeCount > 1 ? 's' : ''} kept
                inside the archived email PDF — not filed separately.
              </p>
            )}
            {splitRows.map((row, i) => (
              <SplitRow
                key={row.artifactId}
                row={row}
                itemId={item.id}
                level2Folders={level2Folders}
                level3Required={level3Required(row.level2)}
                knownSplitEntities={proposed.known_split_entities || []}
                documentDate={documentDate}
                onChange={(next) => updateRow(i, next)}
                onOpen={() => openArtifact(row.artifact)}
              />
            ))}
            <label className="field split-date">
              <span>Document date — applied to all (optional)</span>
              <input
                className="form-control"
                type="date"
                value={documentDate?.slice(0, 10) || ''}
                onChange={(e) => setDocumentDate(e.target.value)}
              />
            </label>
          </div>
        ) : (
        /* Folder form */
        <div className="review-form">
          <label className="field">
            <span>Master client folder</span>
            <Combobox
              value={entity}
              onChange={(val, known) => {
                setEntity(val)
                setEntityPicked(!!known)
              }}
            />
          </label>

          <label className="field">
            <span>Subfolder</span>
            <select
              className="form-control"
              value={level2}
              onChange={(e) => {
                setLevel2(e.target.value)
                setLevel3('')
              }}
            >
              <option value="">Select subfolder…</option>
              {level2Folders.map((f) => (
                <option key={f.name} value={f.name}>
                  {f.name}
                </option>
              ))}
            </select>
          </label>

          {level3Cfg && (
            <label className="field">
              <span>{level3Cfg.label}</span>
              <input
                className="form-control"
                list={`l3-${item.id}`}
                value={level3}
                placeholder={level3Cfg.placeholder}
                onChange={(e) => setLevel3(e.target.value)}
              />
              <datalist id={`l3-${item.id}`}>
                {level3Options.map((opt) => (
                  <option key={opt} value={opt} />
                ))}
              </datalist>
            </label>
          )}

          <label className="field">
            <span>File summary</span>
            <input className="form-control" value={fileSummary} onChange={(e) => setFileSummary(e.target.value)} />
            {fileSummary && (() => {
              const previewArtifact = (item.artifacts || []).find((a) => a.kind === 'attachment') || (item.artifacts || [])[0]
              const preview = driveFilenamePreview(previewArtifact, fileSummary, documentDate)
              return preview ? <span className="field-hint">Drive filename: <strong>{preview}</strong></span> : null
            })()}
          </label>

          <label className="field">
            <span>Document date (optional)</span>
            <input
              className="form-control"
              type="date"
              value={documentDate?.slice(0, 10) || ''}
              onChange={(e) => setDocumentDate(e.target.value)}
            />
          </label>

          <label className="field field--full">
            <span>
              Notes — why does this belong here? <em>(the assistant learns from this)</em>
            </span>
            <textarea
              className="form-control"
              rows={2}
              value={notes}
              placeholder="e.g. Bare & Swett dec pages for this client always file under Insurance."
              onChange={(e) => setNotes(e.target.value)}
            />
          </label>

          {!useApprove && (
            <label className="field field--full checkbox-field">
              <input type="checkbox" checked={learn} onChange={(e) => setLearn(e.target.checked)} />
              <span>Apply this correction to similar future emails</span>
            </label>
          )}
        </div>
        )}

        <div className="review-actions">
          {splitMode && multi.isMulti ? (
            <button className="btn btn-primary" onClick={onSplit} disabled={!canSplit}>
              File each separately
            </button>
          ) : (
            <button className="btn btn-primary" onClick={onPrimary} disabled={!canFile}>
              {primaryLabel}
            </button>
          )}

          {!showReject ? (
            <button className="btn btn-ghost" onClick={() => setShowReject(true)}>
              Not a filing email
            </button>
          ) : (
            <div className="review-reject">
              <input
                className="form-control"
                value={rejectReason}
                placeholder="Reason (optional)"
                onChange={(e) => setRejectReason(e.target.value)}
              />
              <button
                className="btn btn-danger"
                onClick={() =>
                  run(email.subject || '(no subject)', () =>
                    rejectReview(item.id, rejectReason || 'Not a filing email.', tokenUtils.getUserEmail() || null),
                  )
                }
              >
                Confirm reject
              </button>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}

// One attachment's assignment in the split panel: where it files (client + subfolder + optional
// Level 3) and the name it carries. Manages its own Level-3 suggestions for its chosen subfolder.
function SplitRow({ row, itemId, level2Folders, level3Required, knownSplitEntities, documentDate, onChange, onOpen }) {
  const rule = level2Folders.find((f) => f.name === row.level2)?.subfolder_rule || 'none'
  const level3Cfg = LEVEL3[rule] || null
  const [level3Options, setLevel3Options] = useState([])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      if (!LEVEL3[rule] || !row.level2) {
        if (!cancelled) setLevel3Options([])
        return
      }
      try {
        const data = await getLevel3Options(row.level2, row.entity)
        if (!cancelled) setLevel3Options(data?.options || [])
      } catch {
        if (!cancelled) setLevel3Options([])
      }
    })()
    return () => {
      cancelled = true
    }
  }, [row.level2, row.entity, rule])

  return (
    <div className="split-row">
      <button type="button" className="chip chip--button split-row-file" onClick={onOpen} title="Open in new tab">
        📄 {row.name}
      </button>
      <div className="split-row-fields">
        <label className="field">
          <span>
            Client folder
            {row.entity && (() => {
              const isKnown = knownSplitEntities.includes(row.entity)
              const lowConf = row.entityConfidence !== null && row.entityConfidence < 80
              if (!isKnown) return <span className="badge badge-warn split-entity-badge">New folder</span>
              if (lowConf) return <span className="badge badge-warn split-entity-badge" title={`Claude confidence: ${row.entityConfidence}% — please verify`}>Verify</span>
              return <span className="badge badge-success split-entity-badge">Known</span>
            })()}
          </span>
          <Combobox value={row.entity} onChange={(val) => onChange({ ...row, entity: val, entityConfidence: null })} />
        </label>

        <label className="field">
          <span>Subfolder</span>
          <select
            className="form-control"
            value={row.level2}
            onChange={(e) => onChange({ ...row, level2: e.target.value, level3: '' })}
          >
            <option value="">Select subfolder…</option>
            {level2Folders.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name}
              </option>
            ))}
          </select>
        </label>

        {level3Cfg && (
          <label className="field">
            <span>
              {level3Cfg.label}
              {level3Required ? '' : ' (optional)'}
            </span>
            <input
              className="form-control"
              list={`l3s-${itemId}-${row.artifactId}`}
              value={row.level3}
              placeholder={level3Cfg.placeholder}
              onChange={(e) => onChange({ ...row, level3: e.target.value })}
            />
            <datalist id={`l3s-${itemId}-${row.artifactId}`}>
              {level3Options.map((opt) => (
                <option key={opt} value={opt} />
              ))}
            </datalist>
          </label>
        )}

        <label className="field field--full">
          <span>File name summary</span>
          <input
            className="form-control"
            value={row.fileSummary}
            placeholder="e.g. Cash Basis Financial Report"
            onChange={(e) => onChange({ ...row, fileSummary: e.target.value })}
          />
          {row.fileSummary && (
            <span className="field-hint">
              Drive filename: <strong>{driveFilenamePreview(row.artifact, row.fileSummary, documentDate)}</strong>
            </span>
          )}
        </label>
      </div>
    </div>
  )
}
