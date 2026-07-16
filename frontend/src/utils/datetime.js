// Single source of truth for rendering backend timestamps.
//
// The backend stores and sends UTC. Most timestamps now carry an explicit "Z"; a few
// raw-dict endpoints may still emit a naive ISO string. We treat any value without a
// timezone designator as UTC (never browser-local) and then format every timestamp in
// one fixed business timezone so all viewers see the same wall clock.
const TIME_ZONE = 'America/New_York'

function toDate(value) {
  if (!value) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  let text = String(value)
  const hasTimezone = /([zZ]|[+-]\d{2}:?\d{2})$/.test(text)
  if (!hasTimezone && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text)) {
    text = text.replace(' ', 'T') + 'Z'
  }
  const parsed = new Date(text)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

const DATETIME_OPTS = {
  timeZone: TIME_ZONE,
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  second: '2-digit',
}

const DATE_OPTS = { timeZone: TIME_ZONE, year: 'numeric', month: 'numeric', day: 'numeric' }

const TIME_OPTS = { timeZone: TIME_ZONE, hour: 'numeric', minute: '2-digit' }

export function formatDateTime(value) {
  const date = toDate(value)
  return date ? date.toLocaleString('en-US', DATETIME_OPTS) : '—'
}

export function formatDate(value) {
  const date = toDate(value)
  return date ? date.toLocaleDateString('en-US', DATE_OPTS) : '—'
}

export function formatTimeOfDay(value) {
  const date = toDate(value)
  return date ? date.toLocaleTimeString('en-US', TIME_OPTS) : ''
}
