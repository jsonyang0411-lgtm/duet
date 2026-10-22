import { useCallback, useEffect, useState } from 'react'
import { get } from '../api'

// GET /api/relay: the Claude relay's state and newest jobs (src/web/server.py
// _relay_snapshot). Polled while a page that shows it is open.

export const JOB_STATES = {
  pending: ['排队中', 'pend'],
  running: ['进行中', 'cy'],
  done: ['完成', 'ok'],
  failed: ['失败', 'need'],
  cancelled: ['已取消', 'off'],
  lost: ['失联', 'warn'],
}

export function useRelay(intervalMs = 5000) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(async () => {
    try { setData(await get('/api/relay')); setErr('') } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => {
    load()
    const t = setInterval(load, intervalMs)
    return () => clearInterval(t)
  }, [load, intervalMs])
  return { data, err, reload: load }
}

export function fmtClock(ts) {
  if (!ts) return '--:--'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  const t = `${p(d.getHours())}:${p(d.getMinutes())}`
  return d.toDateString() === new Date().toDateString() ? t : `${d.getMonth() + 1}/${d.getDate()} ${t}`
}

export function fmtSeconds(s) {
  if (s === null || s === undefined || s === '') return ''
  const n = Math.round(Number(s))
  if (n < 60) return `${n} 秒`
  if (n < 3600) return `${Math.floor(n / 60)} 分 ${n % 60} 秒`
  return `${Math.floor(n / 3600)} 小时 ${Math.floor((n % 3600) / 60)} 分`
}

/** "在线" / "不在线（3 分前联系过）" / "还没连上过" */
export function macState(d) {
  if (!d) return ['—', undefined]
  if (d.mac_online) return ['在线', true]
  if (d.mac_last_seen_sec !== null && d.mac_last_seen_sec !== undefined) return [`不在线（${fmtSeconds(d.mac_last_seen_sec)}前联系过）`, false]
  return [d.running && d.listening ? '还没连上过' : '—', d.running && d.listening ? false : undefined]
}
