import { useEffect, useRef, useState } from 'react'
import {
  DownloadSimple, UploadSimple, FolderOpen, MagnifyingGlass, ArrowUp, Folder, CheckCircle,
  PaperPlaneTilt, Robot, Sparkle, Brain,
} from '@phosphor-icons/react'
import { api, get, post } from '../api'
import { useConfig, useField, toPayload } from '../store/config'
import { useToast } from '../store/toast'
import { useGroups } from '../lib/groups'
import { PageHead, Card, Field, Note, Dialog, Confirm, ChipsInput, Seg, TextInput, TextArea, Empty } from './ui'
import { CfgText, CfgTextArea, CfgNumber, CfgSelect, CfgSecret, CfgChips, CfgSeg } from './cfg'

const SECTIONS = {
  ai: { en: 'AI', title: 'AI 服务', sub: '机器人用哪家的模型回复，以及对应的密钥' },
  identity: { en: 'IDENTITY', title: '身份与群聊', sub: '机器人在微信里叫什么、在哪些群里工作、什么话算是在叫它' },
  persona: { en: 'PERSONA', title: '人设', sub: '机器人以什么样的人设说话' },
  data: { en: 'DATA', title: '数据与日志', sub: '微信数据放在哪里，日志记多详细' },
  sandbox: { en: 'SANDBOX', title: '提示词沙箱', sub: '不进微信，先看看机器人会怎么回一句话' },
}

export default function ConfigPanel({ section }) {
  const meta = SECTIONS[section] || SECTIONS.ai
  const { status, error, reload } = useConfig()
  let body
  if (status === 'loading') body = <Empty title="正在读取设置…" />
  else if (status === 'error') {
    body = (
      <Empty title="设置读取失败">
        {error}<div style={{ marginTop: 10 }}><button type="button" className="btn" onClick={reload}>重试</button></div>
      </Empty>
    )
  } else if (section === 'identity') body = <IdentitySection />
  else if (section === 'persona') body = <PersonaSection />
  else if (section === 'data') body = <DataSection />
  else if (section === 'sandbox') body = <SandboxSection />
  else body = <AiSection />
  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead no="02" en={meta.en} title={meta.title} sub={meta.sub} />
        {body}
        {section !== 'sandbox' && status !== 'loading' && status !== 'error' && <BackupCard />}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ AI 服务 */

const PROVIDERS = [
  { value: 'deepseek', name: 'DeepSeek', note: '推荐 · 中文好、便宜', icon: Sparkle },
  { value: 'openai', name: 'OpenAI', note: '可以接图片识别', icon: Brain },
  { value: 'claude', name: 'Claude', note: 'Anthropic', icon: Robot },
]

function AiSection() {
  const [backend, setBackend] = useField('ai_backend')
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="choice-grid" role="radiogroup" aria-label="AI 服务商">
        {PROVIDERS.map((p) => {
          const on = backend === p.value
          const Ico = p.icon
          return (
            <button
              key={p.value} type="button" role="radio" aria-checked={on ? 'true' : 'false'}
              className={`choice chc${on ? ' on' : ''}`} onClick={() => setBackend(p.value)}
            >
              <Ico weight={on ? 'fill' : 'regular'} />
              <span className="c-name">{p.name}</span>
              <span className="c-note">{p.note}</span>
              {on && <span className="c-cur">正在用</span>}
            </button>
          )
        })}
      </div>
      {backend === 'openai' ? (
        <Card title="OpenAI" eyebrow="PROVIDER">
          <div className="form-grid">
            <CfgSecret k="openai_api_key" label="API Key" envKey="OPENAI_API_KEY" className="span-2" />
            <CfgText k="openai_base_url" label="接口地址" envKey="OPENAI_BASE_URL" mono placeholder="留空用官方地址" hint="用代理或中转服务时才填" />
            <CfgSelect k="openai_model" label="模型" envKey="OPENAI_MODEL" options={[
              { value: 'gpt-5.6-terra', label: 'GPT-5.6 Terra · 平衡（推荐）' },
              { value: 'gpt-5.6', label: 'GPT-5.6 · 更强，更贵' },
              { value: 'gpt-5.6-luna', label: 'GPT-5.6 Luna · 更快，更省' },
            ]} />
          </div>
        </Card>
      ) : backend === 'claude' ? (
        <Card title="Claude" eyebrow="PROVIDER">
          <div className="form-grid">
            <CfgSecret k="anthropic_api_key" label="API Key" envKey="ANTHROPIC_API_KEY" className="span-2" />
            <CfgText k="anthropic_base_url" label="接口地址" envKey="ANTHROPIC_BASE_URL" mono placeholder="https://api.anthropic.com" />
            <CfgSelect k="summarize_model" label="总结用的模型" envKey="SUMMARIZE_MODEL" options={[
              { value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5 · 快、便宜' },
              { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6 · 质量高' },
            ]} hint="飞书同步写总结时用" />
          </div>
          <div style={{ marginTop: 14 }}>
            <Note kind="cy">群聊回复用的 Claude 模型是另一项设置（CLAUDE_CHAT_MODEL，默认 Haiku 4.5），在「高级开关 → 回复管线」里改。</Note>
          </div>
        </Card>
      ) : (
        <Card title="DeepSeek" eyebrow="PROVIDER">
          <div className="form-grid">
            <CfgSecret k="deepseek_api_key" label="API Key" envKey="DEEPSEEK_API_KEY" hint="在 platform.deepseek.com 申请" className="span-2" />
            <CfgText k="deepseek_base_url" label="接口地址" envKey="DEEPSEEK_BASE_URL" mono placeholder="https://api.deepseek.com" hint="用代理或中转服务时才改" />
            <CfgSelect k="deepseek_model" label="模型" envKey="DEEPSEEK_MODEL" options={[
              { value: 'deepseek-v4-flash', label: 'V4 Flash · ¥1 入 / ¥2 出 每百万字' },
              { value: 'deepseek-v4-pro', label: 'V4 Pro · ¥3 入 / ¥6 出 每百万字' },
            ]} />
          </div>
        </Card>
      )}
      <Note>三家的密钥分开保存，切换服务商不会清掉另外两家的密钥。保存后，密钥只显示首尾几位。</Note>
      <Note kind="cy">也可以把全部或一部分任务（回复、主动发言、记忆整理）交给自己电脑上的模型，在「高级开关 → 本地模型」里设置。全部用本地模型时，这里的密钥可以不填。</Note>
    </div>
  )
}

/* ------------------------------------------------------------- 身份与群聊 */

// The literal option values are checked by tests/test_macos_adaptation.py.
const WECHAT_BACKENDS = [
  { value: 'wcdb', label: 'WCDB 直读（Windows）' },
  { value: 'mac_hybrid', label: 'macOS WeFlow' },
  { value: 'mac_ui', label: 'macOS 界面' },
]

function IdentitySection() {
  const [name] = useField('bot_display_name')
  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card title="机器人身份" eyebrow="IDENTITY">
        <div className="form-grid">
          <CfgText
            k="bot_display_name" label="微信昵称" envKey="BOT_DISPLAY_NAME"
            hint="和机器人账号在群里显示的名字一致。群友 @ 这个名字时，机器人才知道是在叫它"
            warn={!String(name || '').trim() ? '没有填昵称，机器人认不出别人在 @ 它' : ''}
          />
          <CfgSeg k="wechat_backend" label="微信后端" envKey="WECHAT_BACKEND" options={WECHAT_BACKENDS} hint="Windows 用 WCDB 直读；macOS 上二选一" />
        </div>
      </Card>
      <GroupsCard />
      <Card title="唤醒词" eyebrow="TRIGGER">
        <CfgChips
          k="trigger_keywords" envKey="TRIGGER_KEYWORDS" minItems={1}
          hint="群友的消息里出现这些词时，机器人当作被 @ 了来回复。至少保留一个；不想用这个功能，就留一个群里不会出现的词"
        />
      </Card>
    </div>
  )
}

function GroupsCard() {
  const [value, setValue] = useField('wechat_groups')
  const names = Array.isArray(value) ? value : []
  const [wantSome, setWantSome] = useState(false)
  const mode = names.length || wantSome ? 'some' : 'all'
  const all = mode === 'all'
  const groups = useGroups()
  const known = (groups.list || []).map((g) => g.group_name).filter(Boolean)
  const pickable = known.filter((n) => !names.includes(n))
  return (
    <Card title="监听哪些群" eyebrow="GROUPS">
      <Field envKey="WECHAT_GROUPS" hint="只有列出的群会被读取和回复。群名要和微信里完全一致">
        <div><Seg
          label="监听范围" value={mode}
          onChange={(v) => { setWantSome(v === 'some'); if (v === 'all') setValue([]) }}
          options={[{ value: 'all', label: '全部群聊' }, { value: 'some', label: '只监听这些群' }]}
        /></div>
      </Field>
      {!all && (
        <div className="stack" style={{ marginTop: 14 }}>
          <ChipsInput split={false} value={names} onChange={setValue} placeholder="输入群名后按回车（一次一个）" />
          {pickable.length > 0 && (
            <div>
              <div className="label" style={{ marginBottom: 6 }}>从消息记录里找到的群</div>
              <div className="chips">
                {pickable.slice(0, 30).map((n) => (
                  <button key={n} type="button" className="chip pick" onClick={() => setValue([...names, n])}>+ {n}</button>
                ))}
              </div>
            </div>
          )}
          {names.length === 0 && <Note kind="warn">还没有添加群。保存时会变回「全部群聊」。</Note>}
        </div>
      )}
    </Card>
  )
}

/* ------------------------------------------------------------------- 人设 */

const PERSONAS = [
  { value: 'yang_yujing', name: '杨宇靖', note: '内置人设，逐条读取指定的群' },
  { value: 'custom', name: '自定义', note: '可以自己改人设和回复规则，起始内容是杨宇靖' },
]

function PersonaSection() {
  const [profile, setProfile] = useField('chat_persona_profile')
  const { base } = useConfig()
  const preset = base?.yang_preset
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="choice-grid two" role="radiogroup" aria-label="人设">
        {PERSONAS.map((p) => {
          const on = profile === p.value
          return (
            <button key={p.value} type="button" role="radio" aria-checked={on ? 'true' : 'false'} className={`choice chc${on ? ' on' : ''}`} onClick={() => setProfile(p.value)}>
              <span className="c-name">{p.name}</span>
              <span className="c-note">{p.note}</span>
              {on && <span className="c-cur">正在用</span>}
            </button>
          )
        })}
      </div>
      {profile === 'yang_yujing' ? (
        <>
          <Card title="杨宇靖 · 群设置" eyebrow="PRESET">
            <div className="form-grid">
              <CfgText k="yang_yujing_group_name" label="逐条读取的群" envKey="YANG_YUJING_GROUP_NAME" hint="这个群的每条消息都会交给模型判断要不要接话。必填" />
              <CfgText k="yang_yujing_always_reply_group_name" label="逐条必回的群" envKey="YANG_YUJING_ALWAYS_REPLY_GROUP_NAME" hint="可不填。这个群里每条消息都会回复" />
            </div>
          </Card>
          <Card title="内置人设内容" eyebrow="READ ONLY">
            {preset ? (
              <div className="stack">
                <Field label="人设"><TextArea value={preset.prompt} onChange={() => {}} rows={7} readOnly /></Field>
                <Field label="回复规则"><TextArea value={preset.reply_rules} onChange={() => {}} rows={6} readOnly /></Field>
                <div className="f-hint">由程序内置（src/personas/yang_yujing.json），这里只读：每条最多 {preset.max_chars} 字，看 {preset.context_window_sec} 秒内最多 {preset.context_count} 条消息。「自定义」里改的内容不受影响，切回去还在。</div>
              </div>
            ) : (
              <Note>读不到内置人设的内容。</Note>
            )}
          </Card>
        </>
      ) : (
        <Card title="自定义 · 人设" eyebrow="EDITABLE">
          <div className="stack" style={{ gap: 16 }}>
            <CfgText k="chat_persona_name" label="人设名字" envKey="CHAT_PERSONA_NAME" />
            <CfgTextArea k="chat_persona_prompt" label="人设" envKey="CHAT_PERSONA_PROMPT" rows={9} hint="这个人是谁、什么性格、怎么说话" />
            <CfgTextArea k="chat_reply_rules" label="回复规则" envKey="CHAT_REPLY_RULES" rows={7} hint="长度、什么时候该回、不能做什么" />
            <div className="form-grid three">
              <CfgNumber k="chat_max_chars" label="每条最长字数" envKey="CHAT_MAX_CHARS" suffix="字" />
              <CfgNumber k="chat_context_window_sec" label="看多久以内的消息" envKey="CHAT_CONTEXT_WINDOW_SEC" suffix="秒" />
              <CfgNumber k="chat_context_count" label="最多看几条" envKey="CHAT_CONTEXT_COUNT" suffix="条" />
            </div>
          </div>
        </Card>
      )}
      <Note kind="cy">示例对话（让人设学说话方式的例句）在「高级开关 → 回复管线 → PERSONA_EXAMPLES」，要先打开 PIPELINE_V2 才起作用。</Note>
    </div>
  )
}

/* -------------------------------------------------------------- 数据与日志 */

function DataSection() {
  const [dir, setDir] = useField('wechat_data_dir')
  const { detectedDataDir } = useConfig()
  const [browse, setBrowse] = useState(false)
  const [detect, setDetect] = useState(null)
  const [busy, setBusy] = useState(false)
  const runDetect = async () => {
    setBusy(true)
    try { setDetect(await post('/api/wechat-data-dir/detect', { path: dir || '' })) } catch (e) { setDetect({ found: false, message: e.message }) } finally { setBusy(false) }
  }
  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card title="微信数据目录" eyebrow="WECHAT DATA">
        <Field envKey="WECHAT_DATA_DIR" hint="Windows 微信放聊天数据的文件夹（xwechat_files），一般能自动找到，找不到时再手动选">
          <div className="input-wrap">
            <TextInput value={dir} onChange={setDir} mono placeholder={detectedDataDir || '例如 D:\\xwechat_files'} />
            <button type="button" className="btn" onClick={() => setBrowse(true)}><FolderOpen />浏览…</button>
            <button type="button" className="btn" onClick={runDetect} disabled={busy}><MagnifyingGlass />{busy ? '检查中…' : '检查'}</button>
          </div>
        </Field>
        {detectedDataDir && detectedDataDir !== dir && (
          <div className="row" style={{ marginTop: 10 }}>
            <span className="f-hint">自动找到：<span className="mono">{detectedDataDir}</span></span>
            <button type="button" className="btn sm" onClick={() => setDir(detectedDataDir)}>用这个</button>
          </div>
        )}
        {detect && (
          <div style={{ marginTop: 12 }}>
            {detect.found && detect.accounts?.length ? (
              <div className="stack" style={{ gap: 6 }}>
                <Note kind="ok">找到 {detect.accounts.length} 个微信账号的数据。机器人会读取当前登录的那个账号。</Note>
                {detect.accounts.map((a) => (
                  <div key={a.wxid} className="kv-line">
                    <span className="mono">{a.wxid}</span>
                    {a.has_session_db ? <span className="tag ok"><CheckCircle weight="fill" />数据库就绪</span> : <span className="tag warn">没找到 session.db</span>}
                  </div>
                ))}
              </div>
            ) : <Note kind="warn">{detect.message || '这个目录里没有找到微信账号的数据。'}</Note>}
          </div>
        )}
        <div style={{ marginTop: 12 }}>
          <Note>如果这台电脑有本机覆盖文件 .env.local（例如虚拟机里的 %APPDATA%\webot\.env.local），那里写的值优先，完全退出再打开 webot 后会以它为准。</Note>
        </div>
      </Card>
      <Card title="日志" eyebrow="LOG">
        <CfgSeg k="log_level" label="记多详细" envKey="LOG_LEVEL" options={[
          { value: 'DEBUG', label: '调试 DEBUG' }, { value: 'INFO', label: '一般 INFO' },
          { value: 'WARNING', label: '警告 WARNING' }, { value: 'ERROR', label: '只记错误 ERROR' },
        ]} hint="排查问题时改成调试；平时用一般。日志在「运行日志」里看" />
      </Card>
      {browse && <BrowseDialog start={dir || detectedDataDir || ''} onPick={(p) => { setDir(p); setBrowse(false) }} onClose={() => setBrowse(false)} />}
    </div>
  )
}

function parentOf(path) {
  if (!path) return ''
  const trimmed = path.replace(/[\\/]+$/, '')
  const i = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'))
  if (i < 0) return ''
  const parent = trimmed.slice(0, i)
  if (/^[A-Za-z]:$/.test(parent)) return `${parent}\\`
  return parent || '/'
}

function BrowseDialog({ start, onPick, onClose }) {
  const [path, setPath] = useState(start)
  const [cur, setCur] = useState(null)
  const [entries, setEntries] = useState([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const go = async (p) => {
    setBusy(true); setErr('')
    try {
      const d = await get('/api/browse', { path: p })
      setEntries((d.entries || []).filter((e) => e.is_dir))
      setCur(d.current_path || p)
      setPath(d.current_path || p)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { go(start) }, [])
  return (
    <Dialog
      title="选择文件夹" eyebrow="BROWSE" wide onClose={onClose}
      footer={<>
        <span className="f-hint grow mono ellipsis">{cur || '（盘符列表）'}</span>
        <button type="button" className="btn" onClick={onClose}>取消</button>
        <button type="button" className="btn primary" disabled={!cur} onClick={() => onPick(cur)}>选这个文件夹</button>
      </>}
    >
      <div className="input-wrap" style={{ marginBottom: 10 }}>
        <button type="button" className="btn" style={{ borderLeft: '1px solid var(--ink)' }} onClick={() => go(parentOf(cur))} disabled={!cur} title="上一级"><ArrowUp /></button>
        <TextInput value={path} onChange={setPath} mono onKeyDown={(e) => { if (e.key === 'Enter') go(path) }} />
        <button type="button" className="btn" onClick={() => go(path)}>前往</button>
      </div>
      {err && <Note kind="need">{err}</Note>}
      <div className="browse-list scroll">
        {busy && <div className="row" style={{ padding: 10 }}><span className="spin" />读取中…</div>}
        {!busy && !entries.length && <div className="f-hint" style={{ padding: 10 }}>这里没有子文件夹</div>}
        {!busy && entries.map((e) => (
          <button key={e.path} type="button" className="list-row" onDoubleClick={() => go(e.path)} onClick={() => go(e.path)}>
            <Folder weight="fill" style={{ color: 'var(--cy-mid)' }} /><span className="ellipsis">{e.name}</span>
          </button>
        ))}
      </div>
    </Dialog>
  )
}

/* -------------------------------------------------------------- 提示词沙箱 */

function parseContext(text) {
  return text.split('\n').map((l) => l.trim()).filter(Boolean).map((line) => {
    const m = line.match(/^([^:：]+)[:：]\s*(.*)$/)
    return m ? { sender_name: m[1].trim(), content: m[2] } : { sender_name: '群友', content: line }
  })
}

function SandboxSection() {
  const { draft, base } = useConfig()
  const [message, setMessage] = useState('')
  const [sender, setSender] = useState('群友甲')
  const [group, setGroup] = useState('测试群')
  const [memory, setMemory] = useState('')
  const [context, setContext] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const yang = draft.chat_persona_profile === 'yang_yujing'

  const run = async () => {
    if (!message.trim()) return
    setBusy(true); setResult(null)
    const t0 = performance.now()
    const payload = {
      message, sender_name: sender, group_name: group, group_memory: memory,
      context_messages: parseContext(context),
      ai_backend: draft.ai_backend,
      deepseek_api_key: draft.deepseek_api_key, deepseek_model: draft.deepseek_model,
      openai_api_key: draft.openai_api_key, openai_base_url: draft.openai_base_url, openai_model: draft.openai_model,
      anthropic_api_key: draft.anthropic_api_key, summarize_model: draft.summarize_model,
    }
    const p = yang ? base.yang_preset : null
    if (yang && p) {
      Object.assign(payload, {
        chat_persona_name: p.name, chat_persona_prompt: p.prompt, chat_reply_rules: p.reply_rules,
        chat_max_chars: p.max_chars, chat_context_window_sec: p.context_window_sec, chat_context_count: p.context_count,
      })
    } else if (!yang) {
      Object.assign(payload, {
        chat_persona_name: draft.chat_persona_name, chat_persona_prompt: draft.chat_persona_prompt,
        chat_reply_rules: draft.chat_reply_rules, chat_max_chars: draft.chat_max_chars,
        chat_context_window_sec: draft.chat_context_window_sec, chat_context_count: draft.chat_context_count,
      })
    }
    try {
      const d = await api('/api/sandbox/test', { method: 'POST', body: payload })
      const ms = Math.round(performance.now() - t0)
      setResult(d.ok ? { ok: true, reply: d.reply, ms } : { ok: false, error: d.error || '没有得到回复', ms })
    } catch (e) {
      setResult({ ok: false, error: e.message })
    } finally { setBusy(false) }
  }

  return (
    <div className="sandbox">
      <div className="stack" style={{ gap: 16 }}>
        <Note kind="warn">会真的调用模型，按次计费。用的是这里正在填写的设置（包括还没保存的 AI 服务和人设）。用哪个模型和机器人回复时一样：按「高级开关 → 本地模型」的分工来。</Note>
        <Card title="模拟一条群消息" eyebrow="INPUT">
          <div className="stack" style={{ gap: 14 }}>
            <Field label="消息内容">
              <TextArea value={message} onChange={setMessage} rows={3} placeholder={`例如：@${draft.bot_display_name || '机器人'} 今天吃什么好`} />
            </Field>
            <div className="form-grid">
              <Field label="谁发的"><TextInput value={sender} onChange={setSender} /></Field>
              <Field label="在哪个群"><TextInput value={group} onChange={setGroup} /></Field>
            </div>
            <details className="more">
              <summary>更多上下文（可不填）</summary>
              <div className="stack" style={{ gap: 14, marginTop: 12 }}>
                <Field label="之前的聊天" hint="一行一条，写成「昵称: 内容」">
                  <TextArea value={context} onChange={setContext} rows={4} placeholder={'群友乙: 晚上开黑吗\n群友甲: 来'} />
                </Field>
                <Field label="群的长期记忆" hint="模拟机器人对这个群记住的事">
                  <TextArea value={memory} onChange={setMemory} rows={2} />
                </Field>
              </div>
            </details>
            <div className="row">
              <button type="button" className="btn primary" onClick={run} disabled={busy || !message.trim()}>
                <PaperPlaneTilt weight="bold" />{busy ? '等模型回复…' : '发送测试'}
              </button>
            </div>
          </div>
        </Card>
        {(busy || result) && (
          <section className="ch card reply-card">
            <div className="card-head">
              <span className="eyebrow" aria-hidden="true">OUTPUT</span>
              {result?.ms !== undefined && <span className="tag num" style={{ marginLeft: 'auto' }}>{(result.ms / 1000).toFixed(1)} 秒</span>}
            </div>
            {busy && <div className="indet" />}
            {result?.ok && (
              <div className="bubble">
                <span className="b-name">{draft.bot_display_name || '机器人'}</span>
                <p>{result.reply || <span className="dim">（模型选择不回复）</span>}</p>
              </div>
            )}
            {result && !result.ok && <Note kind="need">{result.error}</Note>}
          </section>
        )}
      </div>
    </div>
  )
}

/* ---------------------------------------------------------- 备份与导入 */

const IMPORT_MARKERS = ['ai_backend', 'deepseek_model', 'wechat_backend']

function BackupCard() {
  const { reload, dirtyKeys, base } = useConfig()
  const toast = useToast()
  const fileRef = useRef(null)
  const [pending, setPending] = useState(null) // parsed backup awaiting confirmation
  const [busy, setBusy] = useState(false)

  const exportNow = async () => {
    try {
      const res = await fetch('/api/config/export', { cache: 'no-store' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `webot-config-${new Date().toISOString().slice(0, 10)}.json`
      a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 1000)
      toast.ok('备份已导出（密钥是打码的，不在备份里）')
    } catch (e) { toast.err(`导出失败：${e.message}`) }
  }
  const onFile = async (e) => {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    try {
      const data = JSON.parse(await f.text())
      if (!data || typeof data !== 'object' || !IMPORT_MARKERS.some((k) => k in data)) throw new Error('这不是 webot 导出的配置备份')
      setPending({ name: f.name, data })
    } catch (err) { toast.err(`读不了这个文件：${err.message}`) }
  }
  const doImport = async () => {
    setBusy(true)
    try {
      // The export leaves out some settings (voice recognition, …) and the
      // import endpoint writes defaults for anything missing, so fill the gaps
      // with the current settings before posting.
      await post('/api/config/import', { ...toPayload(base), ...pending.data })
      setPending(null)
      await reload()
      toast.ok('已导入并写入配置文件，重启机器人后生效')
    } catch (e) { toast.err(`导入失败：${e.message}`) } finally { setBusy(false) }
  }
  return (
    <section className="ch card backup">
      <div className="row wrap" style={{ gap: 12 }}>
        <div className="stack grow" style={{ gap: 2 }}>
          <span className="eyebrow" aria-hidden="true">BACKUP</span>
          <h2 style={{ fontSize: 'var(--fs-md)' }}>配置备份</h2>
          <span className="f-hint">把设置导出成 JSON 文件，或者从备份恢复。备份里不含密钥，也不含待办和语音设置；导入时这些保持现在的值。</span>
        </div>
        <button type="button" className="btn" onClick={exportNow}><DownloadSimple />导出备份</button>
        <button type="button" className="btn" onClick={() => fileRef.current?.click()}><UploadSimple />导入恢复</button>
        <input ref={fileRef} type="file" accept=".json,application/json" hidden onChange={onFile} />
      </div>
      {pending && (
        <Confirm
          title="导入这个备份？" confirmLabel="导入并写入" busy={busy}
          onCancel={() => setPending(null)} onConfirm={doImport}
        >
          <p>「{pending.name}」会<b>直接写入配置文件</b>，覆盖备份里有的设置。备份里打码的密钥不会覆盖现有密钥。</p>
          {dirtyKeys.length > 0 && <div style={{ marginTop: 10 }}><Note kind="warn">你还有 {dirtyKeys.length} 项修改没保存，导入后这些修改会丢掉。</Note></div>}
        </Confirm>
      )}
    </section>
  )
}
