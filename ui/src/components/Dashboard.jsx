import { useCallback, useEffect, useState } from 'react'
import { Play, Stop, Stethoscope, Key, ArrowRight, ArrowClockwise, CheckCircle, XCircle, MinusCircle } from '@phosphor-icons/react'
import { get } from '../api'
import { useStatus, fmtDuration, WECHAT_BACKEND_NAMES, AI_BACKEND_NAMES } from '../store/status'
import { useConfig } from '../store/config'
import { useKeyExtraction, diagnose } from '../lib/keyExtraction'
import { navigate } from '../lib/router'
import { PageHead, Banner, Barcode, Empty, Note } from './ui'
import { OutcomeTag, fmtTime } from './DecisionsPanel'

// How each WeChat backend reads messages (shown under the title).
const BACKEND_HOW = {
  wcdb: '直接读取 Windows 微信的本地数据库，用键盘操作微信窗口发消息',
  mac_hybrid: '用 WeFlow 读取 macOS 微信的数据，用界面操作发消息',
  mac_ui: '只靠 macOS 微信的界面读取和发送消息',
}

export default function Dashboard() {
  const st = useStatus()
  const { conn, data, running } = st
  const keyMissing = !!data?.error && String(data.error).includes('KEY_MISSING')

  let title = '机器人已停止', code = 'STOP', stateText = '已停止'
  if (conn === 'connecting') { title = '正在连接…'; code = '----'; stateText = '连接中' }
  else if (conn === 'offline') { title = '控制台服务离线'; code = 'LOST'; stateText = '离线' }
  else if (running) { title = '机器人运行中'; code = 'RUN'; stateText = '运行中' }

  // The status only names the backends while the bot runs; otherwise show the saved settings.
  const { base } = useConfig()
  const wb = (running && data?.wechat_backend) || base?.wechat_backend
  const ab = (running && data?.ai_backend) || base?.ai_backend

  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead no="02" en="STATUS" title="运行状态" sub="机器人现在在做什么，以及最近每条消息是怎么处理的" />

        <section className="ch xl hero">
          <div className="hero-stub slab" aria-hidden="true">
            <span className="hs-k">BOT</span>
            <span className="hs-code">{code}</span>
            <span className="hs-state"><i />{stateText}</span>
            <span className="hs-bar"><Barcode seed={`webot-${code}`} /></span>
            <span className="hs-corner" />
          </div>
          <div className="hero-body">
            <div className="row wrap" style={{ gap: 6 }}>
              <span className="tag">微信 · {WECHAT_BACKEND_NAMES[wb] || wb || '未知'}</span>
              <span className="tag">AI · {AI_BACKEND_NAMES[ab] || ab || '未知'}</span>
            </div>
            <h2 className="hero-title">{title}</h2>
            <p className="hero-desc">
              {conn === 'offline'
                ? '控制台连不上 webot 的后台服务。webot 可能已经退出，重新打开 webot 即可。'
                : BACKEND_HOW[wb] ? `${BACKEND_HOW[wb]}。` : '启动后开始读取微信消息并按设置回复。'}
            </p>
            <div className="hero-stats">
              <Stat k="MSG" label="已处理消息" value={data ? (data.messages_processed ?? 0) : '—'} />
              <Stat k="UPTIME" label="运行时长" value={running ? fmtDuration(data?.uptime_sec) : '—'} />
              <Stat k="LAST CALL" label="最近一次模型调用" value={data?.last_api_call_time || '还没有'} />
            </div>
            {conn === 'online' && (
              <div className="row" style={{ marginTop: 16 }}>
                {running
                  ? <button type="button" className="btn" onClick={st.stop} disabled={!!st.busy}><Stop weight="fill" />{st.busy === 'stop' ? '正在停止…' : '停止机器人'}</button>
                  : <button type="button" className="btn primary" onClick={st.start} disabled={!!st.busy}><Play weight="fill" />{st.busy === 'start' ? '正在启动…' : '启动机器人'}</button>}
                <span className="f-hint">{running ? '停止后不再读取和回复消息' : '控制台里保存的设置，启动时生效'}</span>
              </div>
            )}
          </div>
        </section>

        <div className="stack" style={{ marginTop: 16 }}>
          {st.actionError && (
            <Banner title="操作没有成功" actions={<button type="button" className="btn sm" onClick={st.clearActionError}>知道了</button>}>
              {st.actionError}
            </Banner>
          )}
          {keyMissing && <KeyCard />}
          {!keyMissing && data?.error && !running && (
            <Banner title="上次启动出错了" actions={<button type="button" className="btn sm" onClick={() => navigate('logs')}>看日志</button>}>
              {data.error}
            </Banner>
          )}
        </div>

        <div className="dash-grid">
          <ChecksCard />
          <RecentDecisions />
        </div>
      </div>
    </div>
  )
}

function Stat({ k, label, value }) {
  return (
    <div className="stat">
      <span className="st-k" aria-hidden="true">{k}</span>
      <span className="st-l">{label}</span>
      <span className="st-v num">{value}</span>
    </div>
  )
}

/** The bot stopped with KEY_MISSING: fetch the WeChat key again. */
function KeyCard() {
  const ex = useKeyExtraction()
  const { refresh } = useStatus()
  useEffect(() => { if (ex.phase === 'done') refresh() }, [ex.phase, refresh])
  return (
    <section className="ch card key-card">
      <div className="card-head">
        <Key weight="bold" style={{ width: 20, height: 20, color: 'var(--need-ink)' }} />
        <div className="stack" style={{ gap: 2 }}>
          <h2>读不到微信的加密密钥</h2>
          <div className="sub">没有密钥就读不了聊天记录。点下面的按钮重新获取，过程中会提示你退出并重新登录微信。</div>
        </div>
      </div>
      {ex.running && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="row"><span className="spin" /><b>{ex.label}</b><span className="dim">{ex.message}</span></div>
          <div className="indet" />
        </div>
      )}
      {(ex.phase === 'timeout' || ex.phase === 'error') && <Note kind="need"><b>{ex.label}：</b>{ex.message}</Note>}
      {ex.phase === 'done' && (
        <Note kind="ok"><b>密钥获取成功。</b>{ex.result?.wxid ? `账号 ${ex.result.wxid}。` : ''}现在可以启动机器人了。</Note>
      )}
      {ex.phase !== 'done' && (
        <div className="row" style={{ marginTop: 12 }}>
          <button type="button" className="btn primary" onClick={ex.start} disabled={ex.running}>
            <Key weight="bold" />{ex.running ? '获取中…' : ex.phase ? '重试' : '重新获取密钥'}
          </button>
        </div>
      )}
    </section>
  )
}

const DIAG_ROWS = [
  ['python', 'Python 运行环境'],
  ['requirements', '依赖库'],
  ['wechat', '微信电脑端'],
  ['env', '配置文件 .env'],
  ['db', '数据目录读写'],
]

function ChecksCard() {
  const { conn, data, running } = useStatus()
  const [diag, setDiag] = useState(null)
  const [diagBusy, setDiagBusy] = useState(false)
  const [diagErr, setDiagErr] = useState('')
  const runDiag = async () => {
    setDiagBusy(true); setDiagErr('')
    try { setDiag(await diagnose()) } catch (e) { setDiagErr(e.message) } finally { setDiagBusy(false) }
  }
  const Row = ({ label, ok, value }) => (
    <div className="check-row">
      {ok === true ? <CheckCircle weight="fill" className="ok" /> : ok === false ? <XCircle weight="fill" className="bad" /> : <MinusCircle className="na" />}
      <span className="cr-l">{label}</span>
      <span className="cr-v">{value}</span>
    </div>
  )
  return (
    <section className="ch card">
      <div className="card-head">
        <div className="stack" style={{ gap: 2 }}>
          <span className="eyebrow" aria-hidden="true">CHECK</span>
          <h2>检查</h2>
        </div>
        <button type="button" className="btn sm" style={{ marginLeft: 'auto' }} onClick={runDiag} disabled={diagBusy}>
          <Stethoscope />{diagBusy ? '诊断中…' : '环境诊断'}
        </button>
      </div>
      <Row label="控制台服务" ok={conn === 'online' ? true : conn === 'offline' ? false : undefined} value={conn === 'online' ? '已连接' : conn === 'offline' ? '离线' : '连接中'} />
      <Row label="机器人进程" ok={running ? true : undefined} value={running ? '运行中' : '没有运行'} />
      <Row
        label="微信数据库"
        ok={running ? !!data?.db_ok : undefined}
        value={running ? (data?.db_ok ? '能读取' : '读取失败') : '运行时才检查'}
      />
      {diagErr && <Note kind="need">{diagErr}</Note>}
      {diag && (
        <div className="diag">
          <div className="label" style={{ margin: '12px 0 4px' }}>环境诊断</div>
          {DIAG_ROWS.map(([k, label]) => {
            const d = diag[k] || {}
            const extra = k === 'requirements' && d.missing?.length ? `缺少：${d.missing.join('、')}` : d.error
            return <Row key={k} label={label} ok={!!d.ok} value={[d.value, extra].filter(Boolean).join(' · ') || (d.ok ? '正常' : '异常')} />
          })}
        </div>
      )}
    </section>
  )
}

function RecentDecisions() {
  const [items, setItems] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(async () => {
    try {
      const d = await get('/api/decisions', { limit: 6 })
      setItems(d.decisions || []); setErr('')
    } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [load])
  return (
    <section className="ch card">
      <div className="card-head">
        <div className="stack" style={{ gap: 2 }}>
          <span className="eyebrow" aria-hidden="true">TRACE</span>
          <h2>最近的判断</h2>
        </div>
        <button type="button" className="icon-btn sm" style={{ marginLeft: 'auto' }} title="刷新" aria-label="刷新" onClick={load}><ArrowClockwise /></button>
      </div>
      {err && <Note kind="need">{err}</Note>}
      {items && !items.length && <Empty title="还没有记录">机器人运行后，每条消息回没回、为什么，都会记在这里。</Empty>}
      {items && items.length > 0 && (
        <div className="mini-trace">
          {items.map((d, i) => (
            <button type="button" key={`${d.message_id}-${i}`} className="mt-row" onClick={() => navigate(`trace/${encodeURIComponent(d.message_id || '')}`)}>
              <span className="mt-time num">{fmtTime(d.ts)}</span>
              <span className="mt-main">
                <span className="mt-who ellipsis">{d.chat || '—'} · {d.sender || '—'}</span>
                <span className="mt-why ellipsis">{d.reason_text || d.reason}</span>
              </span>
              <OutcomeTag outcome={d.outcome} />
            </button>
          ))}
        </div>
      )}
      <div className="card-foot">
        <button type="button" className="link" onClick={() => navigate('trace')}>全部回复记录<ArrowRight style={{ verticalAlign: -2, marginLeft: 4 }} /></button>
      </div>
    </section>
  )
}
