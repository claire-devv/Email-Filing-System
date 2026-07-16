import { useState, useEffect } from 'react'
import { tokenUtils } from '../../utils/tokenUtils'
import { getGoogleStatus, getDriveRootStatus, getGmailWatchStatus } from '../../api/client'
import { useActivityStats, useActivity, useReviewItems } from '../../hooks/useRresData'

import Sidebar from '../../components/sidebar.jsx'
import Navbar from '../../components/navbar.jsx'
import { ToastProvider, ToastContainer, useToasts } from '../../components/Toasts'

import './dashboard.css'

import ReviewSection from './sections/ReviewSection'
import ActivitySection, { ActivityTable } from './sections/ActivitySection'
import DocumentsSection from './sections/DocumentsSection'
import EntitiesSection from './sections/EntitiesSection'
import SettingsSection from './sections/AdminPanel'

const TAB_TITLES = {
  home: 'Home',
  review: 'Needs Review',
  activity: 'Activity',
  documents: 'Documents',
  entities: 'Entities',
  settings: 'Settings',
}

const ERROR_STATUSES = new Set(['failed', 'waiting_api_limit'])

export default function Dashboard() {
  const [activeTab, setActiveTab] = useState('home')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [reviewFocusId, setReviewFocusId] = useState(null)
  const [docEntityFilter, setDocEntityFilter] = useState('')
  const userEmail = tokenUtils.getUserEmail() || 'admin'

  // Single polling query drives the badge, status dot, and errors list.
  // refetchInterval adapts: 10 s while processing, 30 s idle.
  const { data: stats } = useActivityStats()
  const { data: recentActivity } = useActivity(0)
  const { data: reviewItemsData } = useReviewItems('pending', 0)

  const pendingCount = stats?.needs_review_pending ?? 0
  const systemStatus = stats
    ? {
        processing_count: stats.processing_count ?? 0,
        watch_active: stats.watch_active ?? false,
        watch_error: stats.watch_error ?? null,
        last_sync_ago_minutes: stats.last_sync_ago_minutes ?? null,
      }
    : null
  const errors = (Array.isArray(recentActivity) ? recentActivity : [])
    .filter((a) => ERROR_STATUSES.has(a.status))
    .slice(0, 5)
  const reviewAlerts = Array.isArray(reviewItemsData) ? reviewItemsData.slice(0, 5) : []

  function goTab(tab) {
    setReviewFocusId(null)
    setDocEntityFilter('')
    setActiveTab(tab)
  }

  function openReviewItem(id) {
    setReviewFocusId(id)
    setActiveTab('review')
  }

  function openDocumentsFor(entityName) {
    setDocEntityFilter(entityName || '')
    setActiveTab('documents')
  }

  const handleLogout = () => {
    tokenUtils.clearToken()
    window.location.reload()
  }

  return (
    <ToastProvider>
      <GoogleConnectNotice />
      <div className="dashboard-layout">
        <Sidebar
          activeTab={activeTab}
          setActiveTab={goTab}
          userEmail={userEmail}
          onLogout={handleLogout}
          sidebarOpen={sidebarOpen}
          setSidebarOpen={setSidebarOpen}
          pendingCount={pendingCount}
        />

        <div className="dashboard-main">
          <Navbar
            title={TAB_TITLES[activeTab]}
            userName={userEmail.split('@')[0]}
            userEmail={userEmail}
            pendingCount={pendingCount}
            errors={errors}
            reviewAlerts={reviewAlerts}
            systemStatus={systemStatus}
            onMenuClick={() => setSidebarOpen(!sidebarOpen)}
            onGoToReview={() => goTab('review')}
            onOpenReviewItem={openReviewItem}
            onOpenError={() => goTab('activity')}
          />

          <IntegrationStatusBanner onNavigate={goTab} />

          <main className="dashboard-content">
            {activeTab === 'home' && (
              <HomeSection onNavigate={goTab} onOpenReviewItem={openReviewItem} />
            )}
            {activeTab === 'review' && (
              <ReviewSection focusId={reviewFocusId} />
            )}
            {activeTab === 'activity' && <ActivitySection />}
            {activeTab === 'documents' && <DocumentsSection initialEntity={docEntityFilter} />}
            {activeTab === 'entities' && <EntitiesSection onOpenDocuments={openDocumentsFor} />}
            {activeTab === 'settings' && <SettingsSection />}
          </main>
        </div>
      </div>
      <ToastContainer />
    </ToastProvider>
  )
}

// After the Google OAuth callback redirects back here, surface the result as a toast
// and strip the query params so a page refresh doesn't repeat it.
function GoogleConnectNotice() {
  const { addToast, updateToast } = useToasts()

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const connected = params.get('connected')
    const errorParam = params.get('error')
    if (connected === null && errorParam === null) return

    if (connected === 'true') {
      const email = params.get('email')
      const id = addToast(email ? `Google connected: ${email}` : 'Google account connected')
      updateToast(id, { status: 'done', text: email ? `Google connected: ${email}` : 'Google account connected' })
    } else {
      const message = params.get('message') || errorParam || 'Google connection failed'
      const id = addToast(message)
      updateToast(id, { status: 'error', text: 'Google connection failed', detail: message })
    }

    const url = new URL(window.location.href)
    url.searchParams.delete('connected')
    url.searchParams.delete('email')
    url.searchParams.delete('error')
    url.searchParams.delete('message')
    window.history.replaceState({}, '', url.pathname + url.search + url.hash)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return null
}

// Surfaces broken integrations (Google account, Drive, Gmail Pub/Sub) globally —
// mounts once so it doesn't re-fire 3 API calls on every tab switch.
function IntegrationStatusBanner({ onNavigate }) {
  const [issues, setIssues] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    ;(async () => {
      try {
        const [google, driveRoot, watch] = await Promise.all([
          getGoogleStatus(),
          getDriveRootStatus(),
          getGmailWatchStatus(),
        ])
        const next = []
        if (!google?.connected) {
          next.push(
            google?.requires_reconnect
              ? 'Google account needs to be reconnected — email filing and Drive access are paused.'
              : 'Google account is not connected — connect it to enable email filing.',
          )
        }
        if (!driveRoot?.configured) {
          next.push('Google Drive filing folder is not configured.')
        } else if (!driveRoot?.accessible) {
          next.push(
            `Google Drive folder is not accessible${driveRoot?.access_error ? `: ${driveRoot.access_error}` : '.'}`,
          )
        }
        if (!watch?.active) {
          next.push('Gmail Pub/Sub watch is not active — incoming emails will not be processed automatically.')
        } else if (watch?.last_error) {
          next.push(`Gmail Pub/Sub watch error: ${watch.last_error}`)
        }
        setIssues(next)
      } catch (err) {
        setIssues([err.message || 'Could not check integration status.'])
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  if (loading || issues.length === 0) return null

  return (
    <div className="integration-banner">
      {issues.map((msg, i) => (
        <div key={i} className="section-error">
          <span>{msg}</span>
          <button className="btn btn-secondary btn-sm" onClick={() => onNavigate('settings')}>
            Open Settings
          </button>
        </div>
      ))}
    </div>
  )
}

// HomeSection reads from the same TanStack Query cache as the Dashboard-level hooks —
// zero extra network calls when switching to the Home tab.
function HomeSection({ onNavigate }) {
  const [expandedId, setExpandedId] = useState(null)

  const { data: statsData, isLoading: statsLoading, error: statsError } = useActivityStats()
  const { data: activityData, isLoading: actLoading } = useActivity(0)

  const loading = (statsLoading && !statsData) || (actLoading && !activityData)
  const stats = statsData || {}
  const recent = (Array.isArray(activityData) ? activityData : []).slice(0, 8)

  if (loading) return <div className="section-loading">Loading overview…</div>
  if (statsError && !statsData) return <div className="section-error">{statsError.message || 'Failed to load overview'}</div>

  const cards = [
    { key: 'today', label: "Today's emails", value: stats?.processed_today || 0 },
    { key: 'filed', label: 'Filed successfully', value: stats?.filed_today || 0 },
    { key: 'review', label: 'Needs review', value: stats?.needs_review_pending || 0, tab: 'review' },
    { key: 'errors', label: 'Errors today', value: stats?.errors_today || 0, tab: 'activity' },
  ]

  return (
    <div className="overview">
      <div className="stats-grid">
        {cards.map((c) => {
          const Tag = c.tab ? 'button' : 'div'
          return (
            <Tag
              key={c.key}
              type={c.tab ? 'button' : undefined}
              className={`stat-card stat-card--${c.key}`}
              onClick={c.tab ? () => onNavigate(c.tab) : undefined}
            >
              <span className="stat-label">{c.label}</span>
              <span className="stat-value">{c.value}</span>
            </Tag>
          )
        })}
      </div>

      <section className="dashboard-section">
        <header className="section-header">
          <div>
            <h2 className="section-title">Recent activity</h2>
            <p className="section-desc">The latest emails processed by the assistant.</p>
          </div>
          <div className="section-actions">
            <button className="btn btn-secondary btn-sm" onClick={() => onNavigate('activity')}>
              View all
            </button>
          </div>
        </header>

        <div className="section-content">
          {recent.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-text">No activity yet</div>
              <div className="empty-state-hint">Processed emails will appear here.</div>
            </div>
          ) : (
            <ActivityTable
              rows={recent}
              expandedId={expandedId}
              onToggle={(id) => setExpandedId((cur) => (cur === id ? null : id))}
            />
          )}
        </div>
      </section>
    </div>
  )
}
