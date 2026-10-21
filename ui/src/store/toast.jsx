import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import { CheckCircle, WarningCircle } from '@phosphor-icons/react'

const ToastContext = createContext(null)
let nextId = 1

export function ToastProvider({ children }) {
  const [items, setItems] = useState([])

  const dismiss = useCallback((id) => setItems((xs) => xs.filter((x) => x.id !== id)), [])

  const toast = useCallback((text, { kind = 'ok', ms } = {}) => {
    const id = nextId++
    setItems((xs) => [...xs.slice(-3), { id, text, kind }])
    setTimeout(() => dismiss(id), ms ?? (kind === 'err' ? 7000 : 3500))
  }, [dismiss])

  const value = useMemo(() => ({
    ok: (t, o) => toast(t, { ...o, kind: 'ok' }),
    err: (t, o) => toast(t, { ...o, kind: 'err' }),
  }), [toast])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind === 'err' ? 'err' : ''}`} onClick={() => dismiss(t.id)}>
            {t.kind === 'err' ? <WarningCircle weight="bold" /> : <CheckCircle weight="bold" />}
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast outside ToastProvider')
  return ctx
}
