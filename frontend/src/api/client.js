// Single source of truth for talking to the RRES backend.
// All requests go through apiFetch: it prefixes VITE_API_BASE_URL, attaches the
// bearer token, and on a 401 clears the token and bounces back to the login screen.
import { tokenUtils } from '../utils/tokenUtils'

const BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export async function apiFetch(path, { skipAuthRedirect = false, headers, ...options } = {}) {
  const finalHeaders = { 'Content-Type': 'application/json', ...(headers || {}) }
  const auth = tokenUtils.getAuthHeader()
  if (auth) finalHeaders.Authorization = auth

  const response = await fetch(`${BASE}${path}`, {
    ...options,
    headers: finalHeaders,
    credentials: 'include',
  })

  if (response.status === 401 && !skipAuthRedirect) {
    tokenUtils.clearToken()
    if (typeof window !== 'undefined') window.location.reload()
    throw new ApiError('Your session has expired. Please sign in again.', 401)
  }
  return response
}

async function getJson(path) {
  const res = await apiFetch(path)
  if (!res.ok) throw new ApiError(`Request failed (${res.status})`, res.status)
  return res.json()
}

async function sendJson(path, method, body, opts = {}) {
  const res = await apiFetch(path, { method, body: JSON.stringify(body ?? {}), ...opts })
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const data = await res.json()
      detail = data.detail || detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status)
  }
  return res.json()
}

// --- Auth ---
// skipAuthRedirect: a bad-credentials 401 here must surface as an error on the
// login form, not trigger the global "session expired" reload.
export const login = (email, password) =>
  sendJson('/auth/login', 'POST', { email, password }, { skipAuthRedirect: true })

// --- Dashboard reads ---
export const getReviewItems = (status = 'pending', { limit = 50, offset = 0 } = {}) =>
  getJson(`/review/items?status=${encodeURIComponent(status)}&limit=${limit}&offset=${offset}`)
export const getActivity = ({ limit = 50, offset = 0 } = {}) =>
  getJson(`/activity?limit=${limit}&offset=${offset}`)
export const getNotificationCounts = () => getJson('/notifications/counts')
export const getActivityStats = () => getJson('/activity/stats')
export const getEntities = () => getJson('/entities')
export const searchEntities = (q = '', limit = 20) =>
  getJson(`/entities/search?q=${encodeURIComponent(q)}&limit=${limit}`)
export const getFolderRules = () => getJson('/admin/folder-rules')
export const getApiUsage = () => getJson('/admin/api-usage')
export const getLevel3Options = (level2, entity) =>
  getJson(
    `/review/items/level3-options?level2=${encodeURIComponent(level2)}` +
      (entity ? `&entity=${encodeURIComponent(entity)}` : ''),
  )

// --- Documents (filed PDFs; storage stays in Google Drive) ---
export const getDocuments = ({ q = '', entity = '', dateFrom = '', dateTo = '', limit = 50, offset = 0 } = {}) =>
  getJson(
    `/documents?q=${encodeURIComponent(q)}&entity=${encodeURIComponent(entity)}` +
      `&date_from=${dateFrom}&date_to=${dateTo}&limit=${limit}&offset=${offset}`,
  )
export async function downloadDocumentBlob(id) {
  const res = await apiFetch(`/documents/${id}/download`)
  if (!res.ok) {
    let detail = `Download failed (${res.status})`
    try {
      const data = await res.json()
      detail = data.detail || detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status)
  }
  return res.blob()
}

// Fetch a review artifact (email-body or attachment PDF) as a Blob, with the bearer
// header attached — the caller turns it into an object URL to open in a new tab.
export async function getArtifactFileBlob(reviewId, artifactId) {
  const res = await apiFetch(`/review/items/${reviewId}/artifacts/${artifactId}/file`)
  if (!res.ok) throw new ApiError(`Could not load file (${res.status})`, res.status)
  return res.blob()
}

// --- Actions ---
export const processUnread = (body = {}) => sendJson('/emails/process-unread', 'POST', body)
export const updateEntity = (id, payload) => sendJson(`/entities/${id}`, 'PATCH', payload)
export const createEntity = (payload) => sendJson('/entities', 'POST', payload)

// --- Google integration (OAuth connect, Drive root, Gmail Pub/Sub watch) ---
export const getGoogleStatus = () => getJson('/auth/google/status')
export const getGoogleConnectUrl = () => getJson('/auth/google/connect')
export const disconnectGoogle = () => sendJson('/auth/google/disconnect', 'POST')
export const getDriveRootStatus = () => getJson('/auth/google/drive-root/status')
export const updateDriveRoot = (payload) => sendJson('/auth/google/drive-root', 'PUT', payload)
export const getDriveRootFolders = () => getJson('/auth/google/drive-root/folders')
export const updateNonEntityFolders = (folders) =>
  sendJson('/auth/google/non-entity-folders', 'PUT', { folders })
export const getGmailWatchStatus = () => getJson('/admin/gmail/watch/status')
export const startGmailWatch = (payload = {}) => sendJson('/admin/gmail/watch/start', 'POST', payload)
export const renewGmailWatch = (payload = {}) => sendJson('/admin/gmail/watch/renew', 'POST', payload)
export const stopGmailWatch = () => sendJson('/admin/gmail/watch/stop', 'POST')

// --- Dashboard user management (admin only) ---
export const getUsers = () => getJson('/users')
export const createUser = (email, password) => sendJson('/users', 'POST', { email, password })
export const updateUser = (id, payload) => sendJson(`/users/${id}`, 'PATCH', payload)
export const deleteUser = (id) => sendJson(`/users/${id}`, 'DELETE')

// --- Review write actions (backend-ready; consumed by the review write-UI) ---
export const approveReview = (id, reviewedBy = null) =>
  sendJson(`/review/items/${id}/approve`, 'POST', { reviewed_by: reviewedBy })
export const correctReview = (id, payload) =>
  sendJson(`/review/items/${id}/correct`, 'POST', payload)
export const rejectReview = (id, reason = null, reviewedBy = null) =>
  sendJson(`/review/items/${id}/reject`, 'POST', { reason, reviewed_by: reviewedBy })
// Multi-entity split: file each attachment to its own entity in one action.
// payload: { assignments: [{ artifact_id, entity, level2, level3, file_summary }], document_date }
export const fileSplitReview = (id, payload) =>
  sendJson(`/review/items/${id}/file-split`, 'POST', payload)
