import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

// Fetching the WeChat database key (POST /api/onboarding/step1, then poll
// /api/onboarding/step1-status). The server walks through
// hooking → waiting_exit → waiting_login → hooking_restart → done | timeout | error
// and writes the key to the machine .env itself when it succeeds.

export const PHASE_TEXT = {
  starting: '正在启动',
  idle: '正在准备',
  hooking: '正在安装钩子',
  waiting_exit: '请退出微信',
  waiting_login: '请重新登录微信',
  hooking_restart: '正在读取密钥',
  done: '密钥获取成功',
  timeout: '获取超时',
  error: '获取失败',
}

export function useKeyExtraction() {
  const [phase, setPhase] = useState('') // '' = not started
  const [message, setMessage] = useState('')
  const [result, setResult] = useState(null)
  const timer = useRef(null)
  const failures = useRef(0)
  const alive = useRef(true)

  const stopPolling = () => { clearTimeout(timer.current); timer.current = null }
  useEffect(() => { alive.current = true; return () => { alive.current = false; stopPolling() } }, [])

  const poll = useCallback(async () => {
    try {
      const s = await api('/api/onboarding/step1-status')
      if (!alive.current) return
      failures.current = 0
      if (s.phase) setPhase(s.phase)
      setMessage(s.message || '')
      if (s.phase === 'done') { setResult(s.result || {}); return }
      if (s.phase === 'timeout' || s.phase === 'error') return
      if (!s.running) { setPhase('error'); setMessage('获取已经停止了，请重试'); return }
    } catch {
      if (!alive.current) return
      failures.current += 1
      if (failures.current > 30) { setPhase('error'); setMessage('和控制台服务失去联系，稍后再试'); return }
    }
    if (alive.current) timer.current = setTimeout(poll, 1000)
  }, [])

  const start = useCallback(async () => {
    stopPolling()
    setResult(null)
    setPhase('starting')
    setMessage('')
    try {
      const r = await api('/api/onboarding/step1', { method: 'POST' })
      if (!r.ok && r.phase !== 'busy') throw new Error(r.message || r.error || '启动失败')
    } catch (e) {
      setPhase('error')
      setMessage(e.message)
      return
    }
    timer.current = setTimeout(poll, 600)
  }, [poll])

  const running = !!phase && !['done', 'timeout', 'error'].includes(phase)
  return { phase, message, result, running, start, label: PHASE_TEXT[phase] || phase }
}

export async function diagnose() {
  const d = await api('/api/onboarding/diagnose')
  return d.diagnostics || {}
}
