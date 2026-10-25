import { useCallback, useEffect, useState } from 'react'
import { Handshake, Copy, ArrowClockwise, CheckCircle, XCircle, UserPlus, PencilSimple, ChatCircleDots } from '@phosphor-icons/react'
import { get, post } from '../api'
import { useToast } from '../store/toast'
import { PageHead, Card, Field, TextInput, TextArea, NumberInput, Switch, Seg, Empty, Note, Banner, Dialog, Confirm } from './ui'

// 达人审批（src/influencer/，说明见 handoff.md）：审批单、达人名单两个子页。
// 「通过」会真的在聚水潭建出库单、在小红书绑佣金，撤不回，所以执行前要再确认一次。
// 发给达人的消息先由运营复制后在微信里手动发送（Q2）。提醒只做在控制台里（D14）：
// 左栏「达人审批」上的数字 = 待审批 + 待处理，「待处理」子页列出要人工回复的达人消息和缺货。

const FILTERS = [
  { value: 'pending', label: '待审批' },
  { value: 'executing', label: '执行中' },
  { value: 'fulfilled', label: '已执行' },
  { value: 'rejected', label: '已驳回' },
  { value: 'failed', label: '执行失败' },
  { value: 'expired', label: '已过期' },
  { value: 'all', label: '全部' },
]
const PENDING = 'PENDING_APPROVAL'
const STATUS_TAG = { PENDING_APPROVAL: 'cy', EXECUTING: 'pend', FULFILLED: 'ok', REJECTED: 'off', FAILED: 'need', EXPIRED: 'off' }

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '')
const percent = (rate) => Math.round(rate * 1000) / 10

function timeLeft(expiresAt) {
  const hours = (expiresAt - Date.now() / 1000) / 3600
  if (hours <= 0) return '已超过有效期'
  return hours < 1 ? '不到 1 小时后过期' : `${Math.floor(hours)} 小时后过期`
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // 桌面窗口里可能没有剪贴板接口，退回到选中文本再复制
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  let ok = false
  try { ok = document.execCommand('copy') } catch { ok = false }
  area.remove()
  return ok
}

// 「今日概览」：数字都来自数据库（B13），点一下进入对应的列表（R5）
function Overview({ onFilter }) {
  const toast = useToast()
  const [o, setO] = useState(null)
  useEffect(() => { get('/api/influencer/overview').then(setO, () => setO(null)) }, [])
  if (!o) return null
  const tiles = [
    { label: '待审批', value: o.pending, hint: o.expiring_soon ? `${o.expiring_soon} 单 12 小时内过期` : '', go: () => onFilter('pending'), warn: o.expiring_soon > 0 },
    { label: '待处理', value: o.inbox, hint: '达人消息、缺货', go: () => { window.location.hash = '#/influencer/inbox' }, warn: o.inbox > 0 },
    { label: '近 24 小时新申请', value: o.submitted_24h, go: () => onFilter('all') },
    { label: '近 24 小时已执行', value: o.fulfilled_24h, go: () => onFilter('fulfilled') },
    { label: '近 24 小时失败', value: o.failed_24h, go: () => onFilter('failed'), warn: o.failed_24h > 0 },
    { label: '近 24 小时驳回', value: o.rejected_24h, go: () => onFilter('rejected') },
  ]
  if (o.executing) tiles.push({ label: '停在执行中', value: o.executing, hint: '要人工核对', go: () => onFilter('executing'), warn: true })
  const copy = async () => {
    if (await copyText(o.brief)) toast.ok('已复制今日概览')
    else toast.err('复制没有成功，请手动选中文字复制')
  }
  return (
    <section className="overview" aria-label="今日概览">
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="label grow">今日概览</span>
        <button type="button" className="btn sm ghost" onClick={copy}><Copy />复制概览</button>
      </div>
      <div className="ov-tiles">
        {tiles.map((t) => (
          <button key={t.label} type="button" className={`ov-tile${t.warn ? ' warn' : ''}`} onClick={t.go}>
            <span className="ov-v num">{t.value}</span>
            <span className="ov-l">{t.label}</span>
            {t.hint && <span className="ov-h">{t.hint}</span>}
          </button>
        ))}
      </div>
    </section>
  )
}

export default function InfluencerPanel({ sub }) {
  if (sub === 'contacts') return <ContactsPage />
  if (sub === 'inbox') return <InboxPage />
  return <ApprovalsPage />
}

/** 左栏角标用：待审批 + 待处理的数量，每 30 秒问一次。 */
export function useInfluencerAttention() {
  const [count, setCount] = useState(0)
  useEffect(() => {
    let alive = true
    const ask = async () => {
      try {
        const d = await get('/api/influencer/counts')
        if (alive) setCount((d.pending || 0) + (d.inbox || 0))
      } catch { /* 控制台离线时不显示 */ }
    }
    ask()
    const timer = setInterval(ask, 30000)
    return () => { alive = false; clearInterval(timer) }
  }, [])
  return count
}

function useInfluencerStatus() {
  const [status, setStatus] = useState(null)
  const reload = useCallback(async () => {
    try { setStatus(await get('/api/influencer/status')) } catch { /* 页面本身会显示读取失败 */ }
  }, [])
  useEffect(() => { reload() }, [reload])
  return { status, reload }
}

function StatusNotes({ status }) {
  if (!status) return null
  return (
    <div className="stack" style={{ gap: 8, marginBottom: 14 }}>
      {status.mock && (
        <Banner title="模拟模式已打开（INFLUENCER_MOCK=1）">
          库存、出库单号、运单号和佣金都是假的：批准不会真的建单，也不会真的绑定佣金。
        </Banner>
      )}
      {!status.erp.ready && <Note kind="warn">聚水潭现在用不了：{status.erp.detail}。批准会失败，审批单会留在待审批。</Note>}
      {!status.xhs.ready && <Note kind="warn">小红书 CLI 现在用不了：{status.xhs.detail}。查不到达人数据，也绑不了佣金。</Note>}
    </div>
  )
}

// ── 审批单 ──────────────────────────────────────────────────────────

function ApprovalsPage() {
  const { status, reload: reloadStatus } = useInfluencerStatus()
  const [filter, setFilter] = useState('pending')
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    try {
      setData(await get('/api/influencer/approvals', { status: filter }))
      setErr('')
    } catch (e) { setErr(e.message) }
  }, [filter])
  useEffect(() => { load() }, [load])

  const refresh = () => { load(); reloadStatus() }
  const items = data?.items || []
  const counts = data?.counts || {}
  const tabs = FILTERS.map((f) => ({ ...f, count: f.value === 'all' ? undefined : counts[f.value] ?? 0 }))

  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="02" en="CREATOR" title="达人审批"
          sub="达人申请样品的审批单。通过后会在聚水潭建出库单、在小红书绑定佣金，批准前请核对收件信息和达人原话"
          actions={<button type="button" className="btn sm" onClick={refresh}><ArrowClockwise />刷新</button>}
        />
        <StatusNotes status={status} />
        <Overview key={data ? JSON.stringify(data.counts) : 'loading'} onFilter={setFilter} />
        {status?.inbox > 0 && (
          <Note kind="warn">有 {status.inbox} 条待处理事项（达人发来的消息、缺货），<a className="link" href="#/influencer/inbox">去看看</a>。</Note>
        )}
        <div className="filters">
          <Seg small label="状态" value={filter} onChange={setFilter} options={tabs} />
        </div>
        {err && <Note kind="need">读取失败：{err}</Note>}
        {data && !items.length && !err && (
          <Empty icon={Handshake} title={filter === 'pending' ? '没有待审批的申请' : '这里还没有审批单'}>
            {filter === 'pending'
              ? '名单里的达人在私聊里申请样品、收件信息补齐以后，审批单会出现在这里。'
              : '换个状态看看。'}
          </Empty>
        )}
        <div className="stack" style={{ gap: 14, marginTop: 12 }}>
          {items.map((p) => (
            <ApprovalCard key={p.packet_id} packet={p} maxRate={status?.max_commission_rate ?? 0.25} onChanged={refresh} />
          ))}
        </div>
      </div>
    </div>
  )
}

function ApprovalCard({ packet: p, maxRate, onChanged }) {
  const toast = useToast()
  const pending = p.status === PENDING
  const [xhs, setXhs] = useState(p.xhs_account || '')
  const [rate, setRate] = useState(percent(p.proposed_commission_rate))
  const [confirming, setConfirming] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  // 刷新后以服务器上的为准：达人可能在这期间补发了主页链接
  useEffect(() => { setXhs(p.xhs_account || '') }, [p.xhs_account])
  useEffect(() => { setRate(percent(p.proposed_commission_rate)) }, [p.proposed_commission_rate])

  const unqualified = p.qualification.startsWith('不达标')
  const qualKind = p.qualification.startsWith('达标') ? 'ok' : unqualified ? 'need' : 'warn'
  const rateOk = typeof rate === 'number' && rate > 0 && rate <= percent(maxRate)
  const cleared = !!p.personal_info_cleared_at
  let blocker = ''
  if (unqualified) blocker = '资质不达标，按规则不能寄样，请驳回'
  else if (!xhs.trim()) blocker = '缺小红书账号：请让达人补发主页链接，或者在上面填写'
  else if (!rateOk) blocker = `佣金要大于 0，不超过 ${percent(maxRate)}%`

  const act = async (action) => {
    setBusy(true)
    try {
      const body = action === 'approve'
        ? { packet_id: p.packet_id, action, xhs_account: xhs.trim(), commission_rate: rate / 100 }
        : { packet_id: p.packet_id, action, reason: reason.trim() }
      const d = await post('/api/influencer/approvals/action', body)
      toast.ok(action === 'approve' ? `已执行：出库单 ${d.order_sn}。记得把消息复制给达人` : '已驳回。记得把婉拒话术复制给达人', { ms: 6000 })
    } catch (e) {
      toast.err(e.message)
    } finally {
      setBusy(false)
      setConfirming(false)
      setRejecting(false)
      onChanged()
    }
  }

  return (
    <section className="ch card gate-card" aria-label={`审批单 ${p.packet_id}`}>
      <div className="card-head">
        <div className="stack" style={{ gap: 2 }}>
          <span className="eyebrow" aria-hidden="true">{p.packet_id}</span>
          <h2>{p.requested_product}<span className="dim"> · {p.influencer_name}</span></h2>
          <div className="sub">提交于 {when(p.created_at)}{pending && ` · ${timeLeft(p.expires_at)}`}</div>
        </div>
        <span className={`tag ${STATUS_TAG[p.status] || ''}`} style={{ marginLeft: 'auto' }}>{p.status_label}</span>
      </div>

      <dl className="kv">
        <dt>达人</dt>
        <dd>{p.influencer_name} <span className="mono dim">{p.influencer_id}</span></dd>
        <dt>资质</dt>
        <dd>
          <span className={`tag ${qualKind}`}>{p.qualification || '待核实'}</span>
          {p.follower_count != null && <span className="dim"> 粉丝 {p.follower_count}</span>}
        </dd>
        <dt>小红书账号</dt>
        <dd>
          {pending
            ? <TextInput mono value={xhs} onChange={setXhs} placeholder="主页链接或小红书号" aria-label="小红书账号" />
            : (p.xhs_account || <span className="dim">没有</span>)}
        </dd>
        <dt>申请</dt>
        <dd>{p.requested_product} · SKU <span className="mono">{p.sku_id}</span> · 库存 {p.stock_available} · 样品成本 {p.sample_cost != null ? `¥${p.sample_cost}` : <span className="dim">未知</span>}</dd>
        <dt>收件</dt>
        <dd>
          {cleared
            ? <span className="dim">已按规定清除（处理完 90 天后）</span>
            : <>{p.recipient_name} · <span className="mono">{p.recipient_phone}</span><br />{p.shipping_address}</>}
        </dd>
        <dt>佣金</dt>
        <dd>
          {pending
            ? <div style={{ maxWidth: 160 }}><NumberInput value={rate} onChange={setRate} invalid={!rateOk} suffix="%" aria-label="佣金" /></div>
            : `${percent(p.proposed_commission_rate)}%`}
        </dd>
      </dl>

      {!cleared && p.evidence && (
        <>
          <div className="label" style={{ marginTop: 12 }}>达人原话</div>
          <div className="quote">{p.evidence}</div>
        </>
      )}

      {pending && (
        <div style={{ marginTop: 12 }}>
          <Note kind="cy">
            批准后：在聚水潭建出库单（扣库存、出运单），在小红书给 <b>{xhs.trim() || '（先填小红书账号）'}</b> 绑定
            <b> {rateOk ? `${rate}%` : '—'} </b>的专属佣金。出库单建了就撤不回。
          </Note>
        </div>
      )}
      {p.status === 'EXECUTING' && <InterruptedExecution packet={p} onChanged={onChanged} />}
      {p.status === 'FULFILLED' && (
        <div style={{ marginTop: 12 }}>
          <Note kind="ok">
            出库单 <b className="mono">{p.order_sn}</b> · 运单 <b className="mono">{p.tracking_number || '—'}</b> · 佣金计划 <span className="mono">{p.commission_plan || '—'}</span>
          </Note>
        </div>
      )}
      {p.status === 'FAILED' && (
        <div style={{ marginTop: 12 }}>
          <Banner title={p.order_sn ? '出库单已建，但佣金没绑上：需要人工补绑' : '执行失败'}>{p.failure_reason}</Banner>
        </div>
      )}

      {p.notice && <NoticeBox status={p.status} text={p.notice} />}

      {p.events?.length > 0 && (
        <ol className="gate-events" aria-label="操作记录">
          {p.events.map((e, i) => (
            <li key={i}>
              <span className="num dim">{when(e.created_at)}</span>
              <span className="tag">{e.source}</span>
              <b>{e.action}</b>
              {e.detail && <span className="muted">{e.detail}</span>}
            </li>
          ))}
        </ol>
      )}

      {pending && (
        <div className="card-foot">
          <span className="f-hint grow">{blocker}</span>
          <button type="button" className="btn" disabled={busy} onClick={() => setRejecting(true)}><XCircle />驳回</button>
          <button type="button" className="btn primary chc" disabled={busy || !!blocker} onClick={() => setConfirming(true)}><CheckCircle />通过</button>
        </div>
      )}

      {confirming && (
        <Confirm
          title={`通过 ${p.packet_id}？`} confirmLabel="确认通过并执行" busy={busy}
          onCancel={() => setConfirming(false)} onConfirm={() => act('approve')}
        >
          <p>马上执行以下操作，出库单建了就撤不回：</p>
          <ul className="gate-steps">
            <li>聚水潭：给 {p.recipient_name}（{p.recipient_phone}）建【{p.requested_product}】样品出库单</li>
            <li>小红书：给 <span className="mono">{xhs.trim()}</span> 绑定 {rate}% 专属佣金</li>
          </ul>
        </Confirm>
      )}
      {rejecting && (
        <Dialog
          title={`驳回 ${p.packet_id}`} eyebrow="REJECT" onClose={() => setRejecting(false)}
          footer={<>
            <button type="button" className="btn" onClick={() => setRejecting(false)}>取消</button>
            <button type="button" className="btn danger" disabled={busy} onClick={() => act('reject')}>{busy ? '处理中…' : '驳回'}</button>
          </>}
        >
          <Field label="驳回原因" hint="只记在操作记录里，每周复盘用，不会告诉达人">
            <TextArea value={reason} onChange={setReason} rows={3} placeholder="例如：粉丝画像和产品不匹配" data-autofocus />
          </Field>
        </Dialog>
      )}
    </section>
  )
}

// 执行被中断（断电、强制退出）的单会一直停在执行中（B23）。运营到聚水潭和小红书核对后，
// 在这里记下结果；这一步不调用聚水潭和小红书。开始执行 10 分钟内不让标，免得和还在跑的执行撞上
function InterruptedExecution({ packet: p, onChanged }) {
  const toast = useToast()
  const [outcome, setOutcome] = useState(null)
  const [orderSn, setOrderSn] = useState('')
  const [tracking, setTracking] = useState('')
  const [plan, setPlan] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  if (!p.interrupted) {
    return (
      <div style={{ marginTop: 12 }}>
        <Note kind="cy">正在执行（{when(p.execution_started_at)} 开始）。如果 10 分钟后还停在这里，说明执行被中断了，到时可以在这里人工核对。</Note>
      </div>
    )
  }
  const fulfilled = outcome === 'fulfilled'
  const missing = fulfilled ? (!orderSn.trim() && '要填出库单号') : (!note.trim() && '要写明核对的结果')
  const submit = async () => {
    setBusy(true)
    try {
      await post('/api/influencer/approvals/action', {
        packet_id: p.packet_id, action: 'resolve', outcome,
        order_sn: orderSn, tracking_number: tracking, commission_plan: plan, note,
      })
      toast.ok(fulfilled ? '已标成已执行。记得把确认消息复制给达人' : '已标成失败', { ms: 6000 })
      setOutcome(null)
    } catch (e) {
      toast.err(e.message)
    } finally {
      setBusy(false)
      onChanged()
    }
  }
  return (
    <div style={{ marginTop: 12 }}>
      <Banner title="执行被中断了，需要人工核对">
        这张单{p.execution_started_at ? `从 ${when(p.execution_started_at)} 开始执行，` : ''}一直没有执行完。
        请到聚水潭看有没有这张单的出库单（外部单号是 <span className="mono">{p.packet_id}</span>），到小红书看佣金有没有绑上，再把结果记在这里。
        佣金没绑上的，先去小红书补绑。
      </Banner>
      <div className="card-foot">
        <span className="grow" />
        <button type="button" className="btn" onClick={() => setOutcome('failed')}><XCircle />核对后标成失败</button>
        <button type="button" className="btn primary chc" onClick={() => setOutcome('fulfilled')}><CheckCircle />核对后标成已执行</button>
      </div>
      {outcome && (
        <Dialog
          title={`${fulfilled ? '标成已执行' : '标成失败'}：${p.packet_id}`} eyebrow="CHECK" onClose={() => setOutcome(null)}
          footer={<>
            <span className="f-hint grow">{missing}</span>
            <button type="button" className="btn" onClick={() => setOutcome(null)}>取消</button>
            <button type="button" className={`btn ${fulfilled ? 'primary chc' : 'danger'}`} disabled={busy || !!missing} onClick={submit}>
              {busy ? '处理中…' : '确认'}
            </button>
          </>}
        >
          {fulfilled ? (
            <div className="stack" style={{ gap: 12 }}>
              <Field label="出库单号" hint="聚水潭里这张单的单号">
                <TextInput mono value={orderSn} onChange={setOrderSn} aria-label="出库单号" data-autofocus />
              </Field>
              <Field label="运单号" hint="还没有就先空着，确认消息里会写「快递单号出来后再发给老师」">
                <TextInput mono value={tracking} onChange={setTracking} aria-label="运单号" />
              </Field>
              <Field label="佣金计划" hint="小红书里的计划编号，可以不填">
                <TextInput mono value={plan} onChange={setPlan} aria-label="佣金计划" />
              </Field>
              <Field label="备注" hint="可以不填">
                <TextArea value={note} onChange={setNote} rows={2} aria-label="备注" />
              </Field>
            </div>
          ) : (
            <div className="stack" style={{ gap: 12 }}>
              <Field label="核对结果" hint="记在操作记录里，例如：聚水潭里没有这张单">
                <TextArea value={note} onChange={setNote} rows={3} aria-label="核对结果" data-autofocus />
              </Field>
              <Field label="出库单号" hint="出库单已经建了、但后面没做完的，填上单号">
                <TextInput mono value={orderSn} onChange={setOrderSn} aria-label="出库单号" />
              </Field>
            </div>
          )}
        </Dialog>
      )}
    </div>
  )
}

function NoticeBox({ status, text }) {
  const toast = useToast()
  const preview = status === PENDING
  const label = preview
    ? '批准后要发给达人的消息（运单号执行后填入）'
    : status === 'FULFILLED' ? '发给达人的确认消息' : '发给达人的婉拒话术'
  const copy = async () => {
    if (await copyText(text)) toast.ok('已复制，去微信里发给达人')
    else toast.err('复制没有成功，请手动选中文字复制')
  }
  return (
    <div className="notice-box">
      <div className="row">
        <span className="label grow">{label}</span>
        {!preview && <button type="button" className="btn sm" onClick={copy}><Copy />复制</button>}
      </div>
      <p className="notice-text">{text}</p>
    </div>
  )
}

// ── 待处理 ──────────────────────────────────────────────────────────

const INBOX_KIND = { message: ['达人消息', 'cy'], out_of_stock: ['缺货', 'warn'] }

function InboxPage() {
  const toast = useToast()
  const [handled, setHandled] = useState(false)
  const [items, setItems] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(async () => {
    try {
      const d = await get('/api/influencer/inbox', { handled: handled ? 1 : 0 })
      setItems(d.items || [])
      setErr('')
    } catch (e) { setErr(e.message) }
  }, [handled])
  useEffect(() => { load() }, [load])
  const done = async (id) => {
    try {
      await post('/api/influencer/inbox/action', { id })
      toast.ok('已标为处理完')
    } catch (e) { toast.err(e.message) }
    load()
  }
  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="02" en="CREATOR" title="待处理"
          sub="名单里的达人发来的不是索样的消息（机器人不替你回答），以及达人申请时缺货。在微信里处理完，再点「已处理」"
          actions={<button type="button" className="btn sm" onClick={load}><ArrowClockwise />刷新</button>}
        />
        <div className="filters">
          <Seg small label="状态" value={handled ? 'done' : 'open'} onChange={(v) => setHandled(v === 'done')}
            options={[{ value: 'open', label: '待处理' }, { value: 'done', label: '已处理' }]} />
        </div>
        {err && <Note kind="need">读取失败：{err}</Note>}
        {items && !items.length && !err && (
          <Empty icon={ChatCircleDots} title={handled ? '还没有处理过的事项' : '没有待处理的事项'}>
            {handled ? '' : '达人发来不是索样的消息、或者申请时缺货，会出现在这里。'}
          </Empty>
        )}
        {items && items.length > 0 && (
          <div className="todo-list" style={{ marginTop: 12 }}>
            {items.map((it) => {
              const [label, kind] = INBOX_KIND[it.kind] || [it.kind, '']
              return (
                <div key={it.id} className={`todo-row${handled ? ' s-completed' : ''}`} aria-label={`待处理事项 ${it.id}`}>
                  <span className="td-no num">#{it.id}</span>
                  <div className="td-main">
                    <div className="row wrap" style={{ gap: 6 }}>
                      <span className={`tag ${kind}`}>{label}</span>
                      <b>{it.display_name || it.chat_id}</b>
                      {it.display_name && <span className="mono dim" style={{ fontSize: 'var(--fs-xs)' }}>{it.chat_id}</span>}
                    </div>
                    <p className="td-text">{it.content}</p>
                    <div className="td-meta">
                      <span>{when(it.created_at)}</span>
                      {it.auto_replied && <span>机器人已回复："收到老师的消息啦，我们核对一下再回复"</span>}
                      {it.handled_at && <span>处理于 {when(it.handled_at)}</span>}
                    </div>
                  </div>
                  <div className="td-act">
                    {!handled && <button type="button" className="btn sm" onClick={() => done(it.id)}><CheckCircle />已处理</button>}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ── 达人名单 ────────────────────────────────────────────────────────

const EMPTY_FORM = { chat_id: '', display_name: '', xhs_account: '', note: '' }

function ContactsPage() {
  const toast = useToast()
  const [contacts, setContacts] = useState(null)
  const [recent, setRecent] = useState(null)
  const [err, setErr] = useState('')
  const [form, setForm] = useState(EMPTY_FORM)
  const [editing, setEditing] = useState(null)

  const load = useCallback(async () => {
    try {
      const [c, r] = await Promise.all([get('/api/influencer/contacts'), get('/api/influencer/recent-chats')])
      setContacts(c.items || [])
      setRecent(r.items || [])
      setErr('')
    } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => { load() }, [load])

  const save = async (body, done) => {
    try {
      await post('/api/influencer/contacts/action', body)
      toast.ok(done)
      await load()
      return true
    } catch (e) {
      toast.err(e.message)
      return false
    }
  }
  const add = async () => {
    // 只提交填了的项：chat_id 已经在名单里时，留空的项不能把原来的值清掉
    const body = { chat_id: form.chat_id.trim(), enabled: true }
    for (const name of ['display_name', 'xhs_account', 'note']) {
      if (form[name].trim()) body[name] = form[name].trim()
    }
    if (await save(body, '已加入名单')) setForm(EMPTY_FORM)
  }
  const field = (name) => ({ value: form[name], onChange: (v) => setForm((f) => ({ ...f, [name]: v })) })

  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="02" en="CREATOR" title="达人名单"
          sub="只有名单里、并且启用的私聊才会进入索样流程；其他私聊照常由机器人聊天回复"
          actions={<button type="button" className="btn sm" onClick={load}><ArrowClockwise />刷新</button>}
        />
        {err && <Note kind="need">读取失败：{err}</Note>}

        <Card title="加入名单" sub="chat_id 是私聊的会话 ID。不知道的话，在下面「最近私聊」里点「设为达人」">
          <div className="form-grid">
            <Field label="chat_id"><TextInput mono placeholder="例如 wxid_xxx" aria-label="chat_id" {...field('chat_id')} /></Field>
            <Field label="显示名"><TextInput placeholder="可不填，机器人会用微信昵称" aria-label="显示名" {...field('display_name')} /></Field>
            <Field label="小红书账号"><TextInput mono placeholder="主页链接或小红书号（可不填）" aria-label="小红书账号" {...field('xhs_account')} /></Field>
            <Field label="备注"><TextInput placeholder="可不填" aria-label="备注" {...field('note')} /></Field>
          </div>
          <div className="card-foot">
            <span className="grow" />
            <button type="button" className="btn primary chc" disabled={!form.chat_id.trim()} onClick={add}><UserPlus />加入名单</button>
          </div>
        </Card>

        <Card title="名单" sub={contacts ? `${contacts.filter((c) => c.enabled).length} 位启用，${contacts.length} 位在名单里` : ''}>
          {contacts && !contacts.length && <Empty icon={Handshake} title="名单还是空的">机器人现在不拦任何私聊。</Empty>}
          {contacts && contacts.length > 0 && (
            <table className="table">
              <thead><tr><th>显示名</th><th>chat_id</th><th>小红书账号</th><th>粉丝 / 点赞</th><th>启用</th><th /></tr></thead>
              <tbody>
                {contacts.map((c) => (
                  <tr key={c.chat_id}>
                    <td>{c.display_name || <span className="dim">（没有）</span>}{c.note && <div className="f-hint">{c.note}</div>}</td>
                    <td className="mono dim" style={{ fontSize: 'var(--fs-xs)' }}>{c.chat_id}</td>
                    <td className="mono" style={{ fontSize: 'var(--fs-xs)', wordBreak: 'break-all' }}>{c.xhs_account || <span className="dim">—</span>}</td>
                    <td className="num">{c.follower_count != null ? `${c.follower_count} / ${c.recent_like_count ?? '—'}` : <span className="dim">—</span>}</td>
                    <td>
                      <Switch
                        checked={c.enabled} label={`${c.enabled ? '停用' : '启用'} ${c.display_name || c.chat_id}`}
                        onChange={(on) => save({ chat_id: c.chat_id, enabled: on }, on ? '已启用' : '已停用：这位达人的私聊交回给机器人聊天')}
                      />
                    </td>
                    <td><button type="button" className="btn sm ghost" onClick={() => setEditing(c)}><PencilSimple />编辑</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="最近私聊" sub="取自机器人收到的消息，群聊和系统会话不在这里">
          {recent && !recent.length && <Empty title="还没有私聊记录">机器人收到私聊消息以后，联系人会出现在这里。</Empty>}
          {recent && recent.length > 0 && (
            <div className="list">
              {recent.map((r) => (
                <div key={r.chat_id} className="list-row">
                  <span className="grow ellipsis">{r.name || r.chat_id} <span className="mono dim" style={{ fontSize: 'var(--fs-xs)' }}>{r.chat_id}</span></span>
                  <span className="dim nowrap" style={{ fontSize: 'var(--fs-xs)' }}>{when(r.last_ts)} · {r.message_count} 条</span>
                  {r.in_list && r.enabled && <span className="tag ok">已在名单</span>}
                  {r.in_list && !r.enabled && (
                    <button type="button" className="btn sm" onClick={() => save({ chat_id: r.chat_id, enabled: true }, '已重新启用')}>重新启用</button>
                  )}
                  {!r.in_list && (
                    <button type="button" className="btn sm" onClick={() => save({ chat_id: r.chat_id, display_name: r.name, enabled: true }, '已设为达人')}>
                      <UserPlus />设为达人
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>

        {editing && (
          <EditContact contact={editing} onClose={() => setEditing(null)} onSave={async (body) => { if (await save(body, '已保存')) setEditing(null) }} />
        )}
      </div>
    </div>
  )
}

function EditContact({ contact, onClose, onSave }) {
  const [values, setValues] = useState({
    display_name: contact.display_name || '', xhs_account: contact.xhs_account || '', note: contact.note || '',
  })
  const field = (name) => ({ value: values[name], onChange: (v) => setValues((s) => ({ ...s, [name]: v })) })
  return (
    <Dialog
      title={`编辑 ${contact.display_name || contact.chat_id}`} eyebrow="CREATOR" onClose={onClose}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>取消</button>
        <button type="button" className="btn primary chc" onClick={() => onSave({ chat_id: contact.chat_id, ...values })}>保存</button>
      </>}
    >
      <div className="stack">
        <Field label="显示名"><TextInput aria-label="显示名" {...field('display_name')} /></Field>
        <Field label="小红书账号"><TextInput mono aria-label="小红书账号" {...field('xhs_account')} /></Field>
        <Field label="备注"><TextInput aria-label="备注" {...field('note')} /></Field>
      </div>
    </Dialog>
  )
}
