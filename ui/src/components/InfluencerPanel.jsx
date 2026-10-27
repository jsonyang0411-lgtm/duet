import { useCallback, useEffect, useState } from 'react'
import { Handshake, ArrowClockwise, CheckCircle, UserPlus, PencilSimple, ChatCircleDots } from '@phosphor-icons/react'
import { get, post } from '../api'
import { useToast } from '../store/toast'
import { PageHead, Card, Field, TextInput, Switch, Seg, Empty, Note, Dialog } from './ui'

// 达人名单（src/influencer/，说明见 handoff.md）：名单、待处理两个子页。
// 名单里启用的达人，私聊由 PRM 起草、在「待审批」审核；寄样单、专属佣金也在「待审批」里用平台动作办
// （jst.sample_order、dy.designated_commission，2026-10-27 起）。提醒只做在控制台里（D14）：
// 左栏「达人名单」上的数字 = 待处理的数量。

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '')

export default function InfluencerPanel({ sub }) {
  if (sub === 'inbox') return <InboxPage />
  return <ContactsPage />
}

/** 左栏角标用：待处理的数量，每 30 秒问一次。 */
export function useInfluencerAttention() {
  const [count, setCount] = useState(0)
  useEffect(() => {
    let alive = true
    const ask = async () => {
      try {
        const d = await get('/api/influencer/counts')
        if (alive) setCount(d.inbox || 0)
      } catch { /* 控制台离线时不显示 */ }
    }
    ask()
    const timer = setInterval(ask, 30000)
    return () => { alive = false; clearInterval(timer) }
  }, [])
  return count
}

// ── 待处理 ──────────────────────────────────────────────────────────

const INBOX_KIND = {
  message: ['达人消息', 'cy'], out_of_stock: ['缺货', 'warn'], commission: ['要求提佣金', 'warn'],
  draft: ['回复草稿（没发出，等审核）', 'pend'],
}
const AUTO_REPLY = {
  message: '机器人已回复："收到老师的消息啦，我们核对一下再回复"',
  commission: '佣金话术已回复，或已放进「待审批」等审核（佣金上限不变）',
}

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
          sub="Mac 端机器人想回却没发出的话（等你审核后自己在微信里处理），以及以前留下的事项。处理完再点「已处理」"
          actions={<button type="button" className="btn sm" onClick={load}><ArrowClockwise />刷新</button>}
        />
        <div className="filters">
          <Seg small label="状态" value={handled ? 'done' : 'open'} onChange={(v) => setHandled(v === 'done')}
            options={[{ value: 'open', label: '待处理' }, { value: 'done', label: '已处理' }]} />
        </div>
        {err && <Note kind="need">读取失败：{err}</Note>}
        {items && !items.length && !err && (
          <Empty icon={ChatCircleDots} title={handled ? '还没有处理过的事项' : '没有待处理的事项'}>
            {handled ? '' : '机器人想回却没发出的话会出现在这里。'}
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
                      {it.auto_replied && <span>机器人已自动回复</span>}
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

const EMPTY_FORM = { chat_id: '', display_name: '', platform: 'DY', platform_no: '', note: '' }
const PLATFORM_OPTIONS = [{ value: 'DY', label: '抖音' }, { value: 'XHS', label: '小红书' }]
const PLATFORM_LABEL = { DY: '抖音', XHS: '小红书' }

// 达人资料（tiers.py）：按名单上的号码从聚水潭查的档位、主播类型、销售额；名单同步时在后台更新
function DarenProfile({ c }) {
  if (!c.platform_no) return <span className="dim">没填号码</span>
  if (!c.tier) return <span className="dim">{c.profile_note || '等后台查'}</span>
  return (
    <span>
      <span className={`tag ${c.tier === '无销售数据' ? 'need' : 'ok'}`}>{c.tier}</span>
      {c.channel && <span className="dim"> · {c.channel}</span>}
      {c.level != null && <span className="dim"> · LV{c.level}</span>}
      <div className="f-hint">{c.sales_label}</div>
    </span>
  )
}

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
    for (const name of ['display_name', 'platform_no', 'note']) {
      if (form[name].trim()) body[name] = form[name].trim()
    }
    if (body.platform_no) body.platform = form.platform
    if (await save(body, '已加入名单')) setForm(EMPTY_FORM)
  }
  const field = (name) => ({ value: form[name], onChange: (v) => setForm((f) => ({ ...f, [name]: v })) })

  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="02" en="CREATOR" title="达人名单"
          sub="名单里、并且启用的达人，私聊由 Claude 起草、你审核后发出；名单以外的私聊不回。填上抖音号 / 小红书号，系统会从聚水潭查档位和主播类型"
          actions={<button type="button" className="btn sm" onClick={load}><ArrowClockwise />刷新</button>}
        />
        {err && <Note kind="need">读取失败：{err}</Note>}

        <Card title="加入名单" sub="chat_id 是私聊的会话 ID。不知道的话，在下面「最近私聊」里点「设为达人」">
          <div className="form-grid">
            <Field label="chat_id"><TextInput mono placeholder="例如 wxid_xxx" aria-label="chat_id" {...field('chat_id')} /></Field>
            <Field label="显示名"><TextInput placeholder="可不填，机器人会用微信昵称" aria-label="显示名" {...field('display_name')} /></Field>
            <Field label="平台"><Seg small label="平台" value={form.platform} onChange={(v) => setForm((f) => ({ ...f, platform: v }))} options={PLATFORM_OPTIONS} /></Field>
            <Field label="抖音号 / 小红书号" hint="填了以后自动从聚水潭查档位、主播类型（可以先不填）"><TextInput mono placeholder="例如 test_daren01" aria-label="抖音号或小红书号" {...field('platform_no')} /></Field>
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
              <thead><tr><th>显示名</th><th>chat_id</th><th>平台号码</th><th>档位 / 主播类型</th><th>启用</th><th /></tr></thead>
              <tbody>
                {contacts.map((c) => (
                  <tr key={c.chat_id}>
                    <td>{c.display_name || <span className="dim">（没有）</span>}{c.note && <div className="f-hint">{c.note}</div>}</td>
                    <td className="mono dim" style={{ fontSize: 'var(--fs-xs)' }}>{c.chat_id}</td>
                    <td className="mono" style={{ fontSize: 'var(--fs-xs)', wordBreak: 'break-all' }}>
                      {c.platform_no ? `${PLATFORM_LABEL[c.platform] || c.platform} ${c.platform_no}` : (c.xhs_account ? `小红书 ${c.xhs_account}` : <span className="dim">—</span>)}
                    </td>
                    <td><DarenProfile c={c} /></td>
                    <td>
                      <Switch
                        checked={c.enabled} label={`${c.enabled ? '停用' : '启用'} ${c.display_name || c.chat_id}`}
                        onChange={(on) => save({ chat_id: c.chat_id, enabled: on }, on ? '已启用' : '已停用：这位达人的私聊不再起草回复')}
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
    display_name: contact.display_name || '', platform: contact.platform || 'DY', platform_no: contact.platform_no || '',
    xhs_account: contact.xhs_account || '', note: contact.note || '',
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
        <Field label="平台"><Seg small label="平台" value={values.platform} onChange={(v) => setValues((s) => ({ ...s, platform: v }))} options={PLATFORM_OPTIONS} /></Field>
        <Field label="抖音号 / 小红书号" hint="改了号码会马上重查档位"><TextInput mono aria-label="抖音号或小红书号" {...field('platform_no')} /></Field>
        <Field label="小红书主页（旧字段，选填）"><TextInput mono aria-label="小红书账号" {...field('xhs_account')} /></Field>
        <Field label="备注"><TextInput aria-label="备注" {...field('note')} /></Field>
      </div>
    </Dialog>
  )
}
