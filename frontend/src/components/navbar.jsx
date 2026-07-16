import { useState, useRef, useEffect } from 'react'
import './navbar.css'
import { FaBars, FaBell } from 'react-icons/fa6'

function SystemStatusDot({ processingCount = 0, watchActive = false, watchError = null, lastSyncAgoMinutes = null }) {
  let dotClass = 'status-dot--inactive'
  let label = 'Gmail watch inactive'

  if (processingCount > 0) {
    dotClass = 'status-dot--processing'
    label = `Processing ${processingCount} email${processingCount > 1 ? 's' : ''}…`
  } else if (watchActive) {
    if (watchError) {
      dotClass = 'status-dot--error'
      label = `Watch error: ${watchError}`
    } else if (lastSyncAgoMinutes === null) {
      dotClass = 'status-dot--stale'
      label = 'Watch active — no sync yet'
    } else if (lastSyncAgoMinutes > 30) {
      dotClass = 'status-dot--stale'
      label = `Last sync ${lastSyncAgoMinutes}m ago — may be stale`
    } else {
      dotClass = 'status-dot--active'
      label = `Watch active — synced ${lastSyncAgoMinutes < 1 ? 'just now' : `${lastSyncAgoMinutes}m ago`}`
    }
  }

  return (
    <div className="status-dot-wrap" aria-label={label}>
      <span className={`status-dot ${dotClass}`} />
      <span className="status-tooltip">{label}</span>
    </div>
  )
}

export default function Navbar({
  title,
  userName = 'User',
  userEmail = '',
  pendingCount = 0,
  errors = [],
  reviewAlerts = [],
  systemStatus = null,
  onMenuClick,
  onGoToReview,
  onOpenReviewItem,
  onOpenError,
}) {
  const [open, setOpen] = useState(false)
  const bellRef = useRef(null)

  useEffect(() => {
    const onDocClick = (e) => {
      if (bellRef.current && !bellRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  const hasAlerts = pendingCount > 0 || errors.length > 0

  const goReview = () => {
    setOpen(false)
    onGoToReview?.()
  }
  const openError = () => {
    setOpen(false)
    onOpenError?.()
  }
  const openReviewItem = (id) => {
    setOpen(false)
    onOpenReviewItem?.(id)
  }

  return (
    <header className="navbar">
      <div className="navbar-left">
        <button className="navbar-menu-btn" onClick={onMenuClick} aria-label="Toggle navigation">
          <FaBars />
        </button>
        <h1 className="navbar-title">{title}</h1>
      </div>

      <div className="navbar-right">
        {systemStatus !== null && (
          <SystemStatusDot
            processingCount={systemStatus.processing_count}
            watchActive={systemStatus.watch_active}
            watchError={systemStatus.watch_error}
            lastSyncAgoMinutes={systemStatus.last_sync_ago_minutes}
          />
        )}

        <div className="navbar-bell-wrap" ref={bellRef}>
          <button
            className="navbar-bell"
            onClick={() => setOpen((v) => !v)}
            aria-label={pendingCount > 0 ? `${pendingCount} items pending review` : 'Notifications'}
            aria-expanded={open}
          >
            <FaBell />
            {pendingCount > 0 && <span className="navbar-bell-badge">{pendingCount}</span>}
          </button>

          {open && (
            <div className="bell-panel" role="menu">
              <div className="bell-panel-head">Notifications</div>

              {!hasAlerts && <div className="bell-empty">You’re all caught up.</div>}

              {reviewAlerts.length > 0 && (
                <>
                  <div className="bell-section-label">Needs review</div>
                  {reviewAlerts.map((item) => {
                    const subject = item.email?.subject || '(no subject)'
                    return (
                      <button
                        key={item.id}
                        className="bell-item bell-item--review"
                        title={subject}
                        onClick={() => openReviewItem(item.id)}
                      >
                        <span className="bell-item-title">{subject}</span>
                        <span className="bell-item-meta" title={item.email?.sender || ''}>
                          {item.email?.sender || ''}
                        </span>
                      </button>
                    )
                  })}
                  {pendingCount > reviewAlerts.length && (
                    <button className="bell-item bell-item--review" onClick={goReview}>
                      <span className="bell-item-meta">
                        +{pendingCount - reviewAlerts.length} more — open queue →
                      </span>
                    </button>
                  )}
                </>
              )}
              {pendingCount > 0 && reviewAlerts.length === 0 && (
                <button className="bell-item bell-item--review" onClick={goReview}>
                  <span className="bell-item-title">
                    {pendingCount} {pendingCount === 1 ? 'item needs' : 'items need'} review
                  </span>
                  <span className="bell-item-meta">Open the review queue →</span>
                </button>
              )}

              {errors.length > 0 && (
                <>
                  <div className="bell-section-label">Recent errors</div>
                  {errors.map((err) => (
                    <button
                      key={err.id}
                      className="bell-item bell-item--error"
                      title={err.subject || '(no subject)'}
                      onClick={openError}
                    >
                      <span className="bell-item-title">{err.subject || '(no subject)'}</span>
                      <span className="bell-item-meta">{err.message || err.status}</span>
                    </button>
                  ))}
                </>
              )}
            </div>
          )}
        </div>

        <div className="navbar-user">
          <div className="navbar-avatar">{userName.charAt(0).toUpperCase()}</div>
          <div className="navbar-user-text">
            <div className="navbar-user-name">{userName}</div>
            <div className="navbar-user-email">{userEmail}</div>
          </div>
        </div>
      </div>
    </header>
  )
}
