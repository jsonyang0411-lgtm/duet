import { Fragment, useState } from 'react'
import { ArrowClockwise, PaperPlaneRight, PencilSimple, X, Paperclip, WarningCircle, Eye, EyeSlash, Flag, Lightning, CheckCircle, Copy, ChatsCircle } from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import { usePrm, PACKET_STATES, decide, revealNumbers, setupProblems, buyinNotes, fmtSize, VERDICT_TAGS, outcomeTag, executePlatform, repreview, ackPlatform, copyBuyin, buyinSent } from '../lib/prm'
import { PageHead, Card, Note, Banner, Empty, Confirm, TextArea } from './ui'

// 待审批: what the Mac drafted for the registered creators. Nothing reaches a
// creator until somebody here clicks 发送 (or edits and sends). A new message
// from the creator makes the waiting draft out of date; a newer one follows.
// A draft with a platform action (小红书 / 抖音 / 淘宝) shows the Mac's preview of it;
// 批准并执行 runs it on the platform once, and only a verified write lets the
// reply go out. Anything less is listed red at the top until marked dealt with.
// A 抖音百应 private-message draft (PRM_BUYIN_SYNC_MIN) is never sent by the bot: an
// operator copies it, sends it in 百应 by hand and says so (已在百应发了).

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
        {buyinNotes(data).map(([kind, text]) => <Note key={text} kind={kind} icon={ChatsCircle}>{text}</Note>)}
        {!data && <Empty title="正在读取…" />}
        <PlatformAlerts alerts={data?.platform_alerts || []} onDone={reload} />
        {data && !pending.length && (
          <Empty title="现在没有要审批的草稿">已登记的达人发来消息后，Mac 写好的回复会出现在这里。</Empty>
        )}
        <div className="stack" style={{ gap: 12 }}>
          {pending.map((p) => (p.channel === 'buyin'
            ? <BuyinCard key={p.id} p={p} onDone={reload} />
            : <DraftCard key={p.id} p={p} canSend={!!data?.can_send} onDone={reload} />))}
        </div>
        <RecentCard d={data} onDone={reload} />
        <GatesCard d={data} />
      </div>
    </div>
  )
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

function DraftCard({ p, canSend, onDone }) {
  const toast = useToast()
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(p.text)
  const [busy, setBusy] = useState(false)
  const [dropping, setDropping] = useState(false)
  const [executing, setExecuting] = useState(null)   // the text to execute with (null = no dialog)
  const pf = p.platform
  const waiting = p.status !== 'pending'
  const act = async (action, body) => {
    setBusy(true)
    try {
      if (action === 'execute') await executePlatform(p.id, body)
      else if (action === 'repreview') await repreview(p.id)
      else await decide(p.id, action, body)
      toast.ok({ drop: '这条不发了', execute: '已批准，Mac 正在平台上执行', repreview: '已请 Mac 重新预览' }[action] || '已确认，正在发送')
      setEditing(false)
      setDropping(false)
      setExecuting(null)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  const [label, kind] = PACKET_STATES[p.status] || [p.status, 'pend']
  const slots = Object.entries(p.slots || {})
  return (
    <Card
      title={`${p.party_name} · ${p.kind_label}`}
      eyebrow={`DRAFT #${p.id}`}
      sub={`${fmtClock(p.created_at)} ${p.system ? '系统生成' : '写好'}${p.stage_proposal_label ? ` · 建议阶段：${p.stage_proposal_label}` : ''}`}
      actions={<>{p.route === 'auto' && <span className="tag ok" title="这类消息已经达到自动发送门槛">自动发送</span>}<span className={`tag ${kind}`}>{label}</span></>}
      className="prm-draft"
    >
      <div className="stack" style={{ gap: 8 }}>
        {p.needs_attention && (
          <Note kind="warn" icon={WarningCircle}>要特别注意{p.reason ? `：${p.reason}` : ''}</Note>
        )}
        {p.intent && <div><b>达人想要：</b>{p.intent}</div>}
        {!p.needs_attention && p.reason && <div className="dim"><b>依据：</b>{p.reason}</div>}
        {slots.length > 0 && (
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {slots.map(([k, v]) => <span key={k} className="tag">{k}：{v}</span>)}
          </div>
        )}
        {pf && <PlatformBox pf={pf} />}
        {pf && <div className="f-hint">下面是平台上核验成功以后才发给达人的回复：</div>}
        {editing
          ? <TextArea value={text} onChange={setText} rows={5} aria-label="修改要发的内容" />
          : <div className="prm-text" style={{ whiteSpace: 'pre-wrap' }}>{p.text || <span className="dim">（只发附件，没有文字）</span>}</div>}
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
                <button type="button" className="btn" disabled={busy} onClick={() => { setEditing(false); setText(p.text) }}>取消修改</button>
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
                <button type="button" className="btn primary" disabled={busy || !canSend} onClick={() => act('edit', text)}><PaperPlaneRight />确认发送</button>
                <button type="button" className="btn" disabled={busy} onClick={() => { setEditing(false); setText(p.text) }}>取消修改</button>
              </>
            ) : (
              <>
                <button type="button" className="btn primary" disabled={busy || !canSend} onClick={() => act('send')}><PaperPlaneRight />发送</button>
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
      {executing !== null && pf && (
        <Confirm title={`批准并执行 #${p.id}？`} confirmLabel="执行" cancelLabel="再想想" busy={busy}
          onConfirm={() => act('execute', executing)} onCancel={() => setExecuting(null)}>
          <div className="stack" style={{ gap: 6 }}>
            <div>这会在平台上真实写入：<b>{pf.label}</b>。{pf.effect}</div>
            {pf.caveat && <div className="f-err">{pf.caveat}</div>}
            {pf.params.map((x) => <div key={x.label} className="f-hint">{x.label}：<span className="mono">{x.value}</span></div>)}
            <div>只执行一次，不会自动重试；平台读回核验成功（VERIFIED_SUCCESS）才把回复发给达人，否则不发，并在这一页上方标红。</div>
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
      toast.ok(`已标为发错了：「${p.kind_label}」以后都先审，重新计数`)
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
                  <td>{buyin ? `百应私信 · ${p.kind_label}` : p.kind_label}</td>
                  <td>
                    {buyin && p.status === 'sent' ? (
                      <span className="tag ok" title="运营在百应里手动发的（机器人没有发）">已在百应手动发{p.edited ? ' · 改过' : ''}</span>
                    ) : (
                      <span className={`tag ${kind}`} title={p.status === 'sent' ? (confirmed ? '在微信里看到了这条' : '按键已发出，还没在微信里看到') : undefined}>
                        {label}{p.status === 'sent' ? (confirmed ? ' · 已确认' : ' · 未确认') : ''}{p.edited ? ' · 改过' : ''}
                      </span>
                    )}
                    {p.route === 'auto' && <span className="tag ok">自动</span>}
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
          「{asking.kind_label}」这类消息会马上退回「先审后发」，自动发送的计数从头开始。已经发出去的消息收不回来，需要的话请在微信里补一句。
        </Confirm>
      )}
    </Card>
  )
}

/** 8.1: which kinds still wait for a person, and how far each is from going out on its own. */
function GatesCard({ d }) {
  const gates = d?.gates || []
  if (!gates.length) return null
  return (
    <Card title="自动发送门槛" eyebrow="GATES" className="prm-gates"
      sub="某类消息最近 N 条里运营原样发出的条数达标、而且没改过金额 / 号码 / 日期，这一类以后就直接发；钱和投诉一直先审">
      <div className="scroll" style={{ overflowX: 'auto' }}>
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
      </div>
    </Card>
  )
}
