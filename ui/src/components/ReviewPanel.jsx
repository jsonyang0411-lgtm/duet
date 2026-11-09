import { Fragment, useState } from 'react'
import { ArrowClockwise, PaperPlaneRight, PencilSimple, X, Paperclip, WarningCircle, Eye, EyeSlash, Flag, Lightning, CheckCircle, Copy, ChatsCircle, Robot, HandPalm, UsersThree, DeviceMobile } from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import { usePrm, PACKET_STATES, decide, revealNumbers, setupProblems, buyinNotes, buyinFriendAddLine, fmtSize, VERDICT_TAGS, outcomeTag, executePlatform, repreview, ackPlatform, dealAction, nightStatus, copyBuyin, buyinSent, setAutoMode, replyPartCount, inGroup, bossGroupNotes, handSampleSeen, handSampleLine, wechatReviewNote } from '../lib/prm'
import { PageHead, Card, Note, Banner, Empty, Confirm, TextArea } from './ui'
import { FriendAddList } from './FriendAddCard'

// 待审批: what the Mac drafted for the registered creators. Nothing reaches a
// creator until somebody here clicks 发送 (or edits and sends). A new message
// from the creator makes the waiting draft out of date; a newer one follows.
// A draft with a platform action (小红书 / 抖音 / 淘宝) shows the Mac's preview of it;
// 批准并执行 runs it on the platform once, and only a verified write lets the
// reply go out. Anything less is listed red at the top until marked dealt with.
// 下班时段 (PRM_NIGHT_AUTO_HOURS): inside the window a draft that passes every night rule goes
// out by itself (tag 夜间自动发出); a holding line sent for a flagged draft stays at the top
// until 已处理; the status line sits above the gate table.
// A 抖音百应 private-message draft (PRM_BUYIN_SYNC_MIN) is never sent by the bot: an
// operator copies it, sends it in 百应 by hand and says so (已在百应发了).
// 加微信 (PRM_FRIEND_ADD): the contacts 百应 creators left, one confirmed friend request each (FriendAddCard.jsx),
// below the drafts (2026-11-05: the user scrolled past a long list of them to reach the drafts).
// 自动模式: the same rules at any hour, switched at the top of the page (on: a warn line with
// the button that switches it off); what it sent by day is tagged 自动模式发出.
// 手动建的寄样单 (PRM_SAMPLE_SYNC_MIN's ``mine``, 2026-11-05): one of our 聚水潭 orders made by hand shipped and its
// waybill went into no deal (nobody registered fits, two do, the deal is past the sample, another waybill there):
// a line at the top with the waybill, until 「知道了」; a matched one is a shipping notice among the drafts instead.
// 老板拉的群 (PRM_BOSS_GROUPS): a draft of her line in the group the boss made with her and us
// (``channel`` "group") is tagged 「群里 · <群名>」 and its 发送 asks first: it goes in the group, where
// the boss sees it. A group that needs a person to say which member she is gets one line at the top.
// 微信里审批草稿 (PRM_WECHAT_REVIEW): one line at the top says the drafts also go to the admin's WeChat (or why they
// cannot), and a draft told there is tagged 已发到微信; deciding it here or there is the same decision.

export default function ReviewPanel() {
  const { data, err, reload } = usePrm(4000)
  const pending = data?.pending || []
  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="03" en="REVIEW" title="待审批"
          sub="AI 给达人写好的回复先在这里过一遍：点「发送」才会发出去，达人那边什么都看不到"
          actions={<button type="button" className="btn sm" onClick={reload}><ArrowClockwise />刷新</button>}
        />
        {err && <Note kind="need">{err}</Note>}
        {setupProblems(data).map(([kind, text]) => <Note key={text} kind={kind}>{text}</Note>)}
        <WechatReviewLine d={data} />
        {buyinNotes(data).map(([kind, text]) => <Note key={text} kind={kind} icon={ChatsCircle}>{text}</Note>)}
        {bossGroupNotes(data).map(([kind, text]) => <Note key={text} kind={kind} icon={UsersThree}>{text}</Note>)}
        {data?.hand_samples_problem && <Note kind="warn">{data.hand_samples_problem}</Note>}
        <HandSamples rows={data?.hand_samples || []} onDone={reload} />
        {!data && <Empty title="正在读取…" />}
        <AutoModeBar d={data} onDone={reload} />
        <PlatformAlerts alerts={data?.platform_alerts || []} onDone={reload} />
        <NightAlerts alerts={data?.night_alerts || []} onDone={reload} />
        {data && !pending.length && (
          <Empty title="现在没有要审批的草稿">已登记的达人发来消息后，Mac 写好的回复会出现在这里。</Empty>
        )}
        <div className="stack" style={{ gap: 12 }}>
          {pending.map((p) => (p.channel === 'buyin'
            ? <BuyinCard key={p.id} p={p} onDone={reload} />
            : <DraftCard key={p.id} p={p} canSend={!!data?.can_send} replyParts={data?.reply_parts || 1} onDone={reload} />))}
        </div>
        <FriendAddList d={data} onDone={reload} />
        <RecentCard d={data} onDone={reload} />
        <GatesCard d={data} />
      </div>
    </div>
  )
}

/** 微信里审批草稿 (PRM_WECHAT_REVIEW): the drafts also go to the admin's WeChat, or why they cannot. */
function WechatReviewLine({ d }) {
  const note = wechatReviewNote(d)
  if (!note) return null
  return <div className="prm-wechat-review"><Note kind={note[0]} icon={DeviceMobile}>{note[1]}</Note></div>
}

/** "〈手机1〉 = 138****5678" and, after a click that is written down, the full numbers. */
function Numbers({ partyId, legend }) {
  const toast = useToast()
  const [numbers, setNumbers] = useState(null)
  if (!legend?.length) return null
  const reveal = async () => {
    try { setNumbers((await revealNumbers(partyId)).numbers || []) } catch (e) { toast.err(e.message) }
  }
  const full = Object.fromEntries((numbers || []).map((n) => [n.token, n.cleared ? '已清除' : n.value]))
  return (
    <div className="f-hint">
      {legend.map((l) => `${l.token} = ${numbers ? full[l.token] || l.masked : l.masked}`).join('；')}（发送时换成真实号码；改的时候保留占位符）
      {!numbers && <button type="button" className="link" style={{ marginLeft: 8 }} onClick={reveal}><Eye style={{ verticalAlign: -2 }} />显示完整号码</button>}
      {numbers && <button type="button" className="link" style={{ marginLeft: 8 }} onClick={() => setNumbers(null)}><EyeSlash style={{ verticalAlign: -2 }} />隐藏</button>}
    </div>
  )
}

/** Platform actions that ended without a verified write (or whose reply did not go out). */
function PlatformAlerts({ alerts, onDone }) {
  const toast = useToast()
  const [busy, setBusy] = useState(0)
  if (!alerts.length) return null
  const ack = async (id) => {
    setBusy(id)
    try { await ackPlatform(id); toast.ok('已标为处理过了'); onDone() } catch (e) { toast.err(e.message) } finally { setBusy(0) }
  }
  return (
    <div className="stack prm-alerts" style={{ gap: 8 }}>
      {alerts.map((p) => {
        const ex = p.platform?.execution
        return (
          <Banner key={p.id} kind="" title={`#${p.id} ${p.party_name} · ${p.platform?.label || '平台动作'}：要人工处理`}
            actions={<button type="button" className="btn sm" disabled={busy === p.id} onClick={() => ack(p.id)}><CheckCircle />我已处理</button>}>
            <div>{p.error || '平台动作没有核验成功，回复没有发给达人'}</div>
            {ex && <div className="f-hint">结果：{ex.outcome}（{ex.outcome_label}）{ex.late ? ' · 结果晚到' : ''}</div>}
            {ex?.created?.link && <div className="f-hint">平台给的链接：<span className="mono">{ex.created.link}</span></div>}
            {ex?.created?.channel_id && <div className="f-hint">渠道品 ID：<span className="mono">{ex.created.channel_id}</span></div>}
            {(ex?.problems || []).map((t) => <div key={t} className="f-err">{t}</div>)}
          </Banner>
        )
      })}
    </div>
  )
}

/** 自动模式 (POST /api/prm/auto): the 下班时段 rules at any hour, until switched off. Off: the button, and a confirm
 * dialog that says what happens; on: a warn line with when it was switched on and the button that switches it off
 * (no confirm: off is the safe way). Stopped, the switch is kept for the bot's next start. */
function AutoModeBar({ d, onDone }) {
  const toast = useToast()
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  if (!d?.enabled) return null
  const a = d.auto_mode || {}
  const toggle = async (on) => {
    setBusy(true)
    try {
      await setAutoMode(on)
      toast.ok(on ? `自动模式已打开${d.running ? '' : '（机器人启动以后生效）'}：满足安全条件的草稿会直接发给达人` : '自动模式已关掉：草稿照旧等你审批')
      setAsking(false)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="prm-auto" style={{ marginBottom: 12 }}>
      {a.on ? (
        <Banner kind="warn" title={`自动模式开着（${fmtClock(a.since)} 打开）`}
          actions={<button type="button" className="btn sm" disabled={busy} onClick={() => toggle(false)}><HandPalm />关掉自动模式</button>}>
          满足安全条件的草稿不经你审批直接发给达人，拿不准的只回一句过渡的话；{d.opening_auto_min > 0
            ? '新好友的第一份草稿、开场白满足条件也直接发；投诉、平台动作照旧等你。'
            : '投诉、平台动作、新好友的第一份草稿、开场白照旧等你。'}
          {!d.running && ' 机器人现在没在运行：启动以后照这个自动发。'}
        </Banner>
      ) : (
        <Note>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', justifyContent: 'space-between' }}>
            <span>自动模式关着：草稿照常等你审批。要离开电脑时按「打开自动模式」，满足安全条件的草稿就直接发出。</span>
            <button type="button" className="btn sm" disabled={busy} onClick={() => setAsking(true)}><Robot />打开自动模式</button>
          </div>
        </Note>
      )}
      {asking && (
        <Confirm title="打开自动模式？" confirmLabel="打开自动模式" cancelLabel="再想想" busy={busy}
          onConfirm={() => toggle(true)} onCancel={() => setAsking(false)}>
          <div className="stack" style={{ gap: 6 }}>
            <div>打开以后不管几点，都照下班时段的规则：满足安全条件的草稿<b>不经你审批直接发出</b>（佣金不超过 25%，不提坑位费、保底、返现这类）；拿不准的只回一句过渡的话，这一页顶上留提醒，你看过点「已处理」。</div>
            <div>{d.opening_auto_min > 0
              ? '投诉、平台动作（还有补读的、人工接管的）照旧等你；新好友的第一份草稿、开场白满足条件也直接发出。现在已经在「待审批」里的草稿照旧等你（开场白到了卡片上写的时间照样自动发）。'
              : '投诉、平台动作、新好友的第一份草稿、开场白（还有补读的、人工接管的）照旧等你；现在已经在「待审批」里的草稿也照旧等你。'}</div>
            <div>一直开着，机器人重启也不关；再按一下「关掉自动模式」就关掉。</div>
          </div>
        </Confirm>
      )}
    </div>
  )
}

/** 老板拉的群 (PRM_BOSS_GROUPS): a draft of her line in the group the boss made with her and us goes out in that group. */
function GroupTag({ p }) {
  if (!inGroup(p)) return null
  return (
    <span className="tag warn prm-group-tag" title={`发在老板拉的群「${p.group_name || ''}」里（不是她的私聊），老板也看得到`}>
      <UsersThree /><span className="prm-group-name">群里 · {p.group_name || '老板拉的群'}</span>
    </span>
  )
}

/** A draft the 下班时段 window (or 自动模式 by day: ``auto_mode``) sent by itself. */
function NightTag({ p }) {
  if (p.route !== 'night') return null
  return p.auto_mode
    ? <span className="tag cy" title="自动模式下满足全部安全条件，没经过人审">自动模式发出</span>
    : <span className="tag cy" title="下班时段里满足全部安全条件，没经过人审">夜间自动发出</span>
}

/** 开场白没人处理就自动发 (PRM_OPENING_AUTO_MIN): our first line to a new friend that waited its time in 待审批 with
 * nobody deciding, and went out by itself. */
function OpeningAutoTag({ p }) {
  if (p.route !== 'opening') return null
  return <span className="tag cy" title="给新好友的第一句话在「待审批」里放到时间没人处理，满足安全条件，照原样自动发出（没经过人审）">开场白自动发出</span>
}

/** Our hand-made 聚水潭 sample orders that shipped with their waybill in no deal: each line stays until 知道了. */
function HandSamples({ rows, onDone }) {
  const toast = useToast()
  const [busy, setBusy] = useState('')
  if (!rows.length) return null
  const seen = async (h) => {
    setBusy(h.order_no)
    try {
      await handSampleSeen(h.order_no)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy('') }
  }
  const copy = async (h) => {
    try { await navigator.clipboard.writeText(`${h.carrier} ${h.waybill}`.trim()); toast.ok('已复制运单号') } catch { toast.err('没能放进剪贴板：请选中运单号自己复制') }
  }
  return (
    <div className="stack prm-hand-samples" style={{ gap: 8 }}>
      {rows.map((h) => (
        <Banner key={h.order_no} kind="warn" title={handSampleLine(h)}
          actions={<>
            {h.waybill && <button type="button" className="btn sm" onClick={() => copy(h)}><Copy />复制运单号</button>}
            <button type="button" className="btn sm" disabled={busy === h.order_no} onClick={() => seen(h)}><CheckCircle />知道了</button>
          </>}>
          <div className="f-hint">{h.sent_at ? `${fmtClock(h.sent_at)} 发货；` : ''}手动建的寄样单：机器人没有替她填进合作单，告诉她运单号以后点「知道了」</div>
        </Banner>
      ))}
    </div>
  )
}

/** Holding lines the 下班时段 window (or 自动模式) sent for drafts the model flagged: each stays until 已处理. */
function NightAlerts({ alerts, onDone }) {
  const toast = useToast()
  const [busy, setBusy] = useState('')
  if (!alerts.length) return null
  const done = async (a) => {
    const key = `${a.deal_id}:${a.packet}`
    setBusy(key)
    try {
      await dealAction({ action: 'night_done', deal_id: a.deal_id, packet: a.packet })
      toast.ok('已标为处理过了')
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy('') }
  }
  return (
    <div className="stack prm-night-alerts" style={{ gap: 8 }}>
      {alerts.map((a) => {
        const key = `${a.deal_id}:${a.packet}`
        return (
          <Banner key={key} kind="warn" title={`${a.party_name} · ${a.auto ? '自动模式' : '夜间'}自动回了过渡的话${a.packet ? `（#${a.packet}）` : ''}`}
            actions={<button type="button" className="btn sm" disabled={busy === key} onClick={() => done(a)}><CheckCircle />已处理</button>}>
            <div>{a.text}</div>
            {a.at && <div className="f-hint">{fmtClock(a.at)} 发出；运营跟进以后点「已处理」</div>}
          </Banner>
        )
      })}
    </div>
  )
}

/** One CLI command of a preview or an execution: its line, exit code, what it said. */
function StepView({ st }) {
  const tag = st.outcome ? outcomeTag(st.outcome) : (VERDICT_TAGS[st.verdict] || 'pend')
  return (
    <div className="prm-step">
      <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
        <b>{st.label}</b>
        <span className={`tag ${tag}`}>{st.outcome || st.verdict || '—'}</span>
        <span className="dim mono">退出码 {st.exit_code ?? '—'}{st.seconds != null ? ` · ${st.seconds}s` : ''}{st.execute ? ' · --execute' : ' · 预览'}</span>
      </div>
      <div className="mono prm-cmd">{st.command}</div>
      {(st.facts || []).map((f, i) => <div key={i} className="f-hint"><b>{f.label}：</b>{f.value}</div>)}
      {(st.problems || []).map((t) => <div key={t} className="f-err">{t}</div>)}
      {st.raw && <details><summary className="f-hint">命令输出</summary><pre className="prm-raw">{st.raw}</pre></details>}
    </div>
  )
}

/** A draft's platform action: what it will do, the Mac's verdict, and the detail behind it. */
function PlatformBox({ pf }) {
  const [open, setOpen] = useState(false)
  const ex = pf.execution
  return (
    <div className="prm-platform">
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <Lightning weight="bold" />
        <b>平台动作：{pf.label}</b>
        <span className={`tag ${VERDICT_TAGS[pf.verdict] || 'need'}`}>{pf.verdict_label}</span>
        {pf.previewing && <span className="tag pend">正在重新预览…</span>}
      </div>
      {pf.effect && <div className="f-hint">{pf.effect}</div>}
      {pf.caveat && <Note kind="warn">{pf.caveat}</Note>}
      {pf.params.length > 0 && (
        <dl className="kv">{pf.params.map((x) => <Fragment key={x.label}><dt>{x.label}</dt><dd className="mono">{x.value}</dd></Fragment>)}</dl>
      )}
      {pf.requested && <div className="f-hint">草稿里写的：<span className="mono">{JSON.stringify(pf.requested)}</span></div>}
      {pf.problems.map((t) => <Note key={t} kind="need">{t}</Note>)}
      {pf.facts.map((f, i) => <div key={i} className="f-hint"><b>{f.label}：</b>{f.value}</div>)}
      {ex && <div className="f-hint">执行结果：<span className={`tag ${outcomeTag(ex.outcome)}`}>{ex.outcome}</span> {ex.outcome_label}</div>}
      {pf.steps.length > 0 && (
        <button type="button" className="link" onClick={() => setOpen(!open)}>{open ? '收起预览明细' : `看预览明细（${pf.steps.length} 条命令）`}</button>
      )}
      {open && pf.steps.map((st, i) => <StepView key={i} st={st} />)}
      {pf.previewed_at && <div className="f-hint">预览于 {fmtClock(pf.previewed_at)}（Mac 上的时间）</div>}
      <div className="f-hint">{pf.placeholders?.length
        ? `回复里可以用：${pf.placeholders.map((x) => `${x.name}（${x.label}）`).join('、')}，执行成功后换成平台给的值${pf.makes_link ? '；没写 {链接} 时链接接在回复末尾' : ''}`
        : '这个动作没有可用的占位符：回复原样发出'}</div>
    </div>
  )
}

function DraftCard({ p, canSend, replyParts = 1, onDone }) {
  const toast = useToast()
  // a 「改了再执行」 that could not be queued for the Mac: the edited reply is kept for the next try (only the edit box has it)
  const kept = p.status === 'pending' && p.platform && p.final_text != null && p.final_text !== p.text ? p.final_text : null
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(kept ?? p.text)
  const [busy, setBusy] = useState(false)
  const [dropping, setDropping] = useState(false)
  const [executing, setExecuting] = useState(null)   // the text to execute with (null = no dialog)
  const [sending, setSending] = useState(null)       // 老板拉的群: the send that asks first ({action, text}; null = no dialog)
  const pf = p.platform
  const group = inGroup(p)                           // it goes out in the boss's group, where he sees it
  const waiting = p.status !== 'pending'
  const act = async (action, body) => {
    setBusy(true)
    try {
      if (action === 'execute') await executePlatform(p.id, body)
      else if (action === 'repreview') await repreview(p.id)
      else await decide(p.id, action, body)
      toast.ok({ drop: '这条不发了', execute: '已批准，Mac 正在平台上执行', repreview: '已请 Mac 重新预览' }[action]
        || (group ? '已确认，正在发到群里' : '已确认，正在发送'))
      setEditing(false)
      setDropping(false)
      setExecuting(null)
      setSending(null)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  // 发送 / 确认发送: a draft for the boss's group asks first; any other draft goes at once, as it always did.
  const send = (action, body) => (group ? setSending({ action, text: body }) : act(action, body))
  const [label, kind] = PACKET_STATES[p.status] || [p.status, 'pend']
  const slots = Object.entries(p.slots || {})
  const parts = replyPartCount(editing ? text : p.text, replyParts)   // PRM_REPLY_PARTS: a blank line starts the next message
  return (
    <Card
      title={`${p.party_name} · ${p.kind_label}`}
      eyebrow={`DRAFT #${p.id}`}
      sub={`${fmtClock(p.created_at)} ${p.system ? '系统生成' : '写好'}${p.stage_proposal_label ? ` · 建议阶段：${p.stage_proposal_label}` : ''}`}
      actions={<><GroupTag p={p} />{p.wechat_at && <span className="tag cy prm-wechat-tag" title={`${fmtClock(p.wechat_at)} 发到管理员微信里了：在微信里${p.platform ? '引用通知回「执行」/ 2' : '回 1 / 2 / 改'} 也行，和这里点是同一个决定`}>已发到微信</span>}{p.opening && <span className="tag cy" title={p.auto_at ? `她还没跟我们说过话：这是我们先打招呼的话，${fmtClock(p.auto_at)} 还没人处理就自动发` : '她还没跟我们说过话：这是我们先打招呼的话，要人看过再发'}>开场白</span>}{p.huopan && <span className="tag cy" title="运营点了「补发货盘」：我们主动补发的货盘，要人看过再发（不会自动发）">补发货盘</span>}{p.route === 'auto' && <span className="tag ok" title="这类消息已经达到自动发送门槛">自动发送</span>}<NightTag p={p} /><OpeningAutoTag p={p} /><span className={`tag ${kind}`}>{label}</span></>}
      className="prm-draft"
    >
      <div className="stack" style={{ gap: 8 }}>
        {p.needs_attention && (
          <Note kind="warn" icon={WarningCircle}>要特别注意{p.reason ? `：${p.reason}` : ''}</Note>
        )}
        {p.night_held && <div className="f-hint prm-night-held">{p.auto_mode ? '自动模式没有自动发' : '夜间没有自动发'}：{p.night_held}</div>}
        {p.status === 'pending' && p.auto_at && (
          <div className="f-hint prm-opening-auto">给新好友的第一句话：{fmtClock(p.auto_at)} 还没人处理就照原样自动发给她（设置里的「开场白没人处理多久自动发」）；要改的话在这之前发出。</div>
        )}
        {p.status === 'pending' && p.auto_held && <div className="f-hint prm-opening-held">这份不会自动发，等你：{p.auto_held}</div>}
        {p.intent && <div><b>{p.opening || p.huopan ? '这一条：' : '达人想要：'}</b>{p.intent}</div>}
        {!p.needs_attention && p.reason && <div className="dim"><b>依据：</b>{p.reason}</div>}
        {slots.length > 0 && (
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {slots.map(([k, v]) => <span key={k} className="tag">{k}：{v}</span>)}
          </div>
        )}
        {pf && <PlatformBox pf={pf} />}
        {pf && <div className="f-hint">下面是平台上核验成功以后才发给达人的回复：</div>}
        {kept != null && !editing && <div className="f-hint prm-kept-edit">上次「改了再执行」没排进 Mac 的任务队列，改过的回复还留着：点「改了再执行」接着用；直接点「批准并执行」发的是下面的原稿</div>}
        {editing
          ? <TextArea value={text} onChange={setText} rows={5} aria-label="修改要发的内容" />
          : <div className="prm-text" style={{ whiteSpace: 'pre-wrap' }}>{p.text || <span className="dim">（只发附件，没有文字）</span>}</div>}
        {replyParts > 1 && (editing || parts > 1) && (
          <div className="f-hint prm-parts">{parts > 1 ? `会分 ${parts} 条发` : '整段一条发'}（空一行就是分开发，最多 {replyParts} 条）</div>
        )}
        {p.waybill && <div className="f-hint">系统里的运单号：<span className="mono">{p.waybill}</span>（发出的内容里必须是这个单号）</div>}
        <Numbers partyId={p.party_id} legend={p.legend} />
        {p.attachments?.length > 0 && (
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {p.attachments.map((a) => <span key={a.name} className="tag"><Paperclip />{a.name} {fmtSize(a.size)}</span>)}
          </div>
        )}
        {p.status === 'executing' && (
          <Note kind="warn">平台执行中（Mac 任务 #{pf?.job_id ?? '—'}）：核验成功后自动把回复发给达人；没核验成功就不发，在页面上方标红。
            {pf?.lost_at ? ` Mac 从 ${fmtClock(pf.lost_at)} 起失联，等它回来；30 分钟还没回来就算结果未知。` : ''}</Note>
        )}
        {!waiting && pf && (
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            {editing ? (
              <>
                <button type="button" className="btn primary" disabled={busy || !canSend || !pf.can_execute} onClick={() => setExecuting(text)}><Lightning />确认并执行</button>
                <button type="button" className="btn" disabled={busy} onClick={() => { setEditing(false); setText(kept ?? p.text) }}>取消修改</button>
              </>
            ) : (
              <>
                <button type="button" className="btn primary" disabled={busy || !canSend || !pf.can_execute} onClick={() => setExecuting(undefined)}><Lightning />批准并执行</button>
                <button type="button" className="btn" disabled={busy || !pf.can_execute} onClick={() => setEditing(true)}><PencilSimple />改了再执行</button>
                <button type="button" className="btn" disabled={busy || pf.previewing || !pf.params.length} onClick={() => act('repreview')}><ArrowClockwise />重新预览</button>
                <button type="button" className="btn ghost" disabled={busy} onClick={() => setDropping(true)}><X />不发</button>
              </>
            )}
            {!canSend && <span className="dim">机器人没连着微信，现在不能批准执行</span>}
            {canSend && !pf.can_execute && !pf.previewing && <span className="dim">预览没通过，不能执行；处理好以后点「重新预览」</span>}
          </div>
        )}
        {!waiting && !pf && (
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            {editing ? (
              <>
                <button type="button" className="btn primary" disabled={busy || !canSend} onClick={() => send('edit', text)}><PaperPlaneRight />确认发送</button>
                <button type="button" className="btn" disabled={busy} onClick={() => { setEditing(false); setText(p.text) }}>取消修改</button>
              </>
            ) : (
              <>
                <button type="button" className="btn primary" disabled={busy || !canSend} onClick={() => send('send')}><PaperPlaneRight />发送</button>
                <button type="button" className="btn" disabled={busy} onClick={() => setEditing(true)}><PencilSimple />改了再发</button>
                <button type="button" className="btn ghost" disabled={busy} onClick={() => setDropping(true)}><X />不发</button>
              </>
            )}
            {!canSend && <span className="dim">机器人没连着微信，现在发不出去</span>}
          </div>
        )}
      </div>
      {dropping && (
        <Confirm title={`不发 #${p.id}？`} confirmLabel="不发" cancelLabel="再想想" danger busy={busy}
          onConfirm={() => act('drop')} onCancel={() => setDropping(false)}>
          这条草稿不会发给达人{pf ? '，平台上也不会执行' : ''}。达人再发消息时，会有新的草稿。
        </Confirm>
      )}
      {sending && (
        <Confirm title={`#${p.id} 发到群里？`} confirmLabel="发到群里" cancelLabel="再想想" busy={busy}
          onConfirm={() => act(sending.action, sending.text)} onCancel={() => setSending(null)}>
          这条回复会发在老板拉的群「{p.group_name || '（没有群名）'}」里，不是她的私聊：老板和群里的人都看得到。{parts > 1 ? `会分 ${parts} 条发。` : ''}
        </Confirm>
      )}
      {executing !== null && pf && (
        <Confirm title={`批准并执行 #${p.id}？`} confirmLabel="执行" cancelLabel="再想想" busy={busy}
          onConfirm={() => act('execute', executing)} onCancel={() => setExecuting(null)}>
          <div className="stack" style={{ gap: 6 }}>
            <div>这会在平台上真实写入：<b>{pf.label}</b>。{pf.effect}</div>
            {pf.caveat && <div className="f-err">{pf.caveat}</div>}
            {pf.params.map((x) => <div key={x.label} className="f-hint">{x.label}：<span className="mono">{x.value}</span></div>)}
            <div>只执行一次，不会自动重试；平台读回核验成功（VERIFIED_SUCCESS）才把回复发给达人，否则不发，并在这一页上方标红。</div>
            {group && <div>回复发在老板拉的群「{p.group_name || '（没有群名）'}」里，不是她的私聊：老板和群里的人都看得到。</div>}
          </div>
        </Confirm>
      )}
    </Card>
  )
}

/** A 抖音百应 private-message draft: copied, sent in 百应 by an operator, said so here. Not gated on the bot's WeChat. */
function BuyinCard({ p, onDone }) {
  const toast = useToast()
  const shown = p.final_text ?? p.text
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(shown)
  const [busy, setBusy] = useState(false)
  const [dropping, setDropping] = useState(false)
  const [copied, setCopied] = useState(null)        // the text with the full numbers, once copied
  const b = p.buyin || {}
  const slots = Object.entries(p.slots || {})
  const copy = async (body) => {
    setBusy(true)
    try {
      const r = await copyBuyin(p.id, body)
      setCopied(r.text)
      try { await navigator.clipboard.writeText(r.text); toast.ok('已复制：到百应私信里粘贴发给达人，发完点「已在百应发了」') } catch { toast.err('没能放进剪贴板：请选中下面的字自己复制') }
      setEditing(false)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  const act = async (action) => {
    setBusy(true)
    try {
      if (action === 'sent') await buyinSent(p.id, editing && text !== shown ? text : undefined)
      else await decide(p.id, 'drop')
      toast.ok(action === 'sent' ? '记下了：已在百应手动发出' : '这条不发了')
      setDropping(false)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  const [label, kind] = PACKET_STATES[p.status] || [p.status, 'pend']
  return (
    <Card
      title={`${p.party_name} · 百应私信`}
      eyebrow={`DRAFT #${p.id}`}
      sub={`${fmtClock(p.created_at)} 写好 · ${p.kind_label}${b.name ? ` · 百应里的昵称：${b.name}` : ''}${p.stage_proposal_label ? ` · 建议阶段：${p.stage_proposal_label}` : ''}`}
      actions={<><span className="tag warn">百应私信 · 运营手动发</span><span className={`tag ${kind}`}>{label}</span></>}
      className="prm-draft prm-buyin"
    >
      <div className="stack" style={{ gap: 8 }}>
        <Note icon={ChatsCircle}>达人是在抖音百应（精选联盟私信）里发的，机器人不能在百应里发消息：点「复制」，到百应里粘贴发给她，发完点「已在百应发了」。</Note>
        {p.needs_attention && <Note kind="warn" icon={WarningCircle}>要特别注意{p.reason ? `：${p.reason}` : ''}</Note>}
        {b.replied_at && <Note kind="warn">百应里我们在 {fmtClock(b.replied_at)} 已经回过她了（运营在百应里发的），这份可能不用发了。</Note>}
        {b.friend_add && <div className="dim prm-buyin-friend-add">{buyinFriendAddLine(b.friend_add)}</div>}
        {b.said && (
          <div><b>她在百应说：</b><div className="prm-said dim" style={{ whiteSpace: 'pre-wrap' }}>{b.said}</div></div>
        )}
        {p.intent && <div><b>达人想要：</b>{p.intent}</div>}
        {!p.needs_attention && p.reason && <div className="dim"><b>依据：</b>{p.reason}</div>}
        {slots.length > 0 && (
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {slots.map(([k, v]) => <span key={k} className="tag">{k}：{v}</span>)}
          </div>
        )}
        <div className="f-hint">在百应里发给她的回复：</div>
        {editing
          ? <TextArea value={text} onChange={setText} rows={5} aria-label="修改要复制的内容" />
          : <div className="prm-text" style={{ whiteSpace: 'pre-wrap' }}>{shown}</div>}
        <Numbers partyId={p.party_id} legend={p.legend} />
        {copied !== null && (
          <div className="stack" style={{ gap: 4 }}>
            <div className="f-hint">已复制（含完整号码，这次复制记在合作单里）：</div>
            <textarea className="textarea mono" readOnly rows={3} value={copied} aria-label="已复制的内容" onFocus={(e) => e.target.select()} />
          </div>
        )}
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          {editing ? (
            <>
              <button type="button" className="btn primary" disabled={busy || !text.trim()} onClick={() => copy(text)}><Copy />复制改好的</button>
              <button type="button" className="btn" disabled={busy} onClick={() => { setEditing(false); setText(shown) }}>取消修改</button>
            </>
          ) : (
            <>
              <button type="button" className="btn primary" disabled={busy} onClick={() => copy(undefined)}><Copy />复制</button>
              <button type="button" className="btn" disabled={busy} onClick={() => setEditing(true)}><PencilSimple />改了再复制</button>
            </>
          )}
          <button type="button" className="btn" disabled={busy} onClick={() => act('sent')}><CheckCircle />已在百应发了</button>
          <button type="button" className="btn ghost" disabled={busy} onClick={() => setDropping(true)}><X />不发</button>
        </div>
      </div>
      {dropping && (
        <Confirm title={`不发 #${p.id}？`} confirmLabel="不发" cancelLabel="再想想" danger busy={busy}
          onConfirm={() => act('drop')} onCancel={() => setDropping(false)}>
          这条百应私信的草稿不用了，机器人本来也不会发它。她在百应再发消息时，会有新的草稿。
        </Confirm>
      )}
    </Card>
  )
}

function RecentCard({ d, onDone }) {
  const toast = useToast()
  const [asking, setAsking] = useState(null)
  const [busy, setBusy] = useState(false)
  const recent = d?.recent || []
  if (!recent.length) return null
  const wrong = async (p) => {
    setBusy(true)
    try {
      await decide(p.id, 'wrong')
      toast.ok(`已标为发错了${p.route === 'night' ? (p.auto_mode ? '（自动模式发出的）' : '（夜间自动发出的）') : p.route === 'opening' ? '（没人处理、自动发出的开场白）' : ''}：「${p.kind_label}」以后都先审，重新计数`)
      setAsking(null)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  return (
    <Card title="最近处理过的" eyebrow="HISTORY" className="prm-recent" sub={`最近 ${recent.length} 条`}>
      <div className="scroll" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead><tr><th>编号</th><th>达人</th><th>类型</th><th>结果</th><th>内容</th><th>时间</th><th /></tr></thead>
          <tbody>
            {recent.map((p) => {
              const [label, kind] = PACKET_STATES[p.status] || [p.status, 'pend']
              const shown = p.final_text ?? p.text
              const confirmed = p.outcome === 'sent_confirmed'
              const buyin = p.channel === 'buyin'
              return (
                <tr key={p.id}>
                  <td className="num">#{p.id}</td>
                  <td>{p.party_name}</td>
                  <td title={inGroup(p) ? `发在老板拉的群「${p.group_name || ''}」里` : undefined}>
                    {buyin ? `百应私信 · ${p.kind_label}` : inGroup(p) ? `群里 · ${p.kind_label}` : p.kind_label}
                  </td>
                  <td>
                    {buyin && p.status === 'sent' ? (
                      <span className="tag ok" title="运营在百应里手动发的（机器人没有发）">已在百应手动发{p.edited ? ' · 改过' : ''}</span>
                    ) : (
                      <span className={`tag ${kind}`} title={p.status === 'sent' ? (confirmed ? '在微信里看到了这条' : '按键已发出，还没在微信里看到') : undefined}>
                        {label}{p.status === 'sent' ? (confirmed ? ' · 已确认' : ' · 未确认') : ''}{p.edited ? ' · 改过' : ''}
                      </span>
                    )}
                    {p.route === 'auto' && <span className="tag ok">自动</span>}
                    <NightTag p={p} />
                    <OpeningAutoTag p={p} />
                    {p.route === 'night' && p.needs_attention && <span className="tag warn" title={p.reason}>要注意</span>}
                    {p.wrong && <span className="tag need">发错了</span>}
                    {p.platform && <span className={`tag ${p.platform.execution ? outcomeTag(p.platform.execution.outcome) : 'off'}`} title={p.platform.label}>
                      平台 · {p.platform.execution ? p.platform.execution.outcome : '没执行'}</span>}
                    {p.platform?.execution?.created?.link && <div className="f-hint mono">{p.platform.execution.created.link}</div>}
                    {p.error && <div className="f-err">{p.error}</div>}
                  </td>
                  <td className="ellipsis" style={{ maxWidth: 360 }} title={shown}>{shown || '—'}</td>
                  <td className="num">{fmtClock(p.sent_at || p.decided_at || p.created_at)}</td>
                  <td>
                    {p.status === 'sent' && !p.wrong && !buyin && (
                      <button type="button" className="btn sm ghost" onClick={() => setAsking(p)}><Flag />标为发错了</button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {asking && (
        <Confirm title={`#${asking.id} 发错了？`} confirmLabel="标为发错了" cancelLabel="不标" danger busy={busy}
          onConfirm={() => wrong(asking)} onCancel={() => setAsking(null)}>
          {asking.route === 'night' && (asking.auto_mode ? '这条是自动模式自动发出的（没经过人审）。' : '这条是下班时段自动发出的（没经过人审）。')}
          {asking.route === 'opening' && '这条是给新好友的第一句话，放到时间没人处理，自动发出的（没经过人审）。'}
          「{asking.kind_label}」这类消息会马上退回「先审后发」，自动发送的计数从头开始。已经发出去的消息收不回来，需要的话请在微信里补一句。
        </Confirm>
      )}
    </Card>
  )
}

/** 下班时段自动回复 (PRM_NIGHT_AUTO_HOURS): on or off, night or day now (自动模式 too: ``n.auto``), and why it is held
 * when it is. */
function NightLine({ n, openingAuto }) {
  if (!n) return null
  const [kind, text] = nightStatus(n)
  return (
    <div className="prm-night" style={{ marginBottom: 12 }}>
      <Note kind={kind}>
        <b>{text}</b>
        {n.suspended && <div>{n.suspended}</div>}
        <div className="f-hint">
          {n.on || n.auto
            ? `${n.auto ? '自动模式开着，不管几点' : '下班时段里'}，Mac 写好的回复满足全部安全条件才直接发给达人（不看下面的门槛）：不是投诉，佣金不超过 25%，不提坑位费、保底、返现这类，模型要求注意的只发一句不带数字的过渡话；${openingAuto ? '新好友的第一份草稿、开场白满足条件也直接发；平台动作、补读的、撤回、人工接管的照旧等运营。' : '平台动作、补读的、新好友、撤回、人工接管的照旧等运营。'}`
            : '在「合作单」页下面的设置里填「下班时段自动回复」（例如 18:00-09:00）就打开。'}
          改了设置要重启机器人才生效（自动模式在这一页顶上开关，不用重启）。
        </div>
      </Note>
    </div>
  )
}

/** 8.1: which kinds still wait for a person, and how far each is from going out on its own. */
function GatesCard({ d }) {
  const gates = d?.gates || []
  if (!gates.length && !d?.night) return null
  return (
    <Card title="自动发送门槛" eyebrow="GATES" className="prm-gates"
      sub="某类消息最近 N 条里运营原样发出的条数达标、而且没改过金额 / 号码 / 日期，这一类以后就直接发；钱和投诉一直先审（下班时段另有规则，见下面这一行）">
      <NightLine n={d?.night} openingAuto={d?.opening_auto_min > 0} />
      {gates.length > 0 && <div className="scroll" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead><tr><th>类型</th><th>现在</th><th>最近审过的</th><th>原样发出</th><th>门槛</th></tr></thead>
          <tbody>
            {gates.map((g) => (
              <tr key={g.kind}>
                <td>{g.label}</td>
                <td>{g.locked ? <span className="tag off">一直先审</span> : g.route === 'auto' ? <span className="tag ok">自动发送</span> : <span className="tag pend">先审后发</span>}</td>
                <td className="num">{g.decided}/{g.window}</td>
                <td className="num">{g.unchanged}{g.key_changed && <span className="f-err"> · 有改关键信息</span>}</td>
                <td className="num">{g.locked ? '—' : `${g.min_unchanged}/${g.window}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>}
    </Card>
  )
}
