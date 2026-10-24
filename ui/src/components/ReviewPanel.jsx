import { useState } from 'react'
import { ArrowClockwise, PaperPlaneRight, PencilSimple, X, Paperclip, WarningCircle } from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import { usePrm, PACKET_STATES, decide, setupProblems, fmtSize } from '../lib/prm'
import { PageHead, Card, Note, Empty, Confirm, TextArea } from './ui'

// 待审批: what the Mac drafted for the registered creators. Nothing reaches a
// creator until somebody here clicks 发送 (or edits and sends). A new message
// from the creator makes the waiting draft out of date; a newer one follows.

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
        {!data && <Empty title="正在读取…" />}
        {data && !pending.length && (
          <Empty title="现在没有要审批的草稿">已登记的达人发来消息后，Mac 写好的回复会出现在这里。</Empty>
        )}
        <div className="stack" style={{ gap: 12 }}>
          {pending.map((p) => <DraftCard key={p.id} p={p} canSend={!!data?.can_send} onDone={reload} />)}
        </div>
        <RecentCard d={data} />
      </div>
    </div>
  )
}

function DraftCard({ p, canSend, onDone }) {
  const toast = useToast()
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(p.text)
  const [busy, setBusy] = useState(false)
  const [dropping, setDropping] = useState(false)
  const waiting = p.status !== 'pending'
  const act = async (action, body) => {
    setBusy(true)
    try {
      await decide(p.id, action, body)
      toast.ok(action === 'drop' ? '这条不发了' : '已确认，正在发送')
      setEditing(false)
      setDropping(false)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  const [label, kind] = PACKET_STATES[p.status] || [p.status, 'pend']
  const slots = Object.entries(p.slots || {})
  return (
    <Card
      title={`${p.party_name} · ${p.kind_label}`}
      eyebrow={`DRAFT #${p.id}`}
      sub={`${fmtClock(p.created_at)} 写好${p.stage_proposal_label ? ` · 建议阶段：${p.stage_proposal_label}` : ''}`}
      actions={<span className={`tag ${kind}`}>{label}</span>}
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
        {editing
          ? <TextArea value={text} onChange={setText} rows={5} aria-label="修改要发的内容" />
          : <div className="prm-text" style={{ whiteSpace: 'pre-wrap' }}>{p.text || <span className="dim">（只发附件，没有文字）</span>}</div>}
        {p.legend?.length > 0 && (
          <div className="f-hint">
            {p.legend.map((l) => `${l.token} = ${l.masked}`).join('；')}（发送时换成真实号码；改的时候保留占位符）
          </div>
        )}
        {p.attachments?.length > 0 && (
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {p.attachments.map((a) => <span key={a.name} className="tag"><Paperclip />{a.name} {fmtSize(a.size)}</span>)}
          </div>
        )}
        {!waiting && (
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
          这条草稿不会发给达人。达人再发消息时，会有新的草稿。
        </Confirm>
      )}
    </Card>
  )
}

function RecentCard({ d }) {
  const recent = d?.recent || []
  if (!recent.length) return null
  return (
    <Card title="最近处理过的" eyebrow="HISTORY" className="prm-recent" sub={`最近 ${recent.length} 条`}>
      <div className="scroll" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead><tr><th>编号</th><th>达人</th><th>类型</th><th>结果</th><th>内容</th><th>时间</th></tr></thead>
          <tbody>
            {recent.map((p) => {
              const [label, kind] = PACKET_STATES[p.status] || [p.status, 'pend']
              const shown = p.final_text ?? p.text
              return (
                <tr key={p.id}>
                  <td className="num">#{p.id}</td>
                  <td>{p.party_name}</td>
                  <td>{p.kind_label}</td>
                  <td>
                    <span className={`tag ${kind}`} title={p.outcome === 'sent_unconfirmed' ? '按键已发出，还没从微信里回读确认' : undefined}>{label}{p.edited ? ' · 改过' : ''}</span>
                    {p.error && <div className="f-err">{p.error}</div>}
                  </td>
                  <td className="ellipsis" style={{ maxWidth: 360 }} title={shown}>{shown || '—'}</td>
                  <td className="num">{fmtClock(p.sent_at || p.decided_at || p.created_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
