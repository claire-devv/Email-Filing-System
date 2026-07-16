import { useState, useEffect } from 'react'
import { tokenUtils } from './utils/tokenUtils'
import Login from './pages/login/Login.jsx'
import Dashboard from './pages/dashboard/dashboard.jsx'
import './App.css'

function App() {
  const [isLoggedIn, setIsLoggedIn] = useState(tokenUtils.hasToken())
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // Check if user is logged in on mount
    const checkAuth = () => {
      setIsLoggedIn(tokenUtils.hasToken())
      setLoading(false)
    }

    checkAuth()

    // Listen for storage changes (logout from another tab)
    window.addEventListener('storage', checkAuth)
    return () => window.removeEventListener('storage', checkAuth)
  }, [])

  // Update login state when login page succeeds
  const handleLoginSuccess = () => {
    setIsLoggedIn(true)
  }

  if (loading) return <div className="app-loading">Loading...</div>

  return isLoggedIn ? <Dashboard /> : <Login onLoginSuccess={handleLoginSuccess} />
}

export default App
