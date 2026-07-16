import './sidebar.css'
import rresLogo from '../../assets/logo03.png'
import {
  FaHouse,
  FaClipboardCheck,
  FaClockRotateLeft,
  FaFolderOpen,
  FaBuilding,
  FaGear,
  FaRightFromBracket,
} from 'react-icons/fa6'

const MENU_ITEMS = [
  { id: 'home', label: 'Home', icon: <FaHouse /> },
  { id: 'review', label: 'Needs Review', icon: <FaClipboardCheck /> },
  { id: 'activity', label: 'Activity', icon: <FaClockRotateLeft /> },
  { id: 'documents', label: 'Documents', icon: <FaFolderOpen /> },
  { id: 'entities', label: 'Entities', icon: <FaBuilding /> },
  { id: 'settings', label: 'Settings', icon: <FaGear /> },
]

export default function Sidebar({
  activeTab,
  setActiveTab,
  userEmail,
  onLogout,
  sidebarOpen,
  setSidebarOpen,
  pendingCount = 0,
}) {
  const handleMenuClick = (tabId) => {
    setActiveTab(tabId)
    setSidebarOpen(false)
  }

  return (
    <>
      {sidebarOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />}

      <aside className={`sidebar${sidebarOpen ? ' sidebar-open' : ''}`}>
        <div className="sidebar-brand">
          <img className="sidebar-logo" src={rresLogo} alt="RRES" />
          <div className="sidebar-brand-sub">  Email automation</div>
        </div>

        <nav className="sidebar-nav" aria-label="Dashboard sections">
          {MENU_ITEMS.map((item) => (
            <button
              key={item.id}
              className={`sidebar-item${activeTab === item.id ? ' active' : ''}`}
              onClick={() => handleMenuClick(item.id)}
            >
              <span className="sidebar-icon">{item.icon}</span>
              <span>{item.label}</span>
              {item.id === 'review' && pendingCount > 0 && (
                <span className="sidebar-badge">{pendingCount}</span>
              )}
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-user">
            <div className="sidebar-avatar">{userEmail?.charAt(0)?.toUpperCase() || 'U'}</div>
            <div className="sidebar-user-text">
              <div className="sidebar-user-name">{userEmail?.split('@')[0] || 'User'}</div>
              <div className="sidebar-user-email">{userEmail}</div>
            </div>
          </div>

          <button className="sidebar-logout" onClick={onLogout}>
            <FaRightFromBracket />
            <span>Sign out</span>
          </button>
        </div>
      </aside>
    </>
  )
}
