import { useEffect, useState } from 'react'
import { get } from '../api'

// Group list from the message database (/api/nicknames/groups), shared by the
// pages that need a group picker. Cached for a minute.
let cache = null
let cacheAt = 0
let inflight = null

export async function loadGroups(force = false) {
  if (!force && cache && Date.now() - cacheAt < 60000) return cache
  if (!inflight) {
    inflight = get('/api/nicknames/groups')
      .then((d) => { cache = d.groups || []; cacheAt = Date.now(); return cache })
      .finally(() => { inflight = null })
  }
  return inflight
}

export function useGroups() {
  const [state, setState] = useState({ list: cache, error: '' })
  useEffect(() => {
    let alive = true
    loadGroups().then(
      (list) => alive && setState({ list, error: '' }),
      (e) => alive && setState({ list: [], error: e.message }),
    )
    return () => { alive = false }
  }, [])
  return state
}
