import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowRight, Plus, Trash, ArrowCounterClockwise, CaretDown, CaretRight, DownloadSimple, CheckCircle } from '@phosphor-icons/react'
import { get, post } from '../api'
import { useConfig, useField } from '../store/config'
import { useToast } from '../store/toast'
import { useGroups } from '../lib/groups'
import { navigate } from '../lib/router'
import { PageHead, SecDeco, Switch, Field, Note, Empty, ChipsInput, Seg, TextInput, TextArea, NumberInput, Select, Confirm } from './ui'
import { CfgText, CfgNumber, CfgSelect, CfgSecret, CfgChips, CfgSeg } from './cfg'

// The feature modules, laid out like the effect-chain grid of the design:
// one tile per feature; the selected tile's settings open on the right.
const MODULES = [
  { id: 'fun', code: 'FUN', name: '趣味抽签', key: 'fun_enabled', prefix: /^fun_/, line: () => '签文和概率可改' },
  { id: 'proactive', code: 'PRO', name: '主动发言', key: 'proactive_enabled', prefix: /^proactive_/, line: (c) => `看 ${c.proactive_rate_window_sec} 秒内的热度` },
  { id: 'sticky', code: 'STK', name: '粘性提及', key: 'sticky_mention_enabled', prefix: /^sticky_/, line: (c) => `等下一句 ${c.sticky_mention_ttl_sec} 秒` },
  { id: 'followup', code: 'FUP', name: '跟聊', key: 'followup_listen_enabled', prefix: /^followup_/, line: (c) => `回复后听 ${c.followup_listen_ttl_sec} 秒` },
  { id: 'burst', code: 'BST', name: '连发合并', key: 'message_burst_enabled', prefix: /^message_burst_/, line: (c) => `等 ${c.message_burst_window_sec} 秒再处理` },
  { id: 'welcome', code: 'WEL', name: '欢迎新人', key: 'welcome_enabled', prefix: /^welcome_/, line: () => '模板按群分配' },
  { id: 'todo', code: 'TODO', name: '群聊待办', key: 'todo_enabled', prefix: /^todo_/, line: (c) => `每群最多 ${c.todo_max_per_group} 条` },
  { id: 'feishu', code: 'FSU', name: '飞书同步', key: 'feishu_export_enabled', prefix: /^feishu_/, line: (c) => FEISHU_MODES[c.feishu_export_mode] || c.feishu_export_mode },
  { id: 'voice', code: 'ASR', name: '语音识别', key: 'voice_asr_enabled', prefix: /^voice_/, line: (c) => (c.voice_asr_backend === 'openai_whisper' ? '云端识别' : `本地模型 ${c.voice_local_model}`) },
  { id: 'search', code: 'WEB', name: '联网搜索', key: 'web_search_enabled', prefix: /^web_search_/, line: (c) => `最多 ${c.web_search_max_results} 条结果` },
  { id: 'private', code: 'PVT', name: '私聊', key: 'private_chat_enabled', prefix: /^private_/, line: (c) => (c.private_chats?.length ? `${c.private_chats.length} 个联系人` : '所有私聊') },
]

const FEISHU_MODES = { knowledge: '知识库', bitable: '多维表格', spreadsheet: '电子表格', docx: '云文档' }

export default function ModulesPanel({ selected }) {
  const { status, draft, base, error, reload } = useConfig()
  const current = MODULES.find((m) => m.id === selected) || MODULES[0]
  const ready = !!draft

  return (
    <div className="split">
      <div className="main-scroll scroll">
        <div className="page wide">
          <PageHead no="02" en="MODULES" title="功能模块" sub="点一格看说明和设置。斜纹格子表示这个功能关着" />
          {status === 'error' && !ready && <Empty title="设置读取失败">{error}<div style={{ marginTop: 10 }}><button type="button" className="btn" onClick={reload}>重试</button></div></Empty>}
          {!ready && status !== 'error' && <Empty title="正在读取设置…" />}
          {ready && (
            <>
              <div className="flow-head" aria-hidden="true"><span>IN</span><i /><span>OUT</span></div>
              <div className="tiles" role="listbox" aria-label="功能模块">
                {MODULES.map((m) => {
                  const on = !!draft[m.key]
                  const sel = m.id === current.id
                  const dirty = Object.keys(draft).some((k) => m.prefix.test(k) && JSON.stringify(draft[k]) !== JSON.stringify(base[k]))
                  return (
                    <button
                      key={m.id} type="button" role="option" aria-selected={sel ? 'true' : 'false'}
                      className={`tile chc${on ? '' : ' off'}${sel ? ' sel' : ''}`}
                      onClick={() => navigate(`modules/${m.id}`)}
                    >
                      <span className="t-top"><b className="t-code">{m.code}</b><span className="t-name">{m.name}</span></span>
                      <span className="t-state"><i className={on ? 'on' : ''} />{on ? '开' : '关'}{dirty && <span className="t-dirty">未保存</span>}</span>
                      <span className="t-line">{m.line(draft)}</span>
                    </button>
                  )
                })}
                <button type="button" className="tile chc link-tile" onClick={() => navigate('advanced')}>
                  <span className="t-top"><b className="t-code">ADV</b><span className="t-name">更多开关</span></span>
                  <span className="t-line">回复管线、记忆、发送方式、表情包、图片识别…</span>
                  <span className="t-go">去高级开关 <ArrowRight /></span>
                </button>
              </div>
              <Note>这里的开关和参数都进同一份设置，底部会出现保存条；抽签签文、欢迎模板各有自己的保存按钮，保存后马上生效。</Note>
            </>
          )}
        </div>
      </div>
      <aside className="aside scroll" aria-label={`${current.name}的设置`}>
        <div className="aside-inner">
          {ready && <ModuleDetail m={current} />}
        </div>
      </aside>
    </div>
  )
}

function ModuleDetail({ m }) {
  const [on, setOn] = useField(m.key)
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row">
        <SecDeco no="03" en="MODULE" small />
        <span className="sec-rule" aria-hidden="true" />
      </div>
      <div className="mod-head">
        <span className="mod-code" aria-hidden="true">{m.code}</span>
        <div className="stack" style={{ gap: 4 }}>
          <h2 className="mod-name">{m.name}</h2>
          <div className="row">
            <Switch checked={!!on} onChange={setOn} label={`${m.name}开关`} />
            <span className="f-key">{m.key.toUpperCase()}</span>
          </div>
        </div>
      </div>
      <Body id={m.id} on={!!on} />
    </div>
  )
}

function Body({ id, on }) {
  switch (id) {
    case 'fun': return <FunBody />
    case 'proactive': return <ProactiveBody />
    case 'sticky': return (
      <>
        <p className="mod-desc">有人只发了一个「@机器人」没写内容时，把他接下来的一句话当作是对机器人说的。用一次就失效。</p>
        <CfgNumber k="sticky_mention_ttl_sec" label="最多等多久" envKey="STICKY_MENTION_TTL_SEC" suffix="秒" />
      </>
    )
    case 'followup': return (
      <>
        <p className="mod-desc">机器人刚回复完的一小段时间里，群友接着说的短句（「那明天呢」）也会被看一眼，像在接它的话就继续回。先用本地规则筛，筛过了才调用模型。</p>
        <CfgNumber k="followup_listen_ttl_sec" label="回复后听多久" envKey="FOLLOWUP_LISTEN_TTL_SEC" suffix="秒" />
        <CfgNumber k="followup_listen_context_count" label="带几条上下文" envKey="FOLLOWUP_LISTEN_CONTEXT_COUNT" suffix="条" />
        <CfgNumber k="followup_listen_max_message_chars" label="只看多短的消息" envKey="FOLLOWUP_LISTEN_MAX_MESSAGE_CHARS" suffix="字以内" />
        <Note kind="cy">高级开关里的 FOLLOWUP_MODE=declared 会改成让模型自己说要不要等下一句（要先开 PIPELINE_V2）。</Note>
      </>
    )
    case 'burst': return (
      <>
        <p className="mod-desc">同一个人连着发好几条短消息时（微信里一句话拆成几个气泡），稍等一下，把它们合成一句再处理。</p>
        <CfgNumber k="message_burst_window_sec" label="等多久" envKey="MESSAGE_BURST_WINDOW_SEC" suffix="秒" />
        <CfgNumber k="message_burst_max_messages" label="最多合几条" envKey="MESSAGE_BURST_MAX_MESSAGES" suffix="条" />
        <CfgNumber k="message_burst_max_chars" label="合起来最多多少字" envKey="MESSAGE_BURST_MAX_CHARS" suffix="字" />
      </>
    )
    case 'welcome': return <WelcomeBody on={on} />
    case 'todo': return <TodoBody />
    case 'feishu': return <FeishuBody />
    case 'voice': return <VoiceBody />
    case 'search': return (
      <>
        <p className="mod-desc">群友明确要查东西时（「搜一下」「查一下」「最新的」），先上网搜，再带着搜索结果回答。</p>
        <CfgNumber k="web_search_max_results" label="用几条搜索结果" envKey="WEB_SEARCH_MAX_RESULTS" suffix="条" />
        <CfgNumber k="web_search_timeout_sec" label="搜索最多等多久" envKey="WEB_SEARCH_TIMEOUT_SEC" suffix="秒" />
        <CfgNumber k="web_search_reply_max_chars" label="回答最长" envKey="WEB_SEARCH_REPLY_MAX_CHARS" suffix="字" />
        <CfgText k="web_search_region" label="搜索地区" envKey="WEB_SEARCH_REGION" mono hint="默认 cn-zh（中文结果）" />
      </>
    )
    case 'private': return <PrivateBody />
    default: return null
  }
}

/* ---------------------------------------------------------------- 主动发言 */

function ProactiveBody() {
  const { errors } = useConfig()
  return (
    <>
      <p className="mod-desc">没人叫它的时候，按群里聊得热不热闹，偶尔自己插一句。四档热度是「每分钟多少条消息」，越热闹越容易插话。</p>
      <CfgNumber k="proactive_rate_window_sec" label="按多长时间算热度" envKey="PROACTIVE_RATE_WINDOW_SEC" suffix="秒" />
      <div className="form-grid">
        <CfgNumber k="proactive_rate_quiet" label="安静" envKey="QUIET" suffix="条/分" />
        <CfgNumber k="proactive_rate_casual" label="随口" envKey="CASUAL" suffix="条/分" />
        <CfgNumber k="proactive_rate_lively" label="活跃" envKey="LIVELY" suffix="条/分" />
        <CfgNumber k="proactive_rate_burst" label="爆发" envKey="BURST" suffix="条/分" />
      </div>
      {errors.proactive_rate_order && <div className="f-err" role="alert">{errors.proactive_rate_order}</div>}
      <Note kind="cy">高级开关里可以把判断方式从「掷骰子」换成「打分 + 每小时配额」（PROACTIVE_GATE_MODE=score）。</Note>
    </>
  )
}

/* ---------------------------------------------------------------- 私聊 */

function PrivateBody() {
  const [value, setValue] = useField('private_chats')
  const names = Array.isArray(value) ? value : []
  const [wantSome, setWantSome] = useState(false)
  const mode = names.length || wantSome ? 'some' : 'all'
  return (
    <>
      <p className="mod-desc">一对一私聊里，不用 @ 也会回复。</p>
      <Field label="回复哪些私聊" envKey="PRIVATE_CHATS">
        <div><Seg small value={mode} onChange={(v) => { setWantSome(v === 'some'); if (v === 'all') setValue([]) }} options={[{ value: 'all', label: '所有私聊' }, { value: 'some', label: '只回这些人' }]} /></div>
      </Field>
      {mode === 'some' && <ChipsInput split={false} value={names} onChange={setValue} placeholder="输入对方的微信昵称后回车（一次一个）" />}
    </>
  )
}

/* ---------------------------------------------------------------- 待办 */

function TodoBody() {
  const [groupsVal, setGroups] = useField('todo_groups')
  const groups = useGroups()
  const list = Array.isArray(groupsVal) ? groupsVal.filter((g) => g && g !== '*') : []
  const nameOf = (id) => (groups.list || []).find((g) => g.chat_id === id)?.group_name || id
  const [wantSome, setWantSome] = useState(false)
  const mode = list.length || wantSome ? 'some' : 'all'
  const pickable = (groups.list || []).filter((g) => !list.includes(g.chat_id))
  return (
    <>
      <p className="mod-desc">群友在群里说「记一下……」就记成一条待办，说「搞定了」「删掉」就完成或删除。记下的待办在「群聊待办」页里管理。</p>
      <Field label="在哪些群里用" envKey="TODO_GROUPS">
        <div><Seg small value={mode} onChange={(v) => { setWantSome(v === 'some'); if (v === 'all') setGroups(['*']) }} options={[{ value: 'all', label: '所有群' }, { value: 'some', label: '只在这些群' }]} /></div>
      </Field>
      {mode === 'some' && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="chips">
            {list.map((id) => (
              <span className="chip" key={id}><span title={id}>{nameOf(id)}</span>
                <button type="button" aria-label={`移除 ${nameOf(id)}`} onClick={() => { const n = list.filter((x) => x !== id); setGroups(n.length ? n : ['*']) }}>×</button>
              </span>
            ))}
            {!list.length && <span className="f-hint">还没选群；不选就是所有群。</span>}
          </div>
          {pickable.length > 0 && (
            <Select value="" onChange={(v) => v && setGroups([...list, v])} options={[{ value: '', label: '添加一个群…' }, ...pickable.map((g) => ({ value: g.chat_id, label: g.group_name || g.chat_id }))]} />
          )}
        </div>
      )}
      <div className="form-grid">
        <CfgNumber k="todo_max_per_group" label="每个群最多" envKey="TODO_MAX_PER_GROUP" suffix="条" />
        <CfgNumber k="todo_completed_retention_days" label="已完成保留" envKey="TODO_COMPLETED_RETENTION_DAYS" suffix="天" hint="0 = 一直保留" />
        <CfgNumber k="todo_deleted_retention_days" label="已删除保留" envKey="TODO_DELETED_RETENTION_DAYS" suffix="天" hint="0 = 一直保留" />
      </div>
      <CfgChips k="todo_add_keywords" label="记待办的说法" envKey="TODO_ADD_KEYWORDS" minItems={1} />
      <CfgChips k="todo_complete_keywords" label="完成待办的说法" envKey="TODO_COMPLETE_KEYWORDS" minItems={1} />
      <CfgChips k="todo_delete_keywords" label="删除待办的说法" envKey="TODO_DELETE_KEYWORDS" minItems={1} />
      <button type="button" className="link" onClick={() => navigate('todo')}>去管理群聊待办 ›</button>
    </>
  )
}

/* ---------------------------------------------------------------- 飞书 */

function FeishuBody() {
  const [mode] = useField('feishu_export_mode')
  const [autoOn, setAuto] = useField('feishu_auto_sync_enabled')
  return (
    <>
      <p className="mod-desc">群友 @机器人说「同步到飞书」时，把最近的聊天整理后写进飞书。需要在飞书开放平台建一个应用。</p>
      <CfgText k="feishu_app_id" label="App ID" envKey="FEISHU_APP_ID" mono />
      <CfgSecret k="feishu_app_secret" label="App Secret" envKey="FEISHU_APP_SECRET" />
      <CfgSelect k="feishu_export_mode" label="写到哪里" envKey="FEISHU_EXPORT_MODE" options={[
        { value: 'knowledge', label: '知识库（推荐）' }, { value: 'bitable', label: '多维表格' },
        { value: 'spreadsheet', label: '电子表格' }, { value: 'docx', label: '云文档' },
      ]} />
      <CfgNumber k="feishu_export_window_hours" label="同步最近多久的消息" envKey="FEISHU_EXPORT_WINDOW_HOURS" suffix="小时" />
      <CfgChips k="feishu_export_trigger_keywords" label="触发说法" envKey="FEISHU_EXPORT_TRIGGER_KEYWORDS" />
      {mode === 'knowledge' && (
        <>
          <CfgText k="feishu_knowledge_base_name" label="知识库名称" envKey="FEISHU_KNOWLEDGE_BASE_NAME" />
          <CfgText k="feishu_knowledge_folder_token" label="放在哪个文件夹" envKey="FEISHU_KNOWLEDGE_FOLDER_TOKEN" mono hint="可不填，填飞书文件夹的 token" />
          <div className="toggle-row">
            <div className="tr-text">
              <span className="tr-title">自动沉淀<span className="f-key">FEISHU_AUTO_SYNC_ENABLED</span></span>
              <div className="f-hint">消息够多时自动写进知识库，不在群里回复</div>
            </div>
            <Switch checked={!!autoOn} onChange={setAuto} label="自动沉淀" />
          </div>
          {autoOn && (
            <div className="form-grid">
              <CfgNumber k="feishu_auto_sync_min_messages" label="攒够多少条" envKey="FEISHU_AUTO_SYNC_MIN_MESSAGES" suffix="条" />
              <CfgNumber k="feishu_auto_sync_cooldown_sec" label="两次之间至少隔" envKey="FEISHU_AUTO_SYNC_COOLDOWN_SEC" suffix="秒" />
            </div>
          )}
        </>
      )}
      {mode === 'bitable' && (
        <>
          <CfgText k="feishu_bitable_app_token" label="多维表格 app token" envKey="FEISHU_BITABLE_APP_TOKEN" mono />
          <CfgText k="feishu_bitable_table_id" label="数据表 ID" envKey="FEISHU_BITABLE_TABLE_ID" mono />
        </>
      )}
      {mode === 'spreadsheet' && (
        <>
          <CfgText k="feishu_spreadsheet_token" label="电子表格 token" envKey="FEISHU_SPREADSHEET_TOKEN" mono />
          <CfgText k="feishu_spreadsheet_range" label="写入范围" envKey="FEISHU_SPREADSHEET_RANGE" mono hint="例如 Sheet1!A:H" />
        </>
      )}
      {mode === 'docx' && <CfgText k="feishu_doc_folder_token" label="文档放在哪个文件夹" envKey="FEISHU_DOC_FOLDER_TOKEN" mono />}
    </>
  )
}

/* ---------------------------------------------------------------- 语音 */

function VoiceBody() {
  const [backend] = useField('voice_asr_backend')
  return (
    <>
      <p className="mod-desc">把群里的语音消息转成文字，机器人就能「听懂」语音。</p>
      <CfgSeg k="voice_asr_backend" label="在哪里识别" envKey="VOICE_ASR_BACKEND" options={[{ value: 'local_whisper', label: '本机模型' }, { value: 'openai_whisper', label: '云端接口' }]} />
      {backend === 'openai_whisper' ? (
        <>
          <CfgSecret k="voice_openai_api_key" label="API Key" envKey="VOICE_OPENAI_API_KEY" optional hint="不填就用 AI 服务里的 OpenAI Key" />
          <CfgText k="voice_openai_base_url" label="接口地址" envKey="VOICE_OPENAI_BASE_URL" mono placeholder="留空用官方地址" />
        </>
      ) : (
        <>
          <CfgSeg k="voice_local_model" label="本机模型大小" envKey="VOICE_LOCAL_MODEL" options={[
            { value: 'tiny', label: 'tiny' }, { value: 'base', label: 'base' }, { value: 'small', label: 'small' }, { value: 'medium', label: 'medium' },
          ]} hint="越大越准，也越慢、越占内存。一般用 small" />
          <ModelDownload />
        </>
      )}
      <CfgSeg k="voice_asr_language" label="识别语言" envKey="VOICE_ASR_LANGUAGE" options={[{ value: 'zh', label: '中文' }, { value: 'zh-en', label: '中英混合' }, { value: 'auto', label: '自动' }]} />
    </>
  )
}

function ModelDownload() {
  const [model] = useField('voice_local_model')
  const [st, setSt] = useState(null)
  const [err, setErr] = useState('')
  const check = useCallback(async () => {
    try { setSt(await get('/api/voice/model-status', { model })); setErr('') } catch (e) { setErr(e.message) }
  }, [model])
  useEffect(() => { setSt(null); check() }, [check])
  const active = st && (st.phase === 'downloading' || st.phase === 'installing')
  useEffect(() => {
    if (!active) return undefined
    const t = setInterval(check, 1500)
    return () => clearInterval(t)
  }, [active, check])
  const start = async () => {
    try { await post('/api/voice/download-model', { model }); check() } catch (e) { setErr(e.message) }
  }
  return (
    <div className="dl-box">
      <div className="row">
        <span className="mono">whisper-{model}</span>
        <span className="spacer" />
        {!st && !err && <span className="spin" />}
        {st?.downloaded && <span className="tag ok"><CheckCircle weight="fill" />已下载</span>}
        {st && !st.downloaded && !active && <button type="button" className="btn sm" onClick={start}><DownloadSimple />{st.phase === 'error' ? '重新下载' : '下载模型'}</button>}
        {active && <span className="tag pend">{st.phase === 'installing' ? '安装中' : '下载中'}</span>}
      </div>
      {active && <div className="indet" style={{ marginTop: 8 }} />}
      {st?.phase === 'error' && st.error && <div className="f-err" style={{ marginTop: 6 }}>{st.error}</div>}
      {err && <div className="f-err" style={{ marginTop: 6 }}>{err}</div>}
    </div>
  )
}

/* ---------------------------------------------------------------- 抽签 */

function FunBody() {
  return (
    <>
      <p className="mod-desc">群友 @机器人说「抽签」时，按概率抽一支签回给他。下面可以改每一档签的名字、概率和签文。</p>
      <LotsEditor />
    </>
  )
}

function LotsEditor() {
  const toast = useToast()
  const [data, setData] = useState(null)
  const [orig, setOrig] = useState(null)
  const [err, setErr] = useState('')
  const [open, setOpen] = useState(-1)
  const [busy, setBusy] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)
  const load = useCallback(async () => {
    try { const d = await get('/api/lots'); setData(d.config); setOrig(d.config); setErr('') } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => { load() }, [load])
  if (err) return <Note kind="need">签文读取失败：{err}</Note>
  if (!data) return <div className="row"><span className="spin" />读取签文…</div>
  const total = data.weights.reduce((a, b) => a + (Number(b) || 0), 0) || 1
  const dirty = JSON.stringify(data) !== JSON.stringify(orig)
  const setLevel = (i, patch) => setData((d) => ({ ...d, levels: d.levels.map((l, j) => (j === i ? { ...l, ...patch } : l)) }))
  const setWeight = (i, w) => setData((d) => ({ ...d, weights: d.weights.map((x, j) => (j === i ? w : x)) }))
  const bad = data.weights.some((w) => !(Number(w) > 0)) || data.levels.some((l) => !l.name?.trim() || !l.phrases?.length)
  const save = async (payload, msg) => {
    setBusy(true)
    try { await post('/api/lots', payload); await load(); toast.ok(msg) } catch (e) { toast.err(`保存失败：${e.message}`) } finally { setBusy(false) }
  }
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="lots">
        {data.levels.map((lvl, i) => {
          const pct = ((Number(data.weights[i]) || 0) / total) * 100
          return (
            <div key={i} className="lot">
              <button type="button" className="lot-head" aria-expanded={open === i} onClick={() => setOpen(open === i ? -1 : i)}>
                {open === i ? <CaretDown /> : <CaretRight />}
                <span className="lot-emoji">{lvl.emoji}</span>
                <b className="grow ellipsis">{lvl.name}</b>
                <span className="num dim">{pct.toFixed(1)}%</span>
                <span className="dim">{lvl.phrases.length} 条</span>
              </button>
              <div className="bar" aria-hidden="true"><i style={{ width: `${pct}%` }} /></div>
              {open === i && (
                <div className="lot-body stack" style={{ gap: 10 }}>
                  <div className="form-grid three">
                    <Field label="名称"><TextInput value={lvl.name} onChange={(v) => setLevel(i, { name: v })} /></Field>
                    <Field label="图标"><TextInput value={lvl.emoji} onChange={(v) => setLevel(i, { emoji: v })} /></Field>
                    <Field label="权重" error={Number(data.weights[i]) > 0 ? '' : '要大于 0'}><NumberInput value={data.weights[i]} onChange={(v) => setWeight(i, v)} invalid={!(Number(data.weights[i]) > 0)} /></Field>
                  </div>
                  <Field label="签文" hint="一行一条" error={lvl.phrases.length ? '' : '至少要有一条'}>
                    <TextArea rows={5} value={lvl.phrases.join('\n')} onChange={(t) => setLevel(i, { phrases: t.split('\n').map((x) => x.trim()).filter(Boolean) })} />
                  </Field>
                </div>
              )}
            </div>
          )
        })}
      </div>
      <div className="row">
        <button type="button" className="btn primary sm" disabled={!dirty || bad || busy} onClick={() => save(data, '签文已保存，马上生效')}>保存签文</button>
        {dirty && <button type="button" className="btn sm ghost" onClick={() => setData(orig)}>撤销</button>}
        <span className="spacer" />
        <button type="button" className="btn sm ghost" onClick={() => setConfirmReset(true)}><ArrowCounterClockwise />恢复默认</button>
      </div>
      {confirmReset && (
        <Confirm title="恢复默认签文？" danger confirmLabel="恢复默认" busy={busy}
          onCancel={() => setConfirmReset(false)}
          onConfirm={async () => { await save({ weights: [], levels: [] }, '已恢复默认签文'); setConfirmReset(false) }}>
          自己改过的签文会全部删掉，换回程序自带的那一套。
        </Confirm>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- 欢迎 */

function WelcomeBody({ on }) {
  return (
    <>
      <p className="mod-desc">有新人进群时发一句欢迎。可以准备几套欢迎词，按群分配，也可以对某个群关掉。</p>
      {!on && <Note>开关关着时不会发欢迎词；模板可以先准备好。</Note>}
      <WelcomeEditor />
    </>
  )
}

const DISABLED = '__disabled__'

function WelcomeEditor() {
  const toast = useToast()
  const groups = useGroups()
  const [data, setData] = useState(null)
  const [orig, setOrig] = useState(null)
  const [err, setErr] = useState('')
  const [tab, setTab] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    try {
      const d = await get('/api/welcome/templates')
      setData(d.data); setOrig(d.data); setErr('')
      setTab((t) => t || d.data?.templates?.[0]?.id || '')
    } catch (e) { setErr(e.message) }
  }, [])
  useEffect(() => { load() }, [load])
  const tplOptions = useMemo(() => (data?.templates || []).map((t) => ({ value: t.id, label: t.name || t.id })), [data])
  if (err) return <Note kind="need">欢迎模板读取失败：{err}</Note>
  if (!data) return <div className="row"><span className="spin" />读取模板…</div>
  const tpl = data.templates.find((t) => t.id === tab) || data.templates[0]
  const dirty = JSON.stringify(data) !== JSON.stringify(orig)
  const setTpl = (patch) => setData((d) => ({ ...d, templates: d.templates.map((t) => (t.id === tpl.id ? { ...t, ...patch } : t)) }))
  const addTpl = () => {
    const id = `tpl_${Date.now().toString(36)}`
    setData((d) => ({ ...d, templates: [...d.templates, { id, name: `模板 ${d.templates.length + 1}`, message: '欢迎 {new_member} 加入！' }] }))
    setTab(id)
  }
  const delTpl = () => {
    if (data.templates.length <= 1) return
    setData((d) => {
      const templates = d.templates.filter((t) => t.id !== tpl.id)
      const fallback = templates[0].id
      const mapping = Object.fromEntries(Object.entries(d.group_mapping || {}).map(([k, v]) => [k, v === tpl.id ? fallback : v]))
      return { ...d, templates, group_mapping: mapping, default_template: d.default_template === tpl.id ? fallback : d.default_template }
    })
    setTab('')
  }
  const mapping = data.group_mapping || {}
  const mapped = Object.keys(mapping)
  const nameOf = (id) => (groups.list || []).find((g) => g.chat_id === id)?.group_name || id
  const unassigned = (groups.list || []).filter((g) => !mapped.includes(g.chat_id))
  const doubleAt = /@\{new_member\}/.test(tpl?.message || '')
  const save = async () => {
    setBusy(true)
    try { await post('/api/welcome/templates', data); await load(); toast.ok('欢迎模板已保存，马上生效') } catch (e) { toast.err(`保存失败：${e.message}`) } finally { setBusy(false) }
  }
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="row wrap" style={{ gap: 6 }}>
        <Seg small value={tpl?.id} onChange={setTab} options={tplOptions} label="欢迎模板" />
        <button type="button" className="btn sm" onClick={addTpl}><Plus />新建</button>
      </div>
      {tpl && (
        <div className="stack" style={{ gap: 10 }}>
          <Field label="模板名称"><TextInput value={tpl.name} onChange={(v) => setTpl({ name: v })} /></Field>
          <Field label="欢迎词" hint="{new_member} 会换成「@新成员」，前面不用再写 @">
            <TextArea rows={3} value={tpl.message} onChange={(v) => setTpl({ message: v })} />
          </Field>
          {doubleAt && (
            <Note kind="warn">
              这条里写了「@{'{new_member}'}」，发出去会变成两个 @。
              <button type="button" className="link" style={{ marginLeft: 6 }} onClick={() => setTpl({ message: tpl.message.replace(/@\{new_member\}/g, '{new_member}') })}>去掉多余的 @</button>
            </Note>
          )}
          <div className="row">
            <button type="button" className="btn sm" onClick={() => setTpl({ message: `${tpl.message}{new_member}` })}>插入新成员</button>
            <span className="spacer" />
            <button type="button" className="btn sm danger" onClick={delTpl} disabled={data.templates.length <= 1}><Trash />删除这个模板</button>
          </div>
        </div>
      )}
      <div className="label" style={{ marginTop: 6 }}>按群分配</div>
      <Field label="默认用哪个模板">
        <Select value={data.default_template} onChange={(v) => setData((d) => ({ ...d, default_template: v }))} options={tplOptions} />
      </Field>
      {mapped.map((cid) => (
        <div key={cid} className="map-row">
          <span className="ellipsis" title={cid}>{nameOf(cid)}</span>
          <Select
            value={mapping[cid]} aria-label={`${nameOf(cid)} 用的模板`}
            onChange={(v) => setData((d) => ({ ...d, group_mapping: { ...d.group_mapping, [cid]: v } }))}
            options={[...tplOptions, { value: DISABLED, label: '这个群不发欢迎词' }]}
          />
          <button type="button" className="icon-btn sm" aria-label="改回默认" title="改回默认" onClick={() => setData((d) => { const m = { ...d.group_mapping }; delete m[cid]; return { ...d, group_mapping: m } })}>×</button>
        </div>
      ))}
      {unassigned.length > 0 && (
        <Select value="" onChange={(v) => v && setData((d) => ({ ...d, group_mapping: { ...d.group_mapping, [v]: d.default_template } }))}
          options={[{ value: '', label: `给某个群单独设置…（${unassigned.length} 个群）` }, ...unassigned.map((g) => ({ value: g.chat_id, label: g.group_name || g.chat_id }))]} />
      )}
      <div className="row">
        <button type="button" className="btn primary sm" disabled={!dirty || busy} onClick={save}>保存模板</button>
        {dirty && <button type="button" className="btn sm ghost" onClick={() => setData(orig)}>撤销</button>}
      </div>
    </div>
  )
}
