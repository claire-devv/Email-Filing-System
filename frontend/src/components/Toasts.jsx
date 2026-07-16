import { createContext, useCallback, useContext, useRef, useState } from 'react'
import './toasts.css'

const ToastContext = createContext(null)

let nextId = 1

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const timers = useRef({})

  const removeToast = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
    if (timers.current[id]) {
      clearTimeout(timers.current[id])
      delete timers.current[id]
    }
  }, [])

  const addToast = useCallback((text) => {
    const id = nextId++
    setToasts((prev) => [...prev, { id, text, status: 'pending' }])
    return id
  }, [])

  const updateToast = useCallback(
    (id, patch) => {
      setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)))
      if (patch.status === 'done') {
        timers.current[id] = setTimeout(() => removeToast(id), 3000)
      }
    },
    [removeToast],
  )

  return (
    <ToastContext.Provider value={{ toasts, addToast, updateToast, removeToast }}>
      {children}
    </ToastContext.Provider>
  )
}

export function useToasts() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToasts must be used within a ToastProvider')
  return ctx
}

export function ToastContainer() {
  const { toasts, removeToast } = useToasts()
  if (toasts.length === 0) return null

  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast--${t.status}`}>
          <span className="toast-icon" aria-hidden="true">
            {t.status === 'pending' && <span className="toast-spinner" />}
            {t.status === 'done' && '✓'}
            {t.status === 'error' && '⚠'}
          </span>
          <div className="toast-body">
            <div className="toast-text">{t.text}</div>
            {t.detail && <div className="toast-detail">{t.detail}</div>}
          </div>
          {t.status !== 'pending' && (
            <button type="button" className="toast-close" aria-label="Dismiss" onClick={() => removeToast(t.id)}>
              ✕
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
