import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { api, post } from '../api'

// Bot status, polled from /api/status every 2 s. Until the first answer the
// console says "连接中" instead of pretending everything is fine.

const StatusContext = createContext(null)
const POLL_MS = 2000

export function StatusProvider({ children, enabled = true }) {
  const [conn, setConn] = useState('connecting') // connecting | online | offline
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState('') // '' | 'start' | 'stop'
  const [actionError, setActionError] = useState('')
  const timer = useRef(null)

  const poll = useCallback(async () => {
    try {
      const d = await api('/api/status')
      setData(d)
      setConn('online')
    } catch {
      setConn('offline')
    }
  }, [])

  useEffect(() => {
    if (!enabled) return undefined
    let alive = true
    const tick = async () => {
      await poll()
      if (alive) timer.current = setTimeout(tick, POLL_MS)
    }
    tick()
    return () => { alive = false; clearTimeout(timer.current) }
  }, [enabled, poll])

  const run = useCallback(async (which) => {
    setBusy(which)
    setActionError('')
    try {
      await post(which === 'start' ? '/api/start' : '/api/stop')
    } catch (e) {
      setActionError(e.message)
    } finally {
      await poll()
      setBusy('')
    }
  }, [poll])

  // Stop, wait until the bot thread has really exited, then start again.
  const restart = useCallback(async () => {
    setBusy('stop')
    setActionError('')
    try {
      await post('/api/stop')
      const deadline = Date.now() + 20000
      for (;;) {
        const d = await api('/api/status')
        setData(d)
        if (!d.running) break
        if (Date.now() > deadline) throw new Error('机器人 20 秒内没有停下来，请稍后在运行状态页手动启动')
        await new Promise((r) => setTimeout(r, 500))
      }
      setBusy('start')
      await post('/api/start')
    } catch (e) {
      setActionError(e.message)
      throw e
    } finally {
      await poll()
      setBusy('')
    }
  }, [poll])

  const value = useMemo(() => ({
    conn,
    data,
    running: !!data?.running,
    busy,
    actionError,
    clearActionError: () => setActionError(''),
    start: () => run('start'),
    stop: () => run('stop'),
    restart,
    refresh: poll,
  }), [conn, data, busy, actionError, run, restart, poll])

  return <StatusContext.Provider value={value}>{children}</StatusContext.Provider>
}

export function useStatus() {
  const ctx = useContext(StatusContext)
  if (!ctx) throw new Error('useStatus outside StatusProvider')
  return ctx
}

export function fmtDuration(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d) return `${d} 天 ${h} 小时`
  if (h) return `${h} 小时 ${m} 分`
  if (m) return `${m} 分钟`
  return `${s} 秒`
}

export const WECHAT_BACKEND_NAMES = {
  wcdb: 'WCDB 直读',
  mac_hybrid: 'macOS WeFlow',
  mac_ui: 'macOS 界面',
}
