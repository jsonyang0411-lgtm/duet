import { useEffect, useState } from 'react'

// Hash routes: #/status, #/relay, #/wechat … so a reload stays put.
export function parseHash(hash = window.location.hash) {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  return { page: parts[0] || 'status', sub: parts[1] || '', rest: parts.slice(2) }
}

export function navigate(path) {
  const next = `#/${path.replace(/^\/+/, '')}`
  if (window.location.hash !== next) window.location.hash = next
}

export function useRoute() {
  const [route, setRoute] = useState(() => parseHash())
  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return route
}
