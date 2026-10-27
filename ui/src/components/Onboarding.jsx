import { useCallback, useEffect, useState } from 'react'
import { Key, ArrowLeft, CheckCircle, XCircle, Warning, ArrowClockwise, Sun, Moon, Check, Copy, Sparkle } from '@phosphor-icons/react'
import { api } from '../api'
import { useKeyExtraction, diagnose } from '../lib/keyExtraction'
import { useTheme } from '../lib/theme'
import { LogoGlyph, FigurePlaceholder } from './art'
import { SecDeco, Field, TextInput, Seg, Switch, Note, ChipsInput } from './ui'
import { randomToken } from './AdvancedPanel'

// First run of the agent console: get the WeChat key, set up WeChat, set up the
// relay to Claude Code on the Mac. Step 3 sends the server's onboarding steps 3
// and 4 with quiet defaults (no AI service, chat features off: this bot only
// relays), which write .env (ONBOARDING_DONE=true), then saves the relay settings.

const STEPS = [
  { id: 1, en: 'KEY', title: '微信密钥', sub: '让机器人能读微信消息' },
  { id: 2, en: 'WECHAT', title: '微信设置', sub: '机器人账号、在哪些群' },
  { id: 3, en: 'RELAY', title: '中转设置', sub: '谁能发任务、Mac 怎么连' },
]

const isMac = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform || navigator.userAgent)

export default function Onboarding({ onDone }) {
  const [step, setStep] = useState(1)
  const [done, setDone] = useState({})
  const [keyInfo, setKeyInfo] = useState(null) // {wxid, db_path, manualKey?}
  const { theme, toggle } = useTheme()
  const finish = (n, extra) => { setDone((d) => ({ ...d, [n]: true })); if (extra) setKeyInfo(extra); setStep(n + 1) }

  const LINES = {
    1: '先拿到微信密钥，才收得到消息。',
    2: '我在微信里叫什么？听哪些群？',
    3: '最后：你私聊发的 cc 任务我转给 Mac。',
  }

  return (
    <div className="app onboarding">
      <header className="topbar">
        <span className="brand"><span className="brand-mark"><LogoGlyph /></span><span className="brand-name"><b>WEBOT</b><small>首次设置</small></span></span>
        <div className="top-title" aria-hidden="true"><span className="latin">SETUP</span></div>
        <div className="top-actions">
          <button type="button" className="icon-btn" onClick={toggle} title={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'} aria-label={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'}>
            {theme === 'dark' ? <Sun /> : <Moon />}
          </button>
        </div>
      </header>
      <nav className="rail" aria-label="设置步骤">
        <div className="rail-head">
          <SecDeco no="01" en="SETUP" small />
          <span className="label" style={{ marginLeft: 4 }}>首次设置</span>
          <span className="sec-rule" aria-hidden="true" />
        </div>
        <ol className="steps">
          {STEPS.map((s) => {
            const cur = s.id === step
            const ok = done[s.id]
            const reachable = s.id <= step || done[s.id - 1]
            return (
              <li key={s.id}>
                <button type="button" className={`step${cur ? ' cur' : ''}${ok ? ' ok' : ''}`} disabled={!reachable} aria-current={cur ? 'step' : undefined} onClick={() => setStep(s.id)}>
                  <span className="st-no num">{ok ? <Check weight="bold" /> : `0${s.id}`}</span>
                  <span className="st-text"><b>{s.title}</b><small>{s.sub}</small></span>
                </button>
              </li>
            )
          })}
        </ol>
        <div className="dock" aria-hidden="true">
          <div className="linebox"><div className="lb-name">webot<span className="latin">SETUP</span></div><div className="lb-line">{LINES[step]}</div></div>
          <div className="bay"><span className="ring" /><span className="skew" /><span className="skew-2" /><span className="vtext">WEBOT</span></div>
          <div className="figure"><FigurePlaceholder /></div>
          <span className="shadow" />
          <span className="figure-tag">立绘待定</span>
        </div>
      </nav>
      <main className="main">
        <div className="main-scroll scroll">
          <div className="page ob-page">
            <header className="page-head">
              <SecDeco no={`0${step + 1}`} en={STEPS[step - 1].en} />
              <div className="ph-titles"><h1>{STEPS[step - 1].title}</h1><div className="ph-sub">第 {step} 步，共 {STEPS.length} 步 · {STEPS[step - 1].sub}</div></div>
            </header>
            {step === 1 && <StepKey onNext={(info) => finish(1, info)} />}
            {step === 2 && <StepWechat keyInfo={keyInfo} onBack={() => setStep(1)} onNext={() => finish(2)} />}
            {step === 3 && <StepRelay onBack={() => setStep(2)} onDone={onDone} />}
          </div>
        </div>
      </main>
    </div>
  )
}

/* ------------------------------------------------------------- 第 1 步 */

const DIAG = [
  { k: 'python', label: 'Python 运行环境', critical: true },
  { k: 'requirements', label: '依赖库', critical: true },
  { k: 'wechat', label: '微信电脑端', critical: false, help: '自动获取密钥需要微信已经登录。' },
  { k: 'env', label: '配置文件 .env', critical: false },
  { k: 'db', label: '数据目录读写', critical: true },
]

function StepKey({ onNext }) {
  const [diag, setDiag] = useState(null)
  const [diagErr, setDiagErr] = useState('')
  const [manual, setManual] = useState(false)
  const ex = useKeyExtraction()
  const check = useCallback(async () => {
    setDiag(null); setDiagErr('')
    try { setDiag(await diagnose()) } catch (e) { setDiagErr(e.message) }
  }, [])
  useEffect(() => { check() }, [check])
  const blocking = diag ? DIAG.filter((d) => d.critical && !diag[d.k]?.ok) : []
  const wechatOk = !!diag?.wechat?.ok

  if (manual) return <ManualKey onBack={() => setManual(false)} onNext={onNext} />

  return (
    <div className="stack" style={{ gap: 16 }}>
      <section className="ch card">
        <div className="card-head">
          <div className="stack" style={{ gap: 2 }}><span className="eyebrow" aria-hidden="true">CHECK</span><h2>环境检查</h2></div>
          <button type="button" className="btn sm" style={{ marginLeft: 'auto' }} onClick={check}><ArrowClockwise />重新检查</button>
        </div>
        {!diag && !diagErr && <div className="row"><span className="spin" />正在检查这台电脑…</div>}
        {diagErr && <Note kind="need">检查失败：{diagErr}</Note>}
        {diag && DIAG.map((d) => {
          const r = diag[d.k] || {}
          const extra = d.k === 'requirements' && r.missing?.length ? `缺少：${r.missing.join('、')}` : r.error
          return (
            <div key={d.k} className="check-row">
              {r.ok ? <CheckCircle weight="fill" className="ok" /> : d.critical ? <XCircle weight="fill" className="bad" /> : <Warning weight="fill" style={{ color: 'var(--warn-ink)' }} />}
              <span className="cr-l">{d.label}</span>
              <span className="cr-v">{[r.value, extra].filter(Boolean).join(' · ')}{!r.ok && d.help ? ` ${d.help}` : ''}</span>
            </div>
          )
        })}
        {blocking.length > 0 && <div style={{ marginTop: 12 }}><Note kind="need">有 {blocking.length} 项必须先解决：{blocking.map((b) => b.label).join('、')}。{blocking.some((b) => b.k === 'requirements') && '在项目目录运行 pip install -r requirements.txt。'}</Note></div>}
      </section>

      <section className="ch card">
        <div className="card-head"><div className="stack" style={{ gap: 2 }}><span className="eyebrow" aria-hidden="true">KEY</span><h2>获取微信密钥</h2></div></div>
        <p className="mod-desc">自动获取时会提示你<b>退出微信再重新登录</b>一次，期间不要关掉这个窗口。拿到的密钥只保存在这台电脑上。</p>
        {ex.running && (
          <div className="stack" style={{ gap: 8, marginTop: 12 }}>
            <div className="row"><span className="spin" /><b>{ex.label}</b><span className="dim">{ex.message}</span></div>
            <div className="indet" />
          </div>
        )}
        {(ex.phase === 'timeout' || ex.phase === 'error') && <div style={{ marginTop: 12 }}><Note kind="need"><b>{ex.label}：</b>{ex.message}</Note></div>}
        {ex.phase === 'done' && (
          <div style={{ marginTop: 12 }}>
            <Note kind="ok"><b>密钥获取成功。</b>{ex.result?.wxid && <>账号 <span className="mono">{ex.result.wxid}</span>。</>}</Note>
          </div>
        )}
        <div className="row wrap" style={{ marginTop: 14 }}>
          {ex.phase === 'done' ? (
            <button type="button" className="btn primary" onClick={() => onNext({ wxid: ex.result?.wxid, db_path: ex.result?.db_path })}>下一步</button>
          ) : (
            <button type="button" className="btn primary" disabled={ex.running || !wechatOk || blocking.length > 0} onClick={ex.start}>
              <Key weight="bold" />{ex.running ? '获取中…' : ex.phase ? '重试' : '自动获取'}
            </button>
          )}
          <button type="button" className="btn ghost" onClick={() => setManual(true)} disabled={ex.running}>我已经有密钥，手动填写</button>
          {diag && !wechatOk && !ex.phase && <span className="f-warn">请先登录微信电脑端，再点「重新检查」</span>}
        </div>
      </section>
    </div>
  )
}

function ManualKey({ onBack, onNext }) {
  const [key, setKey] = useState('')
  const [wxid, setWxid] = useState('')
  const valid = /^[0-9a-fA-F]{64}$/.test(key.trim())
  return (
    <section className="ch card">
      <div className="card-head"><div className="stack" style={{ gap: 2 }}><span className="eyebrow" aria-hidden="true">MANUAL</span><h2>手动填写密钥</h2></div></div>
      <div className="stack" style={{ gap: 14 }}>
        <Field label="微信数据库密钥" envKey="WCDB_KEY" hint="64 位十六进制字符（0-9、a-f）。会在下一步时保存到这台电脑的配置里" error={key && !valid ? '格式不对：应该是 64 位十六进制字符' : ''}>
          <TextInput value={key} onChange={setKey} mono type="password" autoComplete="off" placeholder="粘贴 64 位密钥" />
        </Field>
        <Field label="微信账号 ID（可不填）" hint="只用来显示"><TextInput value={wxid} onChange={setWxid} mono placeholder="wxid_…" /></Field>
      </div>
      <div className="card-foot">
        <button type="button" className="btn" onClick={onBack}><ArrowLeft />返回</button>
        <span className="spacer" />
        <button type="button" className="btn primary" disabled={!valid} onClick={() => onNext({ manualKey: key.trim(), wxid: wxid.trim() })}>下一步</button>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------- 第 2 步 */

function StepWechat({ keyInfo, onBack, onNext }) {
  const [name, setName] = useState('')
  const [mode, setMode] = useState('all')
  const [groups, setGroups] = useState([])
  const [backend, setBackend] = useState(isMac ? 'mac_hybrid' : 'wcdb')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const valid = name.trim().length > 0 && (mode === 'all' || groups.length > 0)
  const next = async () => {
    setBusy(true); setErr('')
    try {
      const body = {
        bot_display_name: name.trim(),
        wechat_groups: mode === 'all' ? '*' : groups.map((g) => encodeURIComponent(g)).join(','),
        wechat_backend: backend,
        wxid: keyInfo?.wxid || '', db_path: keyInfo?.db_path || '',
      }
      if (keyInfo?.manualKey) body.wcdb_key = keyInfo.manualKey
      const d = await api('/api/onboarding/step2', { method: 'POST', body })
      if (d.ok === false) throw new Error(d.error || '保存失败')
      onNext()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <section className="ch card">
      <div className="stack" style={{ gap: 16 }}>
        {keyInfo?.wxid && <Note kind="ok">微信账号 <span className="mono">{keyInfo.wxid}</span>{keyInfo.manualKey ? '（手动填写的密钥）' : ''}</Note>}
        <Field label="机器人的微信昵称" hint="和机器人账号在群里显示的名字一致，群友 @ 这个名字它才知道是在叫它">
          <TextInput value={name} onChange={setName} placeholder="例如 小助手" autoFocus />
        </Field>
        <Field label="在哪些群工作">
          <div><Seg value={mode} onChange={setMode} options={[{ value: 'all', label: '所有群' }, { value: 'some', label: '只在这些群' }]} /></div>
        </Field>
        {mode === 'some' && <ChipsInput split={false} value={groups} onChange={setGroups} placeholder="输入群名（和微信里完全一致）后按回车，一次一个" />}
        <Field label="微信后端" hint={isMac ? '这台是 Mac：用 WeFlow 或界面操作' : 'Windows 用 WCDB 直读'}>
          <div><Seg value={backend} onChange={setBackend} options={[{ value: 'wcdb', label: 'WCDB 直读（Windows）' }, { value: 'mac_hybrid', label: 'macOS WeFlow' }, { value: 'mac_ui', label: 'macOS 界面' }]} /></div>
        </Field>
        {err && <Note kind="need">{err}</Note>}
      </div>
      <div className="card-foot">
        <button type="button" className="btn" onClick={onBack}><ArrowLeft />上一步</button>
        <span className="spacer" />
        <button type="button" className="btn primary" disabled={!valid || busy} onClick={next}>{busy ? '保存中…' : '下一步'}</button>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------- 第 3 步 */

async function onboardingStep(path, body) {
  const d = await api(path, { method: 'POST', body })
  if (d.ok === false) throw new Error(d.error || '保存失败')
}

function StepRelay({ onBack, onDone }) {
  const [wxid, setWxid] = useState('')
  const [token, setToken] = useState(() => randomToken())
  const [port, setPort] = useState('8790')
  const [onlyMe, setOnlyMe] = useState(true)
  const [relayOnly, setRelayOnly] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [written, setWritten] = useState(false)
  const id = wxid.trim()
  const wxidOk = /^[A-Za-z][A-Za-z0-9_-]{4,}$/.test(id)
  const portOk = /^\d+$/.test(port) && Number(port) >= 1 && Number(port) <= 65535
  const tokenOk = token.trim().length >= 16

  const write = async (relayOn) => {
    setBusy(true); setErr('')
    try {
      if (!written) {
        // This console only relays: no AI service, the chat features off.
        await onboardingStep('/api/onboarding/step3', { ai_backend: 'deepseek', model_route: 'cloud' })
        await onboardingStep('/api/onboarding/step4', {
          fun_enabled: false, sticky_mention_enabled: false, followup_listen_enabled: false,
          message_burst_enabled: true, web_search_enabled: false, proactive_enabled: false,
          private_chat_enabled: relayOn,
          private_chats: relayOn && onlyMe ? encodeURIComponent(id) : '*',
        })
        setWritten(true)
      }
      if (relayOn) {
        const r = await api('/api/advanced', {
          method: 'POST',
          body: { values: { CLAUDE_RELAY: 'on', CLAUDE_RELAY_ONLY: relayOnly ? 'on' : 'off', ADMIN_WXID: id, CLAUDE_RELAY_TOKEN: token.trim(), CLAUDE_RELAY_PORT: port } },
        })
        if (!r.ok) throw new Error([r.error, ...Object.values(r.errors || {})].filter(Boolean).join('；'))
      }
      onDone()
    } catch (e) { setErr(e.message); setBusy(false) }
  }
  const copy = async () => { try { await navigator.clipboard.writeText(token) } catch { /* select it by hand */ } }

  return (
    <section className="ch card">
      <div className="stack" style={{ gap: 16 }}>
        <Note kind="cy">你在微信里<b>私聊</b>机器人账号发「cc 任务」，机器人把任务交给 Mac 上的 Claude Code，做完把结果发回私聊。中转要在 Windows 机器人（WCDB 直读）上运行。</Note>
        <Field label="你的 wxid" hint="发 cc 任务的人（你自己）的微信 wxid，形如 wxid_xxx，不是机器人账号的。只有这个人能用中转" error={id && !wxidOk ? '看起来不像 wxid' : ''}>
          <TextInput value={wxid} onChange={setWxid} mono placeholder="wxid_…" autoFocus />
        </Field>
        <div className="toggle-row">
          <div className="tr-text"><span className="tr-title">只做中转</span><div className="f-hint">只处理 cc 任务：群里被 @、别人的私聊一律不回，也不调用 AI（推荐）</div></div>
          <Switch checked={relayOnly} onChange={setRelayOnly} label="只做中转" />
        </div>
        <div className="toggle-row">
          <div className="tr-text"><span className="tr-title">只读你的私聊</span><div className="f-hint">其他人的私聊不会被读取（推荐）</div></div>
          <Switch checked={onlyMe} onChange={setOnlyMe} label="只读你的私聊" />
        </div>
        <Field label="token" hint="Mac 来取任务时带的密码。复制下来，写进 Mac 的 ~/.claude-relay/token（只放这一行），不要发到聊天里" error={!tokenOk ? '至少 16 个字符' : ''}>
          <div className="input-wrap">
            <TextInput value={token} onChange={setToken} mono />
            <button type="button" className="btn" onClick={() => setToken(randomToken())}><Sparkle />重新生成</button>
            <button type="button" className="btn" onClick={copy}><Copy />复制</button>
          </div>
        </Field>
        <Field label="端口" hint="Mac 连这台电脑的这个端口；Windows 防火墙要放行它（只放行局域网；Mac 不在同一个网络时用 Tailscale，再放行 100.64.0.0/10）" error={!portOk ? '1 到 65535 之间的整数' : ''}>
          <div style={{ width: 168 }}><TextInput value={port} onChange={setPort} mono /></div>
        </Field>
        {err && <Note kind="need">{err}{written && <> 其余设置已经写好，可以先进控制台，在「中转代理」页里再改。</>}</Note>}
      </div>
      <div className="card-foot">
        <button type="button" className="btn" onClick={onBack} disabled={busy || written}><ArrowLeft />上一步</button>
        <span className="spacer" />
        {written && <button type="button" className="btn" onClick={onDone}>先进控制台</button>}
        <button type="button" className="btn ghost" onClick={() => write(false)} disabled={busy}>以后再设置中转</button>
        <button type="button" className="btn primary" disabled={busy || !wxidOk || !portOk || !tokenOk} onClick={() => write(true)}>{busy ? '正在写入…' : '完成'}</button>
      </div>
    </section>
  )
}
