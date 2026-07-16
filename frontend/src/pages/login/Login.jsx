import { useState } from 'react'
import { login } from '../../api/client'
import { tokenUtils } from '../../utils/tokenUtils'
import rresLogo from '../../../assets/logo03.png'
import './Login.css'


const Login = ({ onLoginSuccess }) => {
  const [showPassword, setShowPassword] = useState(false)
  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!email || !password) {
      setError('Enter your email and password.')
      return
    }
    try {
      setLoading(true)
      setError('')
      const { access_token, token_type, expires_at, is_admin } = await login(email, password)
      tokenUtils.setToken(access_token, token_type, expires_at)
      tokenUtils.setUserEmail(email)
      tokenUtils.setIsAdmin(is_admin)
      if (onLoginSuccess) onLoginSuccess()
    } catch (err) {
      setError(err.message || 'Sign in failed. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">

      {/* ── Left brand panel ── */}
      <div className="login-left">
        <img className="login-left-logo" src={rresLogo} alt="RRES" />
      </div>

      {/* ── Right form panel ── */}
      <div className="login-right">
        <div className="login-card">
          <h1 className="login-title">Sign in</h1>
          <p className="login-subtitle">Access the email filing dashboard.</p>

          <form onSubmit={handleSubmit}>
            <label className="field">
              <span>Email</span>
              <input
                className="form-control"
                type="email"
                placeholder="you@company.com"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>

            <label className="field">
              <span>Password</span>
              <div className="password-wrapper">
                <input
                  className="form-control"
                  type={showPassword ? 'text' : 'password'}
                  placeholder="Your password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  className="show-btn"
                  onClick={() => setShowPassword(!showPassword)}
                >
                  {showPassword ? 'Hide' : 'Show'}
                </button>
              </div>
            </label>

            {error && <div className="section-error login-error">{error}</div>}

            <button type="submit" className="btn btn-primary login-btn" disabled={loading}>
              {loading ? 'Signing in…' : 'Sign in'}
            </button>
          </form>

          <p className="login-footnote">
            Emails are classified automatically; anything uncertain waits for your review.
          </p>
        </div>
      </div>

    </div>
  )
}

export default Login
