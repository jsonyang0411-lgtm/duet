import { useCallback, useEffect, useState } from 'react'
import { get, post } from '../api'

// GET /api/prm: the PRM copilot's creators, the drafts waiting for a person and
// the recent ones (src/web/server.py _prm_state). Polled while a page shows it.

export const PACKET_STATES = {
  pending: ['待审批', 'cy'],
  approved: ['发送中', 'pend'],
  sending: ['发送中', 'pend'],
  sent: ['已发出', 'ok'],
  failed: ['没发出去', 'need'],
  dropped: ['不发', 'off'],
  superseded: ['已过时', 'off'],
  no_reply: ['不用回复', 'off'],
}

export function usePrm(intervalMs = 5000) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(async () => {
    try { setData(await get('/api/prm')); setErr('') } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => {
    load()
    const t = setInterval(load, intervalMs)
    return () => clearInterval(t)
  }, [load, intervalMs])
  return { data, err, reload: load }
}

export const decide = (id, action, text) => post('/api/prm/packets', text === undefined ? { id, action } : { id, action, text })
export const partyAction = (body) => post('/api/prm/parties', body)

/** What is wrong with the setup, in the order a person has to fix it ([] = ready). */
export function setupProblems(d) {
  if (!d) return []
  const out = []
  if (!d.enabled) out.push(['need', '达人副驾驶没打开：在「合作单」页下面的设置里打开，再重启机器人。'])
  if (d.enabled && !d.running) out.push(['warn', '机器人没在运行：达人的消息收不到，草稿也发不出去。'])
  if (d.running && !(d.worker_kinds || []).includes('prm')) {
    out.push(['warn', 'Mac 还没表示能写草稿：在 Mac 的 config.json 里配好 prm_workspace（里面要有 kb/），重启 worker。在这之前达人的消息会一直排队。'])
  }
  return out
}

export function fmtSize(n) {
  if (!n && n !== 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
