import { useCallback, useEffect, useState } from 'react'
import { Key, ArrowLeft, CheckCircle, XCircle, Warning, ArrowClockwise, Sun, Moon, Check } from '@phosphor-icons/react'
import { api } from '../api'
import { useKeyExtraction, diagnose } from '../lib/keyExtraction'
import { useTheme } from '../lib/theme'
import { LogoGlyph, FigurePlaceholder } from './art'
import { SecDeco, Field, TextInput, Seg, Switch, Note, ChipsInput, Select } from './ui'

// First run: get the WeChat key, name the bot, pick the AI service, choose a few
// features. Step 4 writes .env (ONBOARDING_DONE=true) and opens the console.

const STEPS = [
  { id: 1, en: 'KEY', title: '微信密钥', sub: '让机器人能读聊天记录' },
  { id: 2, en: 'WECHAT', title: '微信设置', sub: '机器人叫什么、在哪些群' },
  { id: 3, en: 'AI', title: 'AI 服务', sub: '用哪家的模型回复' },
  { id: 4, en: 'FEATURES', title: '功能', sub: '先开哪些功能' },
]

const isMac = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform || navigator.userAgent)

export default function Onboarding({ onDone }) {
  const [step, setStep] = useState(1)
  const [done, setDone] = useState({})
  const [keyInfo, setKeyInfo] = useState(null) // {wxid, db_path, manualKey?}
  const { theme, toggle } = useTheme()
  const finish = (n, extra) => { setDone((d) => ({ ...d, [n]: true })); if (extra) setKeyInfo(extra); setStep(n + 1) }

  const LINES = {
    1: '先拿到微信密钥，才看得到群聊。',
    2: '我在微信里叫什么？去哪些群？',
    3: '用哪里的模型？云端本机都行。',
    4: '最后挑几个功能，以后还能改。',
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
              <div className="ph-titles"><h1>{STEPS[step - 1].title}</h1><div className="ph-sub">第 {step} 步，共 4 步 · {STEPS[step - 1].sub}</div></div>
            </header>
            {step === 1 && <StepKey onNext={(info) => finish(1, info)} />}
            {step === 2 && <StepWechat keyInfo={keyInfo} onBack={() => setStep(1)} onNext={() => finish(2)} />}
            {step === 3 && <StepAi onBack={() => setStep(2)} onNext={() => finish(3)} />}
            {step === 4 && <StepFeatures onBack={() => setStep(3)} onDone={onDone} />}
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

const MODELS = {
  deepseek: [{ value: 'deepseek-v4-flash', label: 'V4 Flash · 便宜（推荐）' }, { value: 'deepseek-v4-pro', label: 'V4 Pro · 更强' }],
  openai: [{ value: 'gpt-5.6-terra', label: 'GPT-5.6 Terra · 平衡（推荐）' }, { value: 'gpt-5.6', label: 'GPT-5.6 · 更强' }, { value: 'gpt-5.6-luna', label: 'GPT-5.6 Luna · 更快' }],
  claude: [{ value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5 · 快、便宜' }, { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6 · 质量高' }],
}
const BASE_URL = { deepseek: 'https://api.deepseek.com', openai: '', claude: 'https://api.anthropic.com', local: 'http://127.0.0.1:11434/v1' }

function StepAi({ onBack, onNext }) {
  const [ai, setAi] = useState('deepseek')
  const [key, setKey] = useState('')
  const [url, setUrl] = useState(BASE_URL.deepseek)
  const [model, setModel] = useState(MODELS.deepseek[0].value)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const local = ai === 'local'
  const pick = (v) => { setAi(v); setUrl(BASE_URL[v]); setModel(v === 'local' ? '' : MODELS[v][0].value); setKey('') }
  const valid = local ? model.trim() && /^https?:\/\//.test(url.trim()) : key.trim().length >= 10
  const next = async () => {
    setBusy(true); setErr('')
    // Everything on a model on this computer: MODEL_ROUTE=local, no cloud key.
    const body = local
      ? { ai_backend: 'deepseek', model_route: 'local', local_base_url: url.trim(), local_model: model.trim(), local_api_key: key.trim() }
      : { ai_backend: ai, model_route: 'cloud' }
    if (ai === 'deepseek') Object.assign(body, { deepseek_api_key: key.trim(), deepseek_base_url: url.trim() || BASE_URL.deepseek, deepseek_model: model })
    if (ai === 'openai') Object.assign(body, { openai_api_key: key.trim(), openai_base_url: url.trim(), openai_model: model })
    if (ai === 'claude') Object.assign(body, { anthropic_api_key: key.trim(), anthropic_base_url: url.trim() || BASE_URL.claude, summarize_model: model })
    try {
      const d = await api('/api/onboarding/step3', { method: 'POST', body })
      if (d.ok === false) throw new Error(d.error || '保存失败')
      onNext()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <section className="ch card">
      <div className="stack" style={{ gap: 16 }}>
        <Field label="用哪里的模型"><div><Seg value={ai} onChange={pick} options={[{ value: 'deepseek', label: 'DeepSeek（推荐）' }, { value: 'openai', label: 'OpenAI' }, { value: 'claude', label: 'Claude' }, { value: 'local', label: '本地模型' }]} /></div></Field>
        {local ? (
          <>
            <div className="form-grid">
              <Field label="本地服务地址" hint="Ollama 默认 http://127.0.0.1:11434/v1，LM Studio 是 http://127.0.0.1:1234/v1" error={url && !/^https?:\/\//.test(url.trim()) ? '要以 http:// 或 https:// 开头' : ''}>
                <TextInput value={url} onChange={setUrl} mono />
              </Field>
              <Field label="模型名" hint="本地服务里的模型名，例如 qwen3.5:4b"><TextInput value={model} onChange={setModel} mono placeholder="必填" /></Field>
            </div>
            <Field label="本地服务密钥（可不填）" hint="大多数本地服务不检查"><TextInput value={key} onChange={setKey} mono type="password" autoComplete="off" /></Field>
            <Note kind="cy">所有任务都交给这台电脑（或局域网里）的模型，不需要云端 API Key，也不产生费用。上线前先运行 python tools/model_check.py 检查一遍。以后可以在「高级开关 → 本地模型」里改成分工：只把一部分任务交给本地模型。</Note>
          </>
        ) : (
          <>
            <Field label="API Key" hint={ai === 'deepseek' ? '在 platform.deepseek.com 申请' : ai === 'openai' ? '在 platform.openai.com 申请' : '在 console.anthropic.com 申请'}>
              <TextInput value={key} onChange={setKey} mono type="password" autoComplete="off" placeholder="粘贴 API Key" />
            </Field>
            <div className="form-grid">
              <Field label="接口地址" hint="用代理或中转服务时才改"><TextInput value={url} onChange={setUrl} mono placeholder={ai === 'openai' ? '留空用官方地址' : ''} /></Field>
              <Field label="模型"><Select value={model} onChange={setModel} options={MODELS[ai]} /></Field>
            </div>
            <Note>调用模型按用量计费，费用由你的账号承担。以后在「系统配置 → AI 服务」里可以换。</Note>
          </>
        )}
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

/* ------------------------------------------------------------- 第 4 步 */

const FEATURES = [
  { k: 'fun_enabled', title: '趣味抽签', desc: '群友 @机器人说「抽签」时抽一支签', def: true },
  { k: 'sticky_mention_enabled', title: '粘性提及', desc: '只发了「@机器人」时，把下一句当作是对它说的', def: true },
  { k: 'followup_listen_enabled', title: '跟聊', desc: '刚回复完时，接着说的短句也会接', def: true },
  { k: 'message_burst_enabled', title: '连发合并', desc: '同一个人连发几条时合成一句再处理', def: true },
  { k: 'web_search_enabled', title: '联网搜索', desc: '有人说「搜一下」时先上网查', def: true },
  { k: 'proactive_enabled', title: '主动发言', desc: '没人叫它时，偶尔自己插一句', def: false },
]

function StepFeatures({ onBack, onDone }) {
  const [v, setV] = useState(Object.fromEntries(FEATURES.map((f) => [f.k, f.def])))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const finish = async () => {
    setBusy(true); setErr('')
    try {
      const d = await api('/api/onboarding/step4', { method: 'POST', body: v })
      if (d.ok === false) throw new Error(d.error || '写入配置失败')
      onDone()
    } catch (e) { setErr(e.message); setBusy(false) }
  }
  return (
    <section className="ch card">
      {FEATURES.map((f) => (
        <div key={f.k} className="toggle-row">
          <div className="tr-text"><span className="tr-title">{f.title}</span><div className="f-hint">{f.desc}</div></div>
          <Switch checked={v[f.k]} onChange={(on) => setV((x) => ({ ...x, [f.k]: on }))} label={f.title} />
        </div>
      ))}
      <div style={{ marginTop: 12 }}><Note kind="cy">点「完成」会把这些设置写进配置文件，然后打开控制台。机器人不会自动启动，在控制台里点「启动」。</Note></div>
      {err && <div style={{ marginTop: 12 }}><Note kind="need">{err}</Note></div>}
      <div className="card-foot">
        <button type="button" className="btn" onClick={onBack}><ArrowLeft />上一步</button>
        <span className="spacer" />
        <button type="button" className="btn primary" disabled={busy} onClick={finish}>{busy ? '正在写入…' : '完成'}</button>
      </div>
    </section>
  )
}

