import { useEffect, useRef, useState } from 'react'
import {
  ArrowClockwise, UserPlus, Prohibit, ArrowCounterClockwise, CaretDown, CaretRight, Eye, EyeSlash, Truck, Check, HandWaving,
  UsersThree, Copy, PencilSimple, Package, IdentificationCard,
} from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import {
  usePrm, partyAction, dealAction, revealNumbers, setupProblems, eventText, MODE_TAGS, ACTORS, DRAFT_NOTE_MAX, draftNoteText,
  saveDraftNote, bossIntroduced,
} from '../lib/prm'
import { PageHead, Card, Note, Empty, Field, TextInput, TextArea, Select, Seg, Confirm } from './ui'
import { AdvancedFields } from './AdvancedPanel'
import { FriendAddPasteCard } from './FriendAddCard'
import { BossGroupsCard } from './BossGroupsCard'

// 合作单: the creators whose private messages get drafted replies (PRM_COPILOT).
// Registering one here means the creator agreed to be contacted on WeChat; their
// messages then go, silently, to the Mac for a draft that waits in 待审批. A deal
// opens (below the list, so it fits any width) into its stage, its mode (copilot /
// a person has it / paused), its 起草备注 for the Mac, the sample shipment, the numbers
// and the audit trail.
// 自动模式 on (switched on 待审批): one warn line at the top says so.
// 老板拉的群 (PRM_BOSS_GROUPS): the card at the top (BossGroupsCard.jsx); a creator taken over from such a group is
// tagged 老板介绍 in the list, and her deal's 三方群 block says 老板拉的群 (``three_party.by`` "boss").

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
        <BossGroupsCard d={data} onDone={reload} />
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
                      {bossIntroduced(p) && <span className="tag cy" style={{ marginLeft: 6 }} title="老板拉群介绍给我们的达人（老板拉的群，PRM_BOSS_GROUPS）：她在那个群里说的话起草，审过发到群里">老板介绍</span>}
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

      <DraftNoteBlock p={p} onDone={onDone} />

      {(!deal.last_message_at || deal.unanswered) && (
        <div className="stack prm-opening" style={{ gap: 6 }}>
          <b>开场白</b>
          {deal.last_message_at ? (
            <div className="f-hint">
              她说过话（比如加好友时的「我是…」），可我们还没正式回过她：她那份草稿没发，最多只有机器人自动回的「稍等」。
              点「写开场白」，Mac 按 kb 写我们正式的第一句（和别的开场白一样直接发货盘图），在「待审批」里等你看过再发。
            </div>
          ) : (
            <div className="f-hint">
              她还没跟我们说过话。点「写开场白」，Mac 按 kb 里的破冰写法写一句先打招呼的话，在「待审批」里等你看过再发。
              新加的好友通过以后一直没说话，系统会自己写一份，不用点；加好友过了很久才登记的，系统不知道她之前说没说过，不自己写，会留一条提示请你看一眼微信。
            </div>
          )}
          <div>
            <button type="button" className="btn" disabled={busy || !p.active || deal.mode !== 'copilot'}
              onClick={() => run({ action: 'opening' }, '已请 Mac 写开场白，写好会出现在「待审批」里')}>
              <HandWaving />写开场白
            </button>
            {(!p.active || deal.mode !== 'copilot') && <span className="dim" style={{ marginLeft: 8 }}>{!p.active ? '已停用的达人不写' : '先恢复副驾驶'}</span>}
          </div>
        </div>
      )}

      {/* 补发货盘 (the deal action huopan, 2026-11-04): send her the 货盘 again and ask which styles she likes */}
      <div className="stack prm-huopan" style={{ gap: 6 }}>
        <b>补发货盘</b>
        <div className="f-hint">
          聊过以后一直没下文、想再推一次款：点「补发货盘」，Mac 按 kb 写一两句，附上货盘图（对方是商务、助理时再加 Excel），问她看中哪几款，在「待审批」里等你看过再发（不会自动发）。
          7 天内发过货盘的不再发，Mac 会说哪天发过。
        </div>
        <div>
          <button type="button" className="btn" disabled={busy || !p.active || deal.mode !== 'copilot'}
            onClick={() => run({ action: 'huopan' }, '已请 Mac 写补发货盘的草稿，写好会出现在「待审批」里')}>
            <Package />补发货盘
          </button>
          {(!p.active || deal.mode !== 'copilot') && <span className="dim" style={{ marginLeft: 8 }}>{!p.active ? '已停用的达人不发' : '先恢复副驾驶'}</span>}
        </div>
      </div>

      {/* 直播手卡 (the deal action handcards, PRM_HANDCARDS, 2026-11-09): the Mac makes her cards, a person looks first */}
      {d?.handcards && <HandcardsBlock p={p} busy={busy} run={run} />}

      {d?.three_party?.enabled && <ThreePartyBlock p={p} d={d} busy={busy} run={run} />}

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

/** 起草备注 (the deal action draft_note, 2026-10-31): what the operators tell the Mac about this creator, say her WeChat
 *  signature's 「直接发链接和 brief、不接语音」. Every draft of hers follows it, the 开场白 too; unlike her 备注 (which the
 *  bot writes for a friend it registered) only an operator writes it. Saved empty, it is cleared; an error stays here. */
function DraftNoteBlock({ p, onDone }) {
  const toast = useToast()
  const saved = p.deal.draft_note || ''
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(saved)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (!editing) setText(saved) }, [saved, editing])
  const length = draftNoteText(text).length
  const hint = '写给 Mac 起草时看的，比如她签名里的要求：直接发链接和 brief、不接语音'
  const cancel = () => { setEditing(false); setText(saved); setError('') }
  const save = async () => {
    if (length > DRAFT_NOTE_MAX) { setError(`起草备注最多 ${DRAFT_NOTE_MAX} 个字（现在 ${length} 个）`); return }
    setBusy(true)
    try {
      await saveDraftNote(p.deal.id, text)
      toast.ok(length ? '起草备注已保存：之后她的草稿都照它写' : '起草备注已清掉')
      setEditing(false)
      setError('')
      onDone()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="stack prm-draft-note" style={{ gap: 6 }}>
      <b>起草备注</b>
      {editing ? (
        <>
          <Field hint={`${hint}。空着保存就是清掉。`} error={error}>
            <TextArea value={text} rows={3} aria-label={`${p.name} 的起草备注`} aria-invalid={error ? 'true' : undefined}
              placeholder="比如：直接发链接和 brief，感兴趣的会回复；勿催视频，不接语音"
              onChange={(value) => { setText(value); setError('') }} />
          </Field>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <button type="button" className="btn primary" disabled={busy} onClick={save}><Check />保存</button>
            <button type="button" className="btn" disabled={busy} onClick={cancel}>取消</button>
            <span className={length > DRAFT_NOTE_MAX ? 'f-err' : 'dim'}>{length}/{DRAFT_NOTE_MAX} 字</span>
          </div>
        </>
      ) : (
        <>
          <div className={saved ? '' : 'dim'} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{saved || '（没有）'}</div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <button type="button" className="btn sm" onClick={() => setEditing(true)}><PencilSimple />{saved ? '改备注' : '写备注'}</button>
            <span className="f-hint">{hint}</span>
          </div>
        </>
      )}
    </div>
  )
}

/** 三方群 (src/prm/three_party.py, 2026-10-30): 「拉三方群」 makes the group in this PC's WeChat (her and PRM_GROUP_WITH,
 *  picked in 「发起群聊」 and read back before 「完成」; nothing is said in it and it is not renamed: the suggested name is
 *  there to copy); 「先试一下」 picks them and cancels.  A run that may or may not have made it asks 「建好了 / 没建成」.
 *  A group the boss made with her and us (``by`` "boss", 老板拉的群, PRM_BOSS_GROUPS) is hers already: it says so and what
 *  the bot does there (her lines drafted, sent in the group), without the name to copy (the boss named it). */
// 做手卡 (PRM_HANDCARDS): the products of her latest 抖店 sample order the bot made are ticked; other 抖店 product ids may be
// typed; none at all: the Mac takes the ones she picked from her case file (or makes none and says why).
function HandcardsBlock({ p, busy, run }) {
  const deal = p.deal
  const order = deal.card_items
  const fromOrder = order?.shop === 'dy' ? order.items : []
  const [ticked, setTicked] = useState(() => fromOrder.map((i) => i.item_id))
  const [extra, setExtra] = useState('')
  const items = [...new Set([...ticked, ...extra.split(/[\s,，、;；]+/).filter(Boolean)])]
  const off = !p.active || deal.mode !== 'copilot'
  const tick = (id, on) => setTicked((t) => (on ? [...t, id] : t.filter((x) => x !== id)))
  return (
    <div className="stack prm-handcards" style={{ gap: 6 }}>
      <b>直播手卡</b>
      <div className="f-hint">
        她要上播、要手卡：点「做手卡」，Mac 照老板模板把勾上的款做成图片版手卡（一款一张，价格、尺码、颜色按商品目录，库存写「充足」），
        附在草稿里，在「待审批」里等你看过再发（不会自动发）。什么都不勾、不填，Mac 按案卷里她选的款做。
      </div>
      {order && order.shop !== 'dy' && <div className="dim">她最近的寄样单是小红书店的：手卡只做抖店的款，在下面填抖店的商品 ID。</div>}
      {fromOrder.length > 0 && (
        <div className="stack" style={{ gap: 4 }}>
          <span className="dim">她最近的寄样单{order.order ? ` ${order.order}` : ''}（{fmtClock(order.at)}）里的款：</span>
          {fromOrder.map((i) => (
            <label key={i.item_id} className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input type="checkbox" checked={ticked.includes(i.item_id)} aria-label={`做手卡：商品 ${i.item_id}`}
                onChange={(e) => tick(i.item_id, e.target.checked)} />
              <span className="mono">{i.item_id}</span>{i.skus > 0 && <span className="dim">（寄了 {i.skus} 个 SKU）</span>}
            </label>
          ))}
        </div>
      )}
      <Field label="其他商品 ID（抖店，空格隔开）">
        <TextInput mono value={extra} onChange={setExtra} placeholder="3843021700182704552" aria-label={`${p.name} 做手卡的其他商品 ID`} />
      </Field>
      <div>
        <button type="button" className="btn" disabled={busy || off}
          onClick={() => run({ action: 'handcards', items }, items.length
            ? `已请 Mac 做 ${items.length} 款手卡，做好会出现在「待审批」里`
            : '已请 Mac 按案卷里她选的款做手卡，做好会出现在「待审批」里')}>
          <IdentificationCard />做手卡
        </button>
        {off && <span className="dim" style={{ marginLeft: 8 }}>{!p.active ? '已停用的达人不做' : '先恢复副驾驶'}</span>}
      </div>
    </div>
  )
}

function ThreePartyBlock({ p, d, busy, run }) {
  const toast = useToast()
  const [asking, setAsking] = useState(false)
  const tp = d.three_party || {}
  const g = p.deal.three_party || {}
  const names = (tp.with || []).join('、')
  const suggested = String(tp.name_template || '{昵称}').replace('{昵称}', p.name)
  const working = tp.busy === p.deal.id
  const making = g.state === 'creating'
  const why = tp.why || (!p.active ? '已停用的达人不拉群' : '') || (tp.busy && !working ? '另一个群正在建，等它好了再点' : '')
  const copyName = async () => {
    try {
      await navigator.clipboard.writeText(suggested)
      toast.ok(`群名已复制：在手机微信里打开这个群 › 群聊名称，粘贴「${suggested}」`)
    } catch { toast.err(`没能放进剪贴板：群名是「${suggested}」`) }
  }
  const tried = g.tried
  const gone = (
    <button type="button" className="btn sm ghost" disabled={busy}
      onClick={() => run({ action: 'group_none' }, '记下了：这位达人现在没有三方群')}>群已经不在了</button>
  )
  return (
    <div className="stack prm-group" style={{ gap: 6 }}>
      <b>三方群</b>
      {g.state === 'created' && g.by === 'boss' && (
        <>
          <div><UsersThree style={{ verticalAlign: -2 }} /> 老板拉的群（{fmtClock(g.at)} 发现）{g.chat_name ? `：微信里叫「${g.chat_name}」` : ''}</div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <span className="f-hint">老板把她和我们拉进了这个群，之后由我们跟她对接：她在群里说的话照常起草，回复发在群里，老板也看得到；老板和别人说的话不起草。群里的话也会交给她私聊的草稿。</span>
            {gone}
          </div>
        </>
      )}
      {g.state === 'created' && g.by !== 'boss' && (
        <>
          <div><UsersThree style={{ verticalAlign: -2 }} /> 三方群已建好（{fmtClock(g.at)}{g.made ? '' : '，手动标的'}）{g.chat_name ? `：微信里叫「${g.chat_name}」` : ''}</div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <span className="f-hint">建议在手机微信里把群名改成「{suggested}」（机器人不改群名，也不在群里说话）。</span>
            <button type="button" className="btn sm" onClick={copyName}><Copy />复制群名</button>
            {gone}
          </div>
          {g.reading
            ? <div className="f-hint prm-group-reading">机器人在读这个群（只读，不在群里说话）：群里谁说了什么，会交给她私聊的草稿，群里谈定的以群里为准。</div>
            : <div className="f-hint prm-group-reading">{g.made ? '这台电脑读不了群' : '机器人不知道这是哪个群（手动标的）'}，读不到群里的话：群里谈定了什么，写进这张合作单的「起草备注」，她私聊的草稿才知道。</div>}
        </>
      )}
      {g.state === 'unsure' && (
        <>
          <Note kind="warn">不确定建没建成：{g.why}</Note>
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="btn primary" disabled={busy} onClick={() => run({ action: 'group_made' }, '记下了：三方群建好了')}><Check />建好了</button>
            <button type="button" className="btn" disabled={busy} onClick={() => run({ action: 'group_none' }, '记下了：没建成，可以再拉一次')}>没建成</button>
          </div>
        </>
      )}
      {(making || working) && (
        <Note kind="warn">{making ? '正在这台电脑的微信里建群' : '正在试（不建群）'}：大约半分钟，请别动键盘鼠标。</Note>
      )}
      {(!g.state || g.state === 'failed') && !working && (
        <>
          {g.state === 'failed' && <Note kind="need">上次没建成：{g.why}</Note>}
          <div className="f-hint">
            谈到寄样时拉三方群：机器人在这台电脑微信的「发起群聊」里搜她和 {names}，各自恰好搜到一个才勾上，读一遍已选的人没错才点「完成」。
            它不在群里说话，也不改群名。第一次用之前可以先点「先试一下」：勾上再取消，不建群。
          </div>
          {tried && (
            <div className={tried.ok ? 'f-hint' : 'f-err'}>{fmtClock(tried.at)} 试过：{tried.message}</div>
          )}
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <button type="button" className="btn primary" disabled={busy || !!why} onClick={() => setAsking(true)}><UsersThree />拉三方群</button>
            <button type="button" className="btn" disabled={busy || !!why} onClick={() => run({ action: 'group_try' }, '在试了：大约半分钟，别动键盘鼠标')}>先试一下（不建群）</button>
            <button type="button" className="btn sm ghost" disabled={busy}
              onClick={() => run({ action: 'group_made' }, '记下了：三方群建好了')}>已经手动建好了</button>
            {why && <span className="dim">{why}</span>}
          </div>
        </>
      )}
      {asking && (
        <Confirm title={`给 ${p.name} 拉三方群？`} confirmLabel="拉群" cancelLabel="再想想" busy={busy}
          onConfirm={() => { setAsking(false); run({ action: 'group' }, '开始建群了：大约半分钟，别动键盘鼠标') }}
          onCancel={() => setAsking(false)}>
          机器人会在这台电脑的微信里点「+」›「发起群聊」，搜她在微信里的名字和 {names}，各自恰好搜到一个才勾上，读一遍已选的人没错才点「完成」。
          接下来大约半分钟它会操作微信窗口，请别动键盘鼠标。点一次只建一个群，不会自动重试。机器人不会在群里说话；
          群建好以后，请在手机上把群名改成「{suggested}」。
        </Confirm>
      )}
    </div>
  )
}
