import { useCallback, useEffect, useMemo, useState } from 'react'
import { LockKey, Desktop, Warning, Copy, Sparkle } from '@phosphor-icons/react'
import { api, get } from '../api'
import { useConfig } from '../store/config'
import { useStatus } from '../store/status'
import { useToast } from '../store/toast'
import { isMasked } from '../lib/schema'
import { Note, Empty, Switch, Seg, NumberInput, TextInput, SecretInput } from './ui'

// The allow-listed advanced switches (GET/POST /api/advanced). The server owns
// the list: type, range, default, label, help, dependencies
// (src/web/advanced_config.py), so this code only renders what it gets.
// AdvancedFields shows some of the groups inside another page (the relay,
// creators and WeChat pages of the console).

function checkLocal(field, value) {
  const v = String(value ?? '').trim()
  if (/[\x00-\x1f\x7f]/.test(String(value ?? ''))) return '不能包含换行'
  if (field.type === 'int' || field.type === 'float') {
    const re = field.type === 'int' ? /^[+-]?\d+$/ : /^[+-]?(\d+(\.\d*)?|\.\d+)$/
    if (!re.test(v)) return field.type === 'int' ? '要填整数' : '要填数字'
    const n = Number(v)
    if (field.min !== undefined && n < field.min) return `不能小于 ${field.min}`
    if (field.max !== undefined && (field.max_exclusive ? n >= field.max : n > field.max)) return field.max_exclusive ? `要小于 ${field.max}` : `不能大于 ${field.max}`
  }
  if (/(^|\s)#/.test(v)) return '不能包含「 #」（.env 会把后面当成注释）'
  return ''
}

function rangeText(f) {
  if (f.type !== 'int' && f.type !== 'float') return ''
  if (f.max_exclusive) return `范围 ${f.min} 到小于 ${f.max}`
  return `范围 ${f.min}–${f.max}`
}

/** A random secret: 32 bytes, base64url (43 characters). */
export function randomToken() {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * Some or all groups of the advanced switches, with their own save bar.
 * ``only``: group ids to show (all when missing). ``onSaved`` runs after a save.
 */
export function AdvancedFields({ only, onSaved, intro = true }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [draft, setDraft] = useState({})
  const [serverErrors, setServerErrors] = useState({})
  const [saving, setSaving] = useState(false)
  const { draft: mainDraft, base } = useConfig()
  const status = useStatus()
  const toast = useToast()
  const onlyKey = (only || []).join(',')

  const load = useCallback(async () => {
    try {
      const d = await get('/api/advanced')
      const wanted = onlyKey ? onlyKey.split(',') : null
      const groups = wanted ? wanted.map((id) => d.groups.find((g) => g.id === id)).filter(Boolean) : d.groups
      setData({ ...d, groups })
      setDraft(Object.fromEntries(groups.flatMap((g) => g.fields.map((f) => [f.key, f.value]))))
      setServerErrors({})
      setErr('')
    } catch (e) { setErr(e.message) }
  }, [onlyKey])
  useEffect(() => { load() }, [load])

  const fields = useMemo(() => (data ? Object.fromEntries(data.groups.flatMap((g) => g.fields.map((f) => [f.key, f]))) : {}), [data])
  const dirty = useMemo(() => Object.keys(draft).filter((k) => fields[k] && String(draft[k]) !== String(fields[k].value)), [draft, fields])
  const localErrors = useMemo(() => {
    const out = {}
    for (const k of dirty) { const m = checkLocal(fields[k], draft[k]); if (m) out[k] = m }
    return out
  }, [dirty, draft, fields])

  // A dependency may also point at a main setting (store/config.jsx).
  const valueOf = (key) => {
    if (key in draft) return String(draft[key])
    const main = mainDraft || base
    if (!main) return ''
    const v = main[key.toLowerCase()]
    return typeof v === 'boolean' ? String(v) : String(v ?? '')
  }
  const unmet = (f) => (f.requires || []).filter((r) => (r.is !== undefined ? valueOf(r.key) !== r.is : valueOf(r.key) === r.not))
  const wechatBackend = base?.wechat_backend

  const save = async () => {
    setSaving(true)
    try {
      const values = Object.fromEntries(dirty.map((k) => [k, draft[k]]))
      const res = await api('/api/advanced', { method: 'POST', body: { values } })
      if (!res.ok) {
        setServerErrors(res.errors || {})
        toast.err(res.error || '保存失败')
        return
      }
      await load()
      if (onSaved) onSaved()
      toast.ok(status.running ? '已保存。在控制台停止再启动机器人后生效' : '已保存，下次启动机器人时生效', { ms: 6000 })
    } catch (e) {
      toast.err(`保存失败：${e.message}`)
    } finally { setSaving(false) }
  }

  if (err) return <Empty title="读取失败">{err}<div style={{ marginTop: 10 }}><button type="button" className="btn" onClick={load}>重试</button></div></Empty>
  if (!data) return <Empty title="正在读取…" />

  const errorCount = Object.keys(localErrors).length
  const titles = data.groups.map((g) => g.title).join('、')
  return (
    <>
      {!onlyKey && (
        <div className="adv-nav" aria-label="分组">
          {data.groups.map((g) => (
            <button key={g.id} type="button" className="btn sm" onClick={() => document.getElementById(`adv-${g.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
              <span className="latin" style={{ fontWeight: 700 }}>{g.code}</span>{g.title}
            </button>
          ))}
        </div>
      )}
      {intro && (
        <Note kind="cy">
          这些设置在机器人启动时读取。保存后在控制台里停止再启动机器人就生效。
          {data.local_overrides?.length > 0 && <> 标着「被 .env.local 覆盖」的项，完全退出 webot 再打开后会以那个文件里的值为准。</>}
        </Note>
      )}
      {serverErrors._ && <div style={{ marginTop: 12 }}><Note kind="need">{serverErrors._}</Note></div>}
      {data.groups.map((g) => (
        <section key={g.id} id={`adv-${g.id}`} className="ch card adv-group">
          <div className="adv-head">
            <span className="adv-code" aria-hidden="true">{g.code}</span>
            <div className="stack" style={{ gap: 2 }}>
              <h2>{g.title}</h2>
              <p className="f-hint">{g.desc}</p>
            </div>
          </div>
          <div className="adv-fields">
            {g.fields.map((f) => {
              const missing = unmet(f)
              const error = localErrors[f.key] || serverErrors[f.key]
              const changed = dirty.includes(f.key)
              return (
                <div key={f.key} className={`adv-field${missing.length ? ' idle' : ''}${changed ? ' changed' : ''}`}>
                  <div className="af-text">
                    <div className="af-title">
                      <span>{f.label}</span>
                      <span className="f-key">{f.key}</span>
                      {changed && <span className="tag pend">已改</span>}
                      {data.local_overrides?.includes(f.key) && <span className="tag warn" title="这台电脑的 .env.local 里也写了这一项"><LockKey />被 .env.local 覆盖</span>}
                      {f.platform && <span className={`tag ${wechatBackend && !(f.platform_backends || ['wcdb']).includes(wechatBackend) ? 'warn' : ''}`} title={f.platform}><Desktop />{f.platform_label || '仅 Windows'}</span>}
                    </div>
                    {f.help && <div className="f-hint">{f.help}{rangeText(f) && ` ${rangeText(f)}。`}</div>}
                    {!f.help && rangeText(f) && <div className="f-hint">{rangeText(f)}。</div>}
                    {missing.map((r) => <div key={r.key} className="af-req"><Warning />{r.text}</div>)}
                    {error && <div className="f-err" role="alert">{error}</div>}
                    {f.generate && changed && draft[f.key] && !isMasked(draft[f.key]) && <NewSecret value={draft[f.key]} />}
                  </div>
                  <div className="af-ctl">
                    <Control f={f} value={draft[f.key]} onChange={(v) => setDraft((d) => ({ ...d, [f.key]: v }))} invalid={!!error} />
                  </div>
                </div>
              )
            })}
          </div>
        </section>
      ))}
      {dirty.length > 0 && (
        <div className={`savebar${errorCount ? ' err' : ''}`} role="region" aria-label={`未保存的${titles}设置`}>
          <span className="sb-count" aria-hidden="true">{String(dirty.length).padStart(2, '0')}</span>
          <div className="sb-text">
            <span>{titles}有 {dirty.length} 项修改未保存{errorCount ? <span className="sb-err"> · {errorCount} 项填得不对</span> : null}</span>
            <small className="ellipsis">{dirty.join('、')}</small>
          </div>
          <button type="button" className="btn on-slab" onClick={() => { setDraft(Object.fromEntries(Object.values(fields).map((f) => [f.key, f.value]))); setServerErrors({}) }} disabled={saving}>放弃修改</button>
          <button type="button" className="btn primary" onClick={save} disabled={saving || !!errorCount}>{saving ? '保存中…' : '保存'}</button>
        </div>
      )}
    </>
  )
}

/** A secret just generated here: shown once so it can be copied to the Mac before saving. */
function NewSecret({ value }) {
  const toast = useToast()
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); toast.ok('已复制。填进 Mac 的 ~/.claude-relay/token，不要发到聊天里') } catch { toast.err('复制失败，请手动选中复制') }
  }
  return (
    <div className="new-secret" style={{ marginTop: 8 }}>
      <Note kind="warn">
        新 token：<span className="mono" style={{ userSelect: 'all', wordBreak: 'break-all' }}>{value}</span>
        <button type="button" className="btn sm" style={{ marginLeft: 8 }} onClick={copy}><Copy />复制</button>
        <div className="f-hint" style={{ marginTop: 4 }}>保存后这里只显示首尾几位。现在复制，填进 Mac 的 ~/.claude-relay/token。</div>
      </Note>
    </div>
  )
}

function Control({ f, value, onChange, invalid }) {
  // f.value is what the server has; SecretInput uses it to leave edit mode after a save.
  if (f.type === 'onoff') return <Switch checked={value === 'on'} onChange={(on) => onChange(on ? 'on' : 'off')} label={f.label} />
  if (f.type === 'bool') return <Switch checked={value === 'true'} onChange={(on) => onChange(on ? 'true' : 'false')} label={f.label} />
  if (f.type === 'enum') return <Seg small value={value} onChange={onChange} label={f.label} options={f.options.map((o) => ({ value: o, label: f.labels?.[o] || o }))} />
  if (f.type === 'int' || f.type === 'float') {
    return <div style={{ width: 168 }}><NumberInput value={value} onChange={(v) => onChange(String(v))} invalid={invalid} suffix={f.unit} /></div>
  }
  if (f.type === 'secret') {
    return (
      <div className="stack" style={{ width: 300, gap: 6 }}>
        <SecretInput value={value} onChange={onChange} saved={f.value} optional={!f.generate} />
        {f.generate && <button type="button" className="btn sm" onClick={() => onChange(randomToken())}><Sparkle />随机生成</button>}
      </div>
    )
  }
  return <div style={{ width: 300 }}><TextInput value={value} onChange={onChange} mono placeholder={f.default || '（空）'} aria-invalid={invalid ? 'true' : undefined} /></div>
}

