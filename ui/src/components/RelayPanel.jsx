import { useState } from 'react'
import { ArrowClockwise, CheckCircle, XCircle, MinusCircle, X, CaretDown, CaretRight } from '@phosphor-icons/react'
import { post } from '../api'
import { useStatus } from '../store/status'
import { useToast } from '../store/toast'
import { useRelay, JOB_STATES, fmtClock, fmtSeconds, macState } from '../lib/relay'
import { PageHead, Card, Note, Empty, Confirm } from './ui'
import { AdvancedFields } from './AdvancedPanel'

// 中转代理: the bot as a relay. "cc …" in an allowed private chat is queued here,
// the Mac worker (tools/claude_relay/worker.py) takes it to Claude Code and the
// result goes back to the chat. This page shows the queue, whether the Mac is
// there, and the relay's settings.

export default function RelayPanel() {
  const { data, err, reload } = useRelay(5000)
  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="02" en="RELAY" title="中转代理"
          sub="你在微信私聊里发「cc 任务」，Mac 上的 Claude Code 执行，结果发回微信"
          actions={<button type="button" className="btn sm" onClick={reload}><ArrowClockwise />刷新</button>}
        />
        {err && <Note kind="need">{err}</Note>}
        <div className="dash-grid" style={{ marginTop: 0 }}>
          <StateCard d={data} />
          <MacCard d={data} />
        </div>
        <JobsCard d={data} reload={reload} />
        <div className="stack" style={{ gap: 12, marginTop: 16 }}>
          <AdvancedFields only={['relay']} onSaved={reload} />
        </div>
      </div>
    </div>
  )
}

function Row({ label, ok, value }) {
  return (
    <div className="check-row">
      {ok === true ? <CheckCircle weight="fill" className="ok" /> : ok === false ? <XCircle weight="fill" className="bad" /> : <MinusCircle className="na" />}
      <span className="cr-l">{label}</span>
      <span className="cr-v">{value}</span>
    </div>
  )
}

function StateCard({ d }) {
  const { running } = useStatus()
  const [mac, macOk] = macState(d)
  const counts = d?.counts || {}
  const port = d?.address?.[1]
  return (
    <Card title="现在的状态" eyebrow="STATE">
      <Row label="机器人" ok={running ? true : undefined} value={running ? '运行中' : '没有运行（在右上角启动）'} />
      <Row
        label="中转端口"
        ok={d?.listening ? true : running ? false : undefined}
        value={d?.listening ? `已打开 ${d.address[0]}:${port}` : running ? '没打开：中转关着，或端口被占用（看运行日志）' : '机器人启动后打开'}
      />
      <Row label="Mac worker" ok={macOk} value={mac} />
      <Row
        label="任务"
        value={['pending', 'running', 'done', 'failed', 'lost', 'cancelled']
          .filter((k) => counts[k]).map((k) => `${JOB_STATES[k][0]} ${counts[k]}`).join(' · ') || '还没有任务'}
      />
    </Card>
  )
}

function MacCard({ d }) {
  const port = d?.address?.[1] || 8790
  const ips = d?.lan_addresses?.length ? d.lan_addresses : ['<这台电脑的局域网 IP>']
  return (
    <Card title="Mac 端怎么接" eyebrow="MAC">
      <ol className="mac-steps" style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <li>
          在 Mac 的 <span className="mono">~/.claude-relay/config.json</span> 里，<span className="mono">relay_url</span> 填
          {ips.map((ip) => <div key={ip}><span className="mono" style={{ userSelect: 'all' }}>http://{ip}:{port}</span></div>)}
        </li>
        <li>把下面设置里的 token 写进 Mac 的 <span className="mono">~/.claude-relay/token</span>（只放这一行）。</li>
        <li>运行 <span className="mono">python3 tools/claude_relay/worker.py --check</span>，能连上、能找到 claude 再继续。</li>
        <li>常驻：双击 <span className="mono">tools/claude_relay/start-worker.command</span>（可加进登录项）。</li>
      </ol>
      <div style={{ marginTop: 12 }}>
        <Note>Windows 防火墙要放行这个端口（只放行局域网）。微信里发 <span className="mono">cc 帮助</span> 看全部指令。</Note>
      </div>
    </Card>
  )
}

function JobsCard({ d, reload }) {
  const [open, setOpen] = useState(null)
  const [asking, setAsking] = useState(null)
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const jobs = d?.jobs || []
  const cancel = async (job) => {
    setBusy(true)
    try {
      const r = await post('/api/relay/cancel', { id: job.id })
      toast.ok(r.outcome === 'requested' ? `#${job.id} 正在执行，已通知 Mac 停下` : `#${job.id} 已取消`)
      setAsking(null)
      reload()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  return (
    <Card title="任务" eyebrow="JOBS" className="relay-jobs" sub={jobs.length ? `最近 ${jobs.length} 个` : undefined}>
      {!d && <Empty title="正在读取…" />}
      {d && !jobs.length && <Empty title="还没有任务">打开中转、启动机器人后，在微信私聊里给机器人发「cc 只回复：收到」试试。</Empty>}
      {jobs.length > 0 && (
        <div className="scroll" style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead>
              <tr><th style={{ width: 28 }} /><th>编号</th><th>状态</th><th>任务</th><th>项目</th><th>时间</th><th>用时</th><th /></tr>
            </thead>
            <tbody>
              {jobs.map((j) => {
                const [label, kind] = JOB_STATES[j.status] || [j.status, 'pend']
                const expanded = open === j.id
                const cancellable = d.running && (j.status === 'pending' || j.status === 'running') && !j.cancel_requested
                return [
                  <tr key={j.id}>
                    <td>
                      <button type="button" className="icon-btn sm" aria-label={expanded ? '收起结果' : '展开结果'} onClick={() => setOpen(expanded ? null : j.id)}>
                        {expanded ? <CaretDown /> : <CaretRight />}
                      </button>
                    </td>
                    <td className="num">#{j.id}</td>
                    <td><span className={`tag ${kind}`}>{label}{j.cancel_requested && j.status === 'running' ? ' · 停止中' : ''}</span></td>
                    <td className="ellipsis" style={{ maxWidth: 360 }} title={j.text}>
                      {j.kind === 'prm' && <span className="tag cy" title="已登记达人的消息：草稿进「待审批」，不会自动回复">达人草稿</span>} {j.text}
                    </td>
                    <td className="mono">{j.project || '—'}</td>
                    <td className="num">{fmtClock(j.created_at)}</td>
                    <td className="num">{fmtSeconds(j.duration_s) || (j.status === 'running' && j.claimed_at ? '…' : '')}</td>
                    <td>{cancellable && <button type="button" className="btn sm ghost" onClick={() => setAsking(j)}><X />取消</button>}</td>
                  </tr>,
                  expanded && (
                    <tr key={`${j.id}-detail`} className="job-detail">
                      <td />
                      <td colSpan={7}>
                        <div className="stack" style={{ gap: 6 }}>
                          <div><b>任务：</b><span style={{ whiteSpace: 'pre-wrap' }}>{j.text}</span></div>
                          {j.result && <div><b>结果：</b><span style={{ whiteSpace: 'pre-wrap' }}>{j.result}</span></div>}
                          {j.error && <div className="f-err"><b>错误：</b>{j.error}</div>}
                          {!j.result && !j.error && <div className="dim">还没有结果。</div>}
                        </div>
                      </td>
                    </tr>
                  ),
                ]
              })}
            </tbody>
          </table>
        </div>
      )}
      {asking && (
        <Confirm
          title={`取消 #${asking.id}？`} confirmLabel="取消任务" cancelLabel="不取消" danger busy={busy}
          onConfirm={() => cancel(asking)} onCancel={() => setAsking(null)}
        >
          {asking.status === 'running'
            ? 'Mac 正在执行这个任务。取消后 Mac 会在下一次心跳（最多 30 秒）时停下，微信里会收到「已取消」。'
            : '这个任务还在排队，取消后不会执行。'}
        </Confirm>
      )}
    </Card>
  )
}
