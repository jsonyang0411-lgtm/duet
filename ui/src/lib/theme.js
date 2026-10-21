import { useCallback, useEffect, useState } from 'react'

// Light is the default; the choice is kept in localStorage['theme'] and applied
// as the `dark` class on <html> (index.html applies it before the first paint).
const KEY = 'theme'

function read() {
  try { return localStorage.getItem(KEY) === 'dark' ? 'dark' : 'light' } catch { return 'light' }
}

export function useTheme() {
  const [theme, setTheme] = useState(read)
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    try { localStorage.setItem(KEY, theme) } catch { /* private mode */ }
  }, [theme])
  useEffect(() => {
    const onStorage = (e) => { if (e.key === KEY) setTheme(read()) }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])
  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), [])
  return { theme, toggle }
}
