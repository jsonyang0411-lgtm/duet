import { useCallback, useEffect, useState } from 'react'
import { get, post } from '../api'

// GET /api/prm: the PRM copilot's creators, the drafts waiting for a person and
// the recent ones (src/web/server.py _prm_state). Polled while a page shows it.

export const PACKET_STATES = {
  pending: ['待审批', 'cy'],
  executing: ['平台执行中', 'pend'],
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

// Platform actions (src/prm/platforms.py): the Mac's preview verdict and an execution's outcome.
export const VERDICT_TAGS = {
  ready: 'ok', blocked: 'need', invalid: 'need', unavailable: 'off', login: 'need', wall: 'need',
  wrong_account: 'need', error: 'need',
}
export const outcomeTag = (outcome) => (outcome === 'VERIFIED_SUCCESS' ? 'ok' : 'need')
/** 批准并执行 (text: the reply as edited; undefined = as drafted). */
export const executePlatform = (id, text) => decide(id, 'execute', text)
export const repreview = (id) => decide(id, 'repreview')
export const ackPlatform = (id) => decide(id, 'ack')
export const partyAction = (body) => post('/api/prm/parties', body)
export const dealAction = (body) => post('/api/prm/deals', body)
/** 自动模式 (POST /api/prm/auto): the 下班时段 rules at any hour, until switched off. */
export const setAutoMode = (on) => post('/api/prm/auto', { on })
/** The full numbers behind a creator's placeholders; every look is written down (8.2). */
export const revealNumbers = (partyId) => post('/api/prm/reveal', { party_id: partyId })

export const MODE_TAGS = { copilot: 'cy', human: 'warn', paused: 'off' }
/** Who did it (deal_events.actor). */
export const ACTORS = { console: '控制台', system: '系统', operator: '运营（微信里）', creator: '达人', mac: 'Mac' }

/** One audit line: "改阶段 意向沟通 → 已寄出" etc. */
export function eventText(ev, stages = {}, modes = {}, shipmentStates = {}) {
  const d = ev.detail || {}
  switch (ev.kind) {
    case 'stage': return `${ev.name}：${stages[d.from] || d.from || '—'} → ${stages[d.to] || d.to}`
    case 'mode': return `${ev.name}：${modes[d.from] || d.from || '—'} → ${modes[d.to] || d.to}`
    case 'shipment': return `${ev.name}：${d.carrier} ${d.waybill}`
    case 'shipment_status': return `${ev.name}：${d.waybill} ${shipmentStates[d.status] || d.status}`
    case 'operator_message': case 'creator_message': case 'holding_reply': return `${ev.name}：${d.text || ''}`
    case 'reveal': return `${ev.name}：${(d.tokens || []).join('、') || '没有号码'}`
    case 'wrong': return `${ev.name}：#${d.packet}`
    case 'purge': return `${ev.name}：${Object.entries(d).map(([k, v]) => `${k} ${v}`).join('，')}`
    case 'purge_case': return `${ev.name}：${d.ok ? '完成' : '没成功'} ${d.result || ''}`
    case 'platform_execute': return `${ev.name}：#${d.packet} ${d.action || ''}（Mac 任务 #${d.job}）`
    case 'platform_result': return `${ev.name}：#${d.packet} ${d.outcome || ''}${d.link ? ` ${d.link}` : ''}`
    case 'platform_preview': return `${ev.name}：#${d.packet}`
    case 'platform_ack': return `${ev.name}：#${d.packet}`
    default: return ev.name
  }
}

/** 下班时段自动回复 (``night`` of GET /api/prm): [note kind, the status line]; ``auto``: 自动模式 (the switch at the
 * top of 待审批) is what puts its rules in force now (no window, or now outside it). */
export function nightStatus(n) {
  if (n.error) return ['need', `下班时段自动回复：${n.error}`]
  const auto = n.auto ? '自动模式开着，照下班时段的规则自动发' : ''
  if (!n.on && !auto) return ['', '下班时段自动回复：关着']
  const head = `下班时段自动回复：${n.on ? n.window : '关着'}`
  const now = [n.on && (n.inside ? '现在是下班时段' : '现在是上班时段'), auto].filter(Boolean).join('；')
  return n.suspended ? ['warn', `${head}（${now}；暂停，不自动发）`] : [auto ? 'warn' : 'cy', `${head}（${now}）`]
}

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
