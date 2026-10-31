import { useCallback, useEffect, useState } from 'react'
import { get, post } from '../api'
import { fmtClock } from './relay'

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
// 抖音百应私信的草稿（PRM_BUYIN_SYNC_MIN）：机器人发不了，运营复制以后自己到百应里发。
/** The reply with the full numbers (text: as edited; undefined = as drafted); every copy is written down. */
export const copyBuyin = (id, text) => decide(id, 'copy', text)
/** The operator sent it in 百应 by hand (text: what went out, when it was edited). */
export const buyinSent = (id, text) => decide(id, 'manual_sent', text)
export const ackPlatform = (id) => decide(id, 'ack')
export const partyAction = (body) => post('/api/prm/parties', body)
export const dealAction = (body) => post('/api/prm/deals', body)
/** 自动模式 (POST /api/prm/auto): the 下班时段 rules at any hour, until switched off. */
export const setAutoMode = (on) => post('/api/prm/auto', { on })
/** The full numbers behind a creator's placeholders; every look is written down (8.2). */
export const revealNumbers = (partyId) => post('/api/prm/reveal', { party_id: partyId })

// 加百应达人微信（PRM_FRIEND_ADD，src/prm/friend_adds.py）：一次确认 = 一次好友申请，号码只显示打码的，显示完整 / 复制都记一笔。
/** POST /api/prm/friend-adds {action, id?, ...}: paste / add / edit / sent_by_phone / sent_already / not_sent / wait_her / drop / link / not_her / link_on_accept / reveal / remark_done. */
export const friendAddAction = (body) => post('/api/prm/friend-adds', body)
export const FRIEND_ADD_TAGS = {
  offered: 'cy', needs_view: 'warn', waiting: 'pend', adding: 'pend', requested: 'ok', not_found: 'warn', known: 'ok',
  failed: 'warn', unsure: 'warn', blocked: 'need', linked: 'ok', dropped: 'off', expired: 'off',
}
/** "今天控制台已加 3/10 · 上一次 14:02 · 2 分钟内不能再加". */
export function friendAddStatusLine(st) {
  if (!st || !st.enabled) return ''
  const parts = [`今天控制台已加 ${st.today ?? 0}/${st.cap ?? 10}`]
  if (st.last_at) parts.push(`上一次 ${fmtClock(st.last_at)}`)
  if (st.wait_sec > 0) parts.push(st.wait_sec < 120 ? `${st.wait_sec} 秒内不能再加` : `约 ${Math.round(st.wait_sec / 60)} 分钟内不能再加`)
  return parts.join(' · ')
}
/** "备注：抖音-小鹿（已填进好友申请）": the remark (备注) a 加微信 record gives her in our WeChat, and whether the add typed
 *  it into the application window ('' without a remark). */
export function remarkLine(r) {
  if (!r?.remark) return ''
  let note = ''
  if (r.remark_set) note = r.state === 'linked' ? '' : '（已填进好友申请）'
  else if (r.last_attempt && r.state === 'requested') note = '（申请窗口里没认出「备注」栏，这次没填上）'
  return `备注：${r.remark}${note}`
}
/** A 百应 draft's line of the 加微信 record made from its conversation: "她在这个会话里留了联系方式（微信号 ab****yz）→
 *  「加微信 #12」：已发申请，等她通过（见上面）" ('' without one). */
export function buyinFriendAddLine(fa) {
  if (!fa) return ''
  const m = fa.masked || {}
  const said = [m.wechat ? `微信号 ${m.wechat}` : '', m.phone ? `手机 ${m.phone}` : ''].filter(Boolean).join('、')
  return `她在这个会话里留了联系方式${said ? `（${said}）` : ''}→「加微信 #${fa.id}」：${fa.label}${fa.open ? '（见上面）' : ''}`
}
/** "10-29 11:00 发的邀约卡（小达人，4 款，卡上没开专属佣金）" ('' without an invite). */
export function inviteLine(inv) {
  if (!inv || !(inv.invited_at || inv.plan_id)) return ''
  const at = typeof inv.invited_at === 'number' ? fmtClock(inv.invited_at) : String(inv.invited_at || '').slice(5, 16).replace('T', ' ')
  const bits = [inv.category_label, inv.products ? `${inv.products} 款` : '',
    inv.commission_rate > 0 ? `卡上开了 ${inv.commission_rate}% 专属佣金` : (inv.commission_rate === 0 ? '卡上没开专属佣金' : '')].filter(Boolean)
  return `${at ? `${at} ` : ''}发的邀约卡${bits.length ? `（${bits.join('，')}）` : ''}${inv.plan_id ? ` · 清单 ${inv.plan_id}` : ''}`
}

// 老板拉的群（PRM_BOSS_GROUPS，src/prm/boss_groups.py）：老板（PRM_GROUP_WITH）把他微信里的达人和我们这个号拉进一个小群，之后由我们
// 跟她对接。群里除了老板和我们只有一个人时机器人自己接手；不止一个时「合作单」顶上问哪个是她。只有她在群里说的话起草，和私聊的草稿一个
// 规矩（先审、门槛、下班时段、自动模式）发在群里，老板也看得到。
/** POST /api/prm/boss-groups/pick {chat, wxid}: she is this member of the pending group: the bot takes it over. */
export const pickBossGroup = (chat, wxid) => post('/api/prm/boss-groups/pick', { chat, wxid })
/** POST /api/prm/boss-groups/ignore {chat}: not a creator's group: never offered again, and stays silent like any group. */
export const ignoreBossGroup = (chat) => post('/api/prm/boss-groups/ignore', { chat })
/** A draft that goes out in a boss group (``channel`` "group" of a packet; any other value, or none, is her private chat). */
export const inGroup = (p) => p?.channel === 'group'
/** A creator the boss introduced in a group he made with us (``source`` "boss_group" of the party or of her deal). */
export const bossIntroduced = (p) => p?.source === 'boss_group' || p?.deal?.source === 'boss_group'
/** The 「老板拉的群」 card's line of counts: "已接手 2 个群；1 个群在等她进群（…）；1 个群要你选哪个是她" (one sentence that
 *  says so when nothing was found yet). */
export function bossGroupsLine(bg) {
  const linked = bg?.linked || 0
  const waiting = bg?.waiting || 0
  const pending = (bg?.pending || []).length
  if (!linked && !waiting && !pending) return '还没有发现老板新拉的群。'
  return [`已接手 ${linked} 个群`,
    waiting ? `${waiting} 个群在等她进群（群里现在只有老板和我们，她进来以后机器人自己接手）` : '',
    pending ? `${pending} 个群要你选哪个是她` : ''].filter(Boolean).join('；')
}

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
    case 'buyin_message': return `${ev.name}：${d.text || ''}`
    case 'buyin_copy': case 'buyin_sent': return `${ev.name}：#${d.packet}${d.edited ? '（改过）' : ''}`
    case 'buyin_draft_failed': return `${ev.name}：#${d.job} ${d.why || ''}`
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

/** How many WeChat messages a reply goes out as (the bot's ``reply_parts``, src/prm/service.py): its paragraphs (a blank
 * line between two), at most ``most`` (``reply_parts`` of GET /api/prm; the ones past it join the last one). */
export function replyPartCount(text, most) {
  const parts = String(text || '').replace(/\r\n/g, '\n').split(/\n[ \t　]*\n/).filter((p) => p.trim())
  return Math.min(parts.length, Math.max(1, most || 1))
}

/** One creator of the 百应 line below: her nickname, and when the conversation is our invite (the Mac's invite ledger), her 抖音号 and when. */
function buyinWho(u) {
  const inv = u?.invited
  if (!inv) return u?.name || ''
  const when = String(inv.invited_at || '').slice(5, 16).replace('T', ' ')
  return `${u.name || '（没有昵称）'}（我们邀约的 · 抖音号 ${inv.aweme_id}${when ? ` · ${when} 邀约` : ''}）`
}

/** What the 待审批 page says of the 百应 private messages: why they cannot be read, who wrote that is not on the list
 *  (our invites marked; how many of them left a contact: the 加微信 cards below), how many of a colleague's invites
 *  answered (only counted: not ours to handle), how many contact cards came in conversations that are not ours. */
export function buyinNotes(d) {
  const b = d?.buyin
  if (!b || !d?.buyin_sync_min) return []
  const out = []
  if (b.status?.text) out.push(['warn', b.status.text])
  if (b.unmatched_count > 0) {
    const names = (b.unmatched || []).map(buyinWho).filter(Boolean)
    const left = b.contacts_left > 0 ? `；其中 ${b.contacts_left} 位留了联系方式，见下面的「加微信」` : ''
    out.push(['', `百应里有 ${b.unmatched_count} 位没登记的达人发了消息，到百应里看${names.length ? `：${names.join('、')}` : ''}（登记了抖音号的达人才会起草）${left}`])
  }
  if (b.others_invited > 0) out.push(['', `另有 ${b.others_invited} 位是店里同事邀约的（卡片不是我们这边发的），她们的回复不归我们管`])
  if (b.others_contacts > 0) out.push(['', `别人邀约的会话里有 ${b.others_contacts} 张联系方式卡片，不归我们处理`])
  return out
}

/** What the 待审批 page says of 老板拉的群: the groups where a person has to say which member she is ([] = none, or off). */
export function bossGroupNotes(d) {
  const bg = d?.boss_groups
  const n = (bg?.enabled && bg.pending?.length) || 0
  return n ? [['warn', `老板拉了 ${n} 个新群，群里不止一个人可能是她：到「合作单」页顶上的「老板拉的群」里选哪个是她，选了机器人才接手。`]] : []
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
