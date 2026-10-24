import { useState } from 'react'
import { ArrowClockwise, UserPlus, Prohibit, ArrowCounterClockwise } from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import { usePrm, partyAction, setupProblems } from '../lib/prm'
import { PageHead, Card, Note, Empty, Field, TextInput, Select } from './ui'
import { AdvancedFields } from './AdvancedPanel'

// 合作单: the creators whose private messages get drafted replies (PRM_COPILOT).
// Registering one here means the creator agreed to be contacted on WeChat; their
// messages then go, silently, to the Mac for a draft that waits in 待审批.

export default function DealsPanel() {
  const { data, err, reload } = usePrm(6000)
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
        <RegisterCard d={data} onDone={reload} />
        <PartiesCard d={data} onDone={reload} />
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
  const [busy, setBusy] = useState(false)
  const candidates = d?.candidates || []
  const pick = (value) => {
    const c = candidates.find((x) => x.wxid === value)
    if (c) { setWxid(c.wxid); setName((n) => n || c.name) }
  }
  const add = async () => {
    setBusy(true)
    try {
      await partyAction({ action: 'add', wxid: wxid.trim(), name: name.trim(), note: note.trim() })
      toast.ok(`已登记 ${name.trim()}`)
      setWxid(''); setName(''); setNote('')
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  return (
    <Card title="登记达人" eyebrow="REGISTER" className="prm-register">
      <div className="stack" style={{ gap: 10 }}>
        {candidates.length > 0 && (
          <Field label="从最近的私聊里选" hint="机器人号最近有私聊的人（达人副驾驶打开、机器人运行后才有这个列表）">
            <Select value="" onChange={pick} aria-label="从最近的私聊里选"
              options={[{ value: '', label: '选一个…' }, ...candidates.map((c) => ({ value: c.wxid, label: `${c.name}（${c.wxid}）` }))]} />
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
        </div>
        <Note>登记表示这位达人已同意由我们在微信上联系。登记后，对方的私聊不会再收到自动回复：机器人不回任何话，只在「待审批」里给运营一份草稿。</Note>
        <div><button type="button" className="btn primary" disabled={busy || !wxid.trim() || !name.trim()} onClick={add}><UserPlus />登记</button></div>
      </div>
    </Card>
  )
}

function PartiesCard({ d, onDone }) {
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
            <thead><tr><th>达人</th><th>wxid</th><th>阶段</th><th>已确认的信息</th><th>要注意</th><th>最近消息</th><th /></tr></thead>
            <tbody>
              {parties.map((p) => {
                const deal = p.deal || {}
                const slots = Object.entries(deal.slots || {})
                return (
                  <tr key={p.id} className={p.active ? '' : 'dim'}>
                    <td><b>{p.name}</b>{p.note && <div className="dim">{p.note}</div>}{!p.active && <span className="tag off">已停用</span>}</td>
                    <td className="mono">{p.wxid}</td>
                    <td>
                      {deal.stage_label || '—'}
                      {deal.stage_proposal && deal.stage_proposal !== deal.stage && <div className="dim">AI 建议：{deal.stage_proposal_label}</div>}
                    </td>
                    <td className="ellipsis" style={{ maxWidth: 280 }} title={slots.map(([k, v]) => `${k}：${v}`).join('\n')}>
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
