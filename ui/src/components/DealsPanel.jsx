import { useEffect, useRef, useState } from 'react'
import {
  ArrowClockwise, UserPlus, Prohibit, ArrowCounterClockwise, CaretDown, CaretRight, Eye, EyeSlash, Truck, Check, HandWaving,
} from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import { usePrm, partyAction, dealAction, revealNumbers, setupProblems, eventText, MODE_TAGS, ACTORS } from '../lib/prm'
import { PageHead, Card, Note, Empty, Field, TextInput, Select, Seg } from './ui'
import { AdvancedFields } from './AdvancedPanel'
import { FriendAddPasteCard } from './FriendAddCard'

// 合作单: the creators whose private messages get drafted replies (PRM_COPILOT).
// Registering one here means the creator agreed to be contacted on WeChat; their
// messages then go, silently, to the Mac for a draft that waits in 待审批. A deal
// opens (below the list, so it fits any width) into its stage, its mode (copilot /
// a person has it / paused), the sample shipment, the numbers and the audit trail.
// 自动模式 on (switched on 待审批): one warn line at the top says so.

export default function DealsPanel() {
  const { data, err, reload } = usePrm(6000)
  const [open, setOpen] = useState(null)
  const current = (data?.parties || []).find((p) => p.id === open && p.deal?.id)
  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="04" en="CREATORS" title="合作单"
          sub="登记达人的 wxid：他们的私聊不再自动回复，由 Mac 起草，运营在「待审批」里决定发不发"
          actions={<button type="button" className="btn sm" onClick={reload}><ArrowClockwise />刷新</button>}
        />
        {err && <Note kind="need">{err}</Note>}
        {setupProblems(data).map(([kind, text]) => <Note key={text} kind={kind}>{text}</Note>)}
        {data?.enabled && data?.auto_mode?.on && (
          <div className="prm-auto-note" style={{ marginBottom: 12 }}>
            <Note kind="warn">自动模式开着（{fmtClock(data.auto_mode.since)} 打开）：满足安全条件的草稿不经审批直接发给达人；在「待审批」页顶上关掉。</Note>
          </div>
        )}
        <RegisterCard d={data} onDone={reload} />
        <FriendAddPasteCard d={data} onDone={reload} />
        <PartiesCard d={data} open={open} setOpen={setOpen} onDone={reload} />
        {current && <DealCard key={current.id} p={current} d={data} onClose={() => setOpen(null)} onDone={reload} />}
        <div className="stack" style={{ gap: 12, marginTop: 16 }}>
          <AdvancedFields only={['prm']} onSaved={reload} />
        </div>
      </div>
    </div>
  )
}

function RegisterCard({ d, onDone }) {
  const toast = useToast()
  const [wxid, setWxid] = useState('')
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const [douyin, setDouyin] = useState('')
  const [busy, setBusy] = useState(false)
  const withDouyin = !!d?.friend_add_status?.enabled          // 加百应达人微信 on: who she is on 抖音 (optional)
  // New friends first (src/wechat/new_friends.py: tagged, their greeting masked, how they came), then
  // the other recent private chats. Registering is a person's decision (a new friend may be a supplier
  // or an acquaintance), unless PRM_AUTO_REGISTER is on: then the bot registers every new friend itself
  // (tagged 自动登记 in the list below) and only those it left (a stopped one, an error) are offered here.
  const candidates = d?.candidates || []
  const fresh = candidates.filter((c) => c.new)
  const picked = candidates.find((c) => c.wxid === wxid.trim())
  const hours = d?.catchup_hours ?? 12
  const pick = (value) => {
    const c = candidates.find((x) => x.wxid === value)
    if (c) {
      setWxid(c.wxid); setName((n) => n || c.name)
      if (withDouyin && c.friend_add?.aweme_id) setDouyin(c.friend_add.aweme_id)   // 加微信: her 抖音号 comes along
    }
  }
  const add = async () => {
    setBusy(true)
    try {
      const r = await partyAction({ action: 'add', wxid: wxid.trim(), name: name.trim(), note: note.trim(),
        ...(withDouyin && douyin.trim() ? { douyin_id: douyin.trim() } : {}) })
      toast.ok(r?.catch_up
        ? `已登记 ${name.trim()}：加好友以来对方发的消息会合成一份草稿（还没说话就写一份开场白），在「待审批」里等你看过再发`
        : `已登记 ${name.trim()}`)
      setWxid(''); setName(''); setNote(''); setDouyin('')
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  return (
    <Card title="登记达人" eyebrow="REGISTER" className="prm-register">
      <div className="stack" style={{ gap: 10 }}>
        {d?.enabled && d?.auto_register && (
          <Note kind="warn">
            新好友自动登记已打开（PRM_AUTO_REGISTER）：机器人认出的每个新加的好友都会自动登记成达人（下面的名单里标「自动登记」），
            加好友以来的消息合成一份草稿（还没说话的写一份开场白），在「待审批」里等人看过再发；之后的草稿和别的达人一样走自动发送门槛和下班时段自动回复。
            客户、快递、同事这些不是达人的人也会被登记：在下面点「停用」去掉，停用的不会再被自动登记。管理员和能用 cc 的人从不登记。
          </Note>
        )}
        {fresh.length > 0 && (
          <div className="stack prm-new-friends" style={{ gap: 8 }}>
            <b>新加的好友</b>
            {fresh.map((c) => (
              <div key={c.wxid} className="row prm-new-friend" style={{ gap: 8, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                <span className="tag cy">新好友</span>
                <div className="grow" style={{ minWidth: 0 }}>
                  <div>
                    <b>{c.name}</b> <span className="mono dim">{c.wxid}</span>
                    {c.inactive && <span className="tag off" style={{ marginLeft: 6 }} title="登记过、又停用了：再登记就是重新启用">已停用</span>}
                    {c.friend_add && <span className="tag warn" style={{ marginLeft: 6 }}>{`${c.friend_add.level_label || '可能是她'}：百应的${c.friend_add.nickname || '达人'}（#${c.friend_add.id}）`}</span>}
                  </div>
                  <div className="dim">{c.how_label}{c.added_at ? ` · ${fmtClock(c.added_at)} 加上的` : ''}</div>
                  <div className={c.verify_text ? '' : 'dim'} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                    验证消息：{c.verify_text || '（没有）'}
                  </div>
                </div>
                <button type="button" className="btn sm" aria-label={`选 ${c.name}`} onClick={() => pick(c.wxid)}>选这个</button>
              </div>
            ))}
          </div>
        )}
        {candidates.length > 0 && (
          <Field label="从最近的私聊里选" hint="新加的好友排在最前面（标着「新好友」）；其余是机器人号最近有私聊的人（达人副驾驶打开、机器人运行后才有这个列表）">
            <Select value="" onChange={pick} aria-label="从最近的私聊里选"
              options={[{ value: '', label: '选一个…' }, ...candidates.map((c) => ({ value: c.wxid, label: `${c.new ? '新好友 · ' : ''}${c.name}（${c.wxid}）` }))]} />
          </Field>
        )}
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <Field label="wxid" hint="对方的微信内部号，形如 wxid_xxx" className="grow">
            <TextInput mono value={wxid} onChange={setWxid} placeholder="wxid_xxx" aria-label="达人 wxid" />
          </Field>
          <Field label="称呼" hint="运营怎么称呼这位达人（最多 30 字）" className="grow">
            <TextInput value={name} onChange={setName} placeholder="比如：小美" aria-label="称呼" />
          </Field>
          <Field label="备注（可选）" className="grow">
            <TextInput value={note} onChange={setNote} placeholder="比如：618 防晒" aria-label="备注" />
          </Field>
          {withDouyin && (
            <Field label="抖音号（可选）" hint="知道她的抖音号就填：Mac 起草时按它带上邀约背景" className="grow">
              <TextInput mono value={douyin} onChange={setDouyin} placeholder="dy_xxx" aria-label="达人抖音号（可选）" />
            </Field>
          )}
        </div>
        {picked?.new && (
          <Note kind="warn">
            {`${picked.name} 是新加的好友（${picked.how_label || '说不清是怎么加的'}）。登记后，加好友以来对方发的消息（验证消息也算，`
              + `${hours > 0 ? `最多 ${hours} 小时内的` : '只算最近 5 分钟的'}）会合成一份草稿，在「待审批」里等人看过再发；`
              + '对方还没说过话（比如刚通过我们的好友申请），就写一份开场白，也是等人看过再发。不是达人（比如供应商、熟人）就不要登记。'}
          </Note>
        )}
        <Note>登记表示这位达人已同意由我们在微信上联系。登记后，对方的私聊不会再收到自动回复：机器人不回任何话，只在「待审批」里给运营一份草稿。</Note>
        <div><button type="button" className="btn primary" disabled={busy || !wxid.trim() || !name.trim()} onClick={add}><UserPlus />登记</button></div>
      </div>
    </Card>
  )
}

function PartiesCard({ d, open, setOpen, onDone }) {
  const toast = useToast()
  const parties = d?.parties || []
  const toggle = async (p) => {
    try {
      await partyAction({ action: p.active ? 'disable' : 'enable', id: p.id })
      toast.ok(p.active ? `${p.name} 已停用：之后的消息不再起草` : `${p.name} 已恢复`)
      onDone()
    } catch (e) { toast.err(e.message) }
  }
  return (
    <Card title="已登记的达人" eyebrow="DEALS" className="prm-parties" sub={parties.length ? `${parties.length} 位` : undefined}>
      {!d && <Empty title="正在读取…" />}
      {d && !parties.length && <Empty title="还没有登记达人">在上面填 wxid 和称呼，点「登记」。</Empty>}
      {parties.length > 0 && (
        <div className="scroll" style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead><tr><th style={{ width: 28 }} /><th>达人</th><th>阶段</th><th>模式</th><th>已确认的信息</th><th>要注意</th><th>最近消息</th><th /></tr></thead>
            <tbody>
              {parties.map((p) => {
                const deal = p.deal || {}
                const slots = Object.entries(deal.slots || {})
                const expanded = open === p.id
                return (
                  <tr key={p.id} className={p.active ? '' : 'dim'}>
                    <td>
                      <button type="button" className="icon-btn sm" aria-expanded={expanded ? 'true' : 'false'}
                        aria-label={expanded ? `收起 ${p.name}` : `展开 ${p.name}`} onClick={() => setOpen(expanded ? null : p.id)}>
                        {expanded ? <CaretDown /> : <CaretRight />}
                      </button>
                    </td>
                    <td>
                      <b>{p.name}</b>
                      {p.auto && <span className="tag warn" style={{ marginLeft: 6 }} title="新加的好友，机器人自动登记的（PRM_AUTO_REGISTER）；不是达人的点「停用」">自动登记</span>}
                      <div className="mono dim">{p.wxid}</div>{p.note && <div className="dim">{p.note}</div>}{!p.active && <span className="tag off">已停用</span>}
                    </td>
                    <td>
                      {deal.stage_label || '—'}
                      {deal.stage_proposal && deal.stage_proposal !== deal.stage && <div className="dim">AI 建议：{deal.stage_proposal_label}</div>}
                    </td>
                    <td>
                      {deal.mode && <span className={`tag ${MODE_TAGS[deal.mode] || ''}`}>{deal.mode_label}</span>}
                      {deal.mode_until && <div className="dim">到 {fmtClock(deal.mode_until)}</div>}
                    </td>
                    <td className="ellipsis" style={{ maxWidth: 260 }} title={slots.map(([k, v]) => `${k}：${v}`).join('\n')}>
                      {slots.length ? slots.map(([k, v]) => `${k}：${v}`).join('；') : '—'}
                    </td>
                    <td>{deal.attention ? <span className="f-err">{deal.attention}</span> : '—'}</td>
                    <td className="num">{deal.last_message_at ? fmtClock(deal.last_message_at) : '—'}</td>
                    <td>
                      <button type="button" className="btn sm ghost" onClick={() => toggle(p)}>
                        {p.active ? <><Prohibit />停用</> : <><ArrowCounterClockwise />恢复</>}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

/** The opened deal, as its own card under the list (shown where the click was, at any width). */
function DealCard({ p, d, onClose, onDone }) {
  const ref = useRef(null)
  useEffect(() => { ref.current?.scrollIntoView?.({ block: 'nearest' }) }, [])
  return (
    <div ref={ref} style={{ marginTop: 16 }}>
      <Card title={`${p.name} 的合作单`} eyebrow={`DEAL #${p.deal.id}`} className="prm-deal-card"
        sub={`${p.deal.stage_label || '—'} · ${p.deal.mode_label || ''}`}
        actions={<button type="button" className="btn sm ghost" onClick={onClose}><CaretDown />收起</button>}>
        <DealDetail p={p} d={d} onDone={onDone} />
      </Card>
    </div>
  )
}

function DealDetail({ p, d, onDone }) {
  const toast = useToast()
  const deal = p.deal
  const stages = d?.stages || {}
  const modes = d?.modes || {}
  const shipmentStates = d?.shipment_states || {}
  const [stage, setStage] = useState(deal.stage)
  const [carrier, setCarrier] = useState('')
  const [waybill, setWaybill] = useState('')
  const [status, setStatus] = useState(deal.shipment?.status || 'transit')
  const [numbers, setNumbers] = useState(null)
  const [busy, setBusy] = useState(false)
  // A change made elsewhere (AI suggestion adopted, a waybill, another console) shows up here.
  useEffect(() => setStage(deal.stage), [deal.stage])
  useEffect(() => setStatus(deal.shipment?.status || 'transit'), [deal.shipment?.status])
  const run = async (body, done) => {
    setBusy(true)
    try {
      await dealAction({ deal_id: deal.id, ...body })
      toast.ok(done)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  const runParty = async (body, done) => {
    setBusy(true)
    try {
      await partyAction(body)
      toast.ok(done)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  const reveal = async () => {
    try {
      const r = await revealNumbers(p.id)
      setNumbers(r.numbers || [])
    } catch (e) { toast.err(e.message) }
  }
  return (
    <div className="stack prm-deal" style={{ gap: 14 }}>
      <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <Field label="阶段" className="grow">
          <Select value={stage} onChange={setStage} aria-label={`${p.name} 的阶段`}
            options={Object.entries(stages).map(([k, v]) => ({ value: k, label: v }))} />
        </Field>
        <button type="button" className="btn" disabled={busy || stage === deal.stage} onClick={() => run({ action: 'stage', stage }, `已改成「${stages[stage]}」`)}><Check />改阶段</button>
        {deal.stage_proposal && deal.stage_proposal !== deal.stage && (
          <button type="button" className="btn primary" disabled={busy} onClick={() => run({ action: 'adopt' }, `已采纳：${deal.stage_proposal_label}`)}>采纳 AI 建议：{deal.stage_proposal_label}</button>
        )}
      </div>

      <div className="stack" style={{ gap: 6 }}>
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <b>谁来回复</b>
          <Seg small value={deal.mode} label={`${p.name} 的模式`}
            onChange={(mode) => run({ action: 'mode', mode }, `已切换：${modes[mode]}`)}
            options={Object.entries(modes).map(([k, v]) => ({ value: k, label: v }))} />
          {deal.mode !== 'copilot' && (
            <button type="button" className="btn sm primary" disabled={busy} onClick={() => run({ action: 'mode', mode: 'copilot' }, '已恢复副驾驶')}>恢复副驾驶</button>
          )}
        </div>
        <div className="f-hint">
          {deal.mode === 'human' && `人工接管中${deal.mode_until ? `（到 ${fmtClock(deal.mode_until)} 自动恢复）` : ''}：达人的消息只记录、不起草，恢复后下一份草稿会带上这段时间双方说过的话。`}
          {deal.mode === 'paused' && '暂停中：达人的消息只记录、不起草，直到点「恢复副驾驶」。'}
          {deal.mode === 'copilot' && `副驾驶：Mac 起草，运营审批。你用这个微信号亲手回了达人，会自动切到人工接管 ${d?.takeover_min || 120} 分钟。`}
        </div>
      </div>

      {!deal.last_message_at && (
        <div className="stack prm-opening" style={{ gap: 6 }}>
          <b>开场白</b>
          <div className="f-hint">
            她还没跟我们说过话。点「写开场白」，Mac 按 kb 里的破冰写法写一句先打招呼的话，在「待审批」里等你看过再发。
            新加的好友通过以后一直没说话，系统会自己写一份，不用点。
          </div>
          <div>
            <button type="button" className="btn" disabled={busy || !p.active || deal.mode !== 'copilot'}
              onClick={() => run({ action: 'opening' }, '已请 Mac 写开场白，写好会出现在「待审批」里')}>
              <HandWaving />写开场白
            </button>
            {(!p.active || deal.mode !== 'copilot') && <span className="dim" style={{ marginLeft: 8 }}>{!p.active ? '已停用的达人不写' : '先恢复副驾驶'}</span>}
          </div>
        </div>
      )}

      <div className="stack" style={{ gap: 6 }}>
        <b>寄样</b>
        {deal.shipment
          ? <div><Truck style={{ verticalAlign: -2 }} /> {deal.shipment.carrier} <span className="mono">{deal.shipment.waybill}</span> · {deal.shipment_status_label}</div>
          : <div className="dim">还没有运单号。</div>}
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <Field label="快递公司"><TextInput value={carrier} onChange={setCarrier} placeholder="顺丰" aria-label={`${p.name} 的快递公司`} /></Field>
          <Field label="运单号" className="grow"><TextInput mono value={waybill} onChange={setWaybill} placeholder="SF1234567890123" aria-label={`${p.name} 的运单号`} /></Field>
          <button type="button" className="btn primary" disabled={busy || !carrier.trim() || !waybill.trim()}
            onClick={() => run({ action: 'shipment', carrier: carrier.trim(), waybill: waybill.trim() }, '已记下运单号，发货通知草稿在「待审批」里')}>
            填运单号并起草发货通知
          </button>
        </div>
        {deal.shipment && (
          <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <Field label="物流状态">
              <Select value={status} onChange={setStatus} aria-label={`${p.name} 的物流状态`}
                options={Object.entries(shipmentStates).map(([k, v]) => ({ value: k, label: v }))} />
            </Field>
            <button type="button" className="btn" disabled={busy} onClick={() => run({ action: 'shipment_status', status }, `物流已更新：${shipmentStates[status]}`)}>更新物流</button>
          </div>
        )}
        {(deal.purged_at || deal.purge_due_at) && (
          <div className="f-hint">
            {deal.purged_at && `收件信息已在 ${fmtClock(deal.purged_at)} 按规定清除（地址只留省市，号码只留打码的）。`}
            {deal.purge_due_at && `${deal.delivered_at && !deal.purged_at ? `${fmtClock(deal.delivered_at)} 签收；` : ''}`
              + `${fmtClock(deal.purge_due_at)} 后自动清除收件信息（地址、手机号、证件号）；签收满 90 天或 90 天没有动静就清。`}
          </div>
        )}
      </div>

      {deal.links?.length > 0 && (
        <div className="stack prm-links" style={{ gap: 6 }}>
          <b>抖音号</b>
          {deal.links.map((l) => (
            <div key={l.account} className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <span>{l.source === 'friend_add' ? '来自百应邀约' : '登记时填的'}：抖音号 <span className="mono">{l.account}</span>
                {l.add_id ? `（加微信 #${l.add_id}）` : ''}{l.invited_at ? `，${String(l.invited_at).slice(5, 16).replace('T', ' ')} 发卡` : ''}</span>
              <button type="button" className="btn sm ghost" disabled={busy}
                onClick={() => runParty({ action: 'unlink', id: p.id, account: l.account }, `已取消关联 ${l.account}`)}>取消关联</button>
            </div>
          ))}
        </div>
      )}

      {(p.has_numbers || deal.legend?.length > 0) && (
        <div className="stack" style={{ gap: 6 }}>
          <b>号码</b>
          {deal.legend?.length > 0 && <div className="f-hint">{deal.legend.map((l) => `${l.token} = ${l.masked}`).join('；')}</div>}
          {numbers
            ? (
              <div className="row" style={{ gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                {numbers.length ? numbers.map((n) => <span key={n.token} className="tag mono">{n.token} {n.cleared ? '（已清除）' : n.value}</span>) : '没有号码'}
                <button type="button" className="btn sm ghost" onClick={() => setNumbers(null)}><EyeSlash />隐藏</button>
              </div>
            )
            : <div><button type="button" className="btn sm" onClick={reveal}><Eye />显示完整号码</button> <span className="dim">每次查看都会记下来</span></div>}
        </div>
      )}

      {deal.events?.length > 0 && (
        <div className="stack" style={{ gap: 4 }}>
          <b>操作记录</b>
          {deal.events.map((ev) => (
            <div key={ev.id} className="prm-event">
              <span className="num dim">{fmtClock(ev.at)}</span> <span className="dim">{ACTORS[ev.actor] || ev.actor}</span> {eventText(ev, stages, modes, shipmentStates)}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
