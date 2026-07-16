// Bearer-token storage for the dashboard session. Network calls live in src/api/client.js.
export const tokenUtils = {
  setToken: (token, tokenType = 'bearer', expiresAt = null) => {
    localStorage.setItem('access_token', token)
    localStorage.setItem('token_type', tokenType)
    if (expiresAt) {
      localStorage.setItem('expires_at', expiresAt)
    } else {
      localStorage.removeItem('expires_at')
    }
  },

  getToken: () => localStorage.getItem('access_token'),

  setUserEmail: (email) => localStorage.setItem('user_email', email),

  getUserEmail: () => localStorage.getItem('user_email') || '',

  setIsAdmin: (isAdmin) => localStorage.setItem('is_admin', isAdmin ? '1' : '0'),

  getIsAdmin: () => localStorage.getItem('is_admin') === '1',

  getTokenType: () => localStorage.getItem('token_type') || 'bearer',

  getExpiresAt: () => localStorage.getItem('expires_at'),

  hasToken: () => !!localStorage.getItem('access_token'),

  isTokenExpired: () => {
    const expiresAt = localStorage.getItem('expires_at')
    if (!expiresAt) return false
    return new Date(expiresAt) < new Date()
  },

  clearToken: () => {
    localStorage.removeItem('access_token')
    localStorage.removeItem('token_type')
    localStorage.removeItem('expires_at')
    localStorage.removeItem('user_email')
    localStorage.removeItem('is_admin')
  },

  getAuthHeader: () => {
    const token = localStorage.getItem('access_token')
    const tokenType = localStorage.getItem('token_type') || 'bearer'
    return token ? `${tokenType} ${token}` : null
  },
}
