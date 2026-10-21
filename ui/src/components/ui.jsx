import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Info, Plus, X, WarningCircle, CheckCircle, Warning } from '@phosphor-icons/react'
import { isMasked } from '../lib/schema'

/* 节标题「02 / STATUS」：装饰，读屏不读 */
export function SecDeco({ no, en, small }) {
  return (
    <span className={`sec-deco${small ? ' s' : ''}`} aria-hidden="true">
      <i className="sec-no">{no}</i><i className="sec-sl">/</i><i className="sec-en">{en}</i>
    </span>
  )
}

export function PageHead({ no, en, title, sub, actions }) {
  return (
    <header className="page-head">
      <SecDeco no={no} en={en} />
      <div className="ph-titles">
        <h1>{title}</h1>
        {sub && <div className="ph-sub">{sub}</div>}
      </div>
      {actions && <div className="ph-actions">{actions}</div>}
    </header>
  )
}

export function Card({ title, sub, eyebrow, actions, children, className = '', size = '' }) {
  return (
    <section className={`ch ${size} card ${className}`}>
      {(title || actions) && (
        <div className="card-head">
          <div className="stack" style={{ gap: 2 }}>
            {eyebrow && <span className="eyebrow" aria-hidden="true">{eyebrow}</span>}
            {title && <h2>{title}</h2>}
            {sub && <div className="sub">{sub}</div>}
          </div>
          {actions && <div className="row" style={{ marginLeft: 'auto' }}>{actions}</div>}
        </div>
      )}
      {children}
    </section>
  )
}

export function Field({ label, envKey, hint, error, warn, children, id, className = '' }) {
  return (
    <div className={`field ${className}`}>
      {(label || envKey) && (
        <div className="f-head">
          {label && (id ? <label htmlFor={id}>{label}</label> : <span className="f-name">{label}</span>)}
          {envKey && <span className="f-key">{envKey}</span>}
        </div>
      )}
      {children}
      {error ? <div className="f-err" role="alert">{error}</div> : warn ? <div className="f-warn">{warn}</div> : null}
      {hint && <div className="f-hint">{hint}</div>}
    </div>
  )
}

export function TextInput({ value, onChange, mono, ...rest }) {
  return (
    <input
      className={`input${mono ? ' mono' : ''}`}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value)}
      spellCheck={false}
      {...rest}
    />
  )
}

export function TextArea({ value, onChange, rows = 5, ...rest }) {
  return <textarea className="textarea" rows={rows} value={value ?? ''} onChange={(e) => onChange(e.target.value)} {...rest} />
}

/** Number input that keeps what you type (no clamping per keystroke). */
export function NumberInput({ value, onChange, invalid, step = 'any', suffix, ...rest }) {
  const [text, setText] = useState(value === undefined || value === null ? '' : String(value))
  useEffect(() => {
    const cur = text.trim() === '' ? NaN : Number(text)
    if (!(cur === value || String(value) === text)) setText(value === undefined || value === null ? '' : String(value))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  const input = (
    <input
      className="input mono"
      inputMode="decimal"
      value={text}
      aria-invalid={invalid ? 'true' : undefined}
      step={step}
      onChange={(e) => {
        const t = e.target.value
        setText(t)
        const n = Number(t)
        onChange(t.trim() === '' || Number.isNaN(n) ? t : n)
      }}
      {...rest}
    />
  )
  if (!suffix) return input
  return <div className="input-wrap">{input}<span className="addon">{suffix}</span></div>
}

export function Select({ value, onChange, options, ...rest }) {
  const known = options.some((o) => String(o.value) === String(value))
  return (
    <select className="select" value={value ?? ''} onChange={(e) => onChange(e.target.value)} {...rest}>
      {!known && value !== undefined && value !== '' && <option value={value}>{String(value)}（当前值）</option>}
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  )
}

export function Switch({ checked, onChange, label, disabled, id }) {
  return (
    <button
      type="button" role="switch" id={id}
      className="switch"
      aria-checked={checked ? 'true' : 'false'}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  )
}

export function Seg({ options, value, onChange, small, label }) {
  return (
    <div className={`seg${small ? ' sm' : ''}`} role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value} type="button"
          aria-pressed={String(o.value) === String(value) ? 'true' : 'false'}
          onClick={() => onChange(o.value)}
          title={o.title}
        >
          {o.label}{o.count !== undefined && <span className="n">{o.count}</span>}
        </button>
      ))}
    </div>
  )
}

/** Chip editor; Enter adds (IME-safe), duplicates are ignored. */
export function ChipsInput({ value, onChange, placeholder = '输入后按回车添加', minItems = 0, allLabel, addLabel = '添加', split = true }) {
  const list = Array.isArray(value) ? value : []
  const [text, setText] = useState('')
  const composing = useRef(false)
  const add = () => {
    // Keywords may be pasted as a list; names (groups, contacts) are taken whole.
    const parts = (split ? text.split(/[,，]/) : [text]).map((s) => s.trim()).filter(Boolean)
    if (!parts.length) return
    const next = [...list]
    for (const p of parts) if (!next.includes(p)) next.push(p)
    onChange(next)
    setText('')
  }
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="chips">
        {!list.length && allLabel && <span className="chip all">{allLabel}</span>}
        {list.map((item) => (
          <span className="chip" key={item}>
            <span title={item}>{item}</span>
            <button
              type="button" aria-label={`删除 ${item}`}
              disabled={list.length <= minItems}
              title={list.length <= minItems ? `至少保留 ${minItems} 个` : '删除'}
              onClick={() => onChange(list.filter((x) => x !== item))}
            ><X weight="bold" /></button>
          </span>
        ))}
      </div>
      <div className="chip-add">
        <input
          className="input" value={text} placeholder={placeholder}
          onChange={(e) => setText(e.target.value)}
          onCompositionStart={() => { composing.current = true }}
          onCompositionEnd={() => { composing.current = false }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !composing.current && !e.nativeEvent.isComposing) { e.preventDefault(); add() }
          }}
        />
        <button type="button" className="btn" onClick={add} disabled={!text.trim()}><Plus weight="bold" />{addLabel}</button>
      </div>
    </div>
  )
}

/**
 * Secret (API key): the server sends back a masked value ("sk-a***b123").
 * Show "已设置 · 尾号" instead of the mask; posting the mask back keeps the
 * stored key, typing a new value replaces it, "清除" posts an empty value.
 */
export function SecretInput({ value, onChange, saved, placeholder = '粘贴新的密钥', optional }) {
  const [editing, setEditing] = useState(false)
  const first = useRef(value)
  const stored = saved === undefined ? first.current : saved
  const masked = isMasked(value)
  const id = useId()
  // After a save/reload/discard the value is the stored one again: leave edit mode.
  useEffect(() => { if (masked || value === stored) setEditing(false) }, [value, stored, masked])
  if (!editing && (masked || !value)) {
    return (
      <div className="secret">
        {masked
          ? <span className="s-state"><CheckCircle weight="fill" />已设置</span>
          : <span className={`s-state ${optional ? '' : 'none'}`} style={optional ? { color: 'var(--ink-3)' } : undefined}>{optional ? '没有设置（可不填）' : '没有设置'}</span>}
        {masked && <span className="s-tail">{value}</span>}
        {!masked && isMasked(stored) && <span className="tag warn">保存后会删除</span>}
        <span className="spacer" />
        <button type="button" className="btn sm" onClick={() => setEditing(true)}>{masked ? '更换' : '填写'}</button>
        {masked && <button type="button" className="btn sm ghost" onClick={() => onChange('')} title="保存后会删除这个密钥">清除</button>}
        {!masked && isMasked(stored) && <button type="button" className="btn sm ghost" onClick={() => onChange(stored)}>撤销</button>}
      </div>
    )
  }
  return (
    <div className="input-wrap">
      <input
        id={id} className="input mono" type="password" autoComplete="off" autoFocus={editing}
        value={masked ? '' : (value ?? '')} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      {editing && (
        <button type="button" className="btn" onClick={() => { onChange(stored); setEditing(false) }}>取消</button>
      )}
    </div>
  )
}

export function Note({ kind = '', icon, children }) {
  const Ico = icon || (kind === 'warn' ? Warning : kind === 'need' ? WarningCircle : Info)
  return <div className={`note ${kind}`}><Ico weight="bold" /><div>{children}</div></div>
}

export function Banner({ kind = '', title, children, actions }) {
  const Ico = kind === 'ok' ? CheckCircle : kind === 'cy' ? Info : WarningCircle
  return (
    <div className={`banner ${kind}`} role={kind ? 'status' : 'alert'}>
      <Ico weight="bold" />
      <div className="b-body">
        {title && <div className="b-title">{title}</div>}
        {children && <div className="b-text">{children}</div>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </div>
  )
}

export function Empty({ icon: Ico = Info, title, children }) {
  return (
    <div className="empty">
      <Ico />
      {title && <div className="e-title">{title}</div>}
      {children && <div>{children}</div>}
    </div>
  )
}

export function Dialog({ title, eyebrow, onClose, children, footer, wide }) {
  const ref = useRef(null)
  useEffect(() => {
    const prev = document.activeElement
    const first = ref.current?.querySelector('[data-autofocus], button, input, select, textarea')
    first?.focus()
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); prev?.focus?.() }
  }, [onClose])
  return createPortal(
    <div className="scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose?.() }}>
      <div ref={ref} className={`ch dialog${wide ? ' wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="dlg-head">
          <div className="grow">
            {eyebrow && <span className="eyebrow" aria-hidden="true">{eyebrow}</span>}
            <h2>{title}</h2>
          </div>
          {onClose && <button type="button" className="icon-btn" aria-label="关闭" onClick={onClose}><X /></button>}
        </div>
        <div className="dlg-body">{children}</div>
        {footer && <div className="dlg-foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}

/** Confirm dialog; the safe choice gets the focus. */
export function Confirm({ title, children, confirmLabel = '确定', cancelLabel = '取消', danger, onConfirm, onCancel, busy }) {
  return (
    <Dialog
      title={title} eyebrow="CONFIRM" onClose={onCancel}
      footer={<>
        <button type="button" className="btn" data-autofocus onClick={onCancel}>{cancelLabel}</button>
        <button type="button" className={`btn ${danger ? 'danger' : 'primary chc'}`} disabled={busy} onClick={onConfirm}>{busy ? '处理中…' : confirmLabel}</button>
      </>}
    >
      {children}
    </Dialog>
  )
}

/** Decorative barcode; the seed makes it stable. */
export function Barcode({ seed = 'webot', bars = 22, unit = 1.5 }) {
  let h = 0x811c9dc5
  for (const ch of String(seed)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 0x01000193) >>> 0 }
  const out = []
  for (let i = 0; i < bars; i++) {
    h = Math.imul(h ^ (h >>> 13), 0x5bd1e995) >>> 0
    out.push(<i key={i} style={{ width: (1 + (h % 3)) * unit, marginRight: (1 + ((h >>> 3) % 2)) * unit }} />)
  }
  return <span className="barcode" aria-hidden="true">{out}</span>
}

export function ToggleRow({ title, desc, envKey, checked, onChange, children }) {
  const id = useId()
  return (
    <div className="toggle-row">
      <div className="tr-text">
        <label htmlFor={id} className="tr-title">{title}{envKey && <span className="f-key">{envKey}</span>}</label>
        {desc && <div className="f-hint">{desc}</div>}
      </div>
      <Switch id={id} checked={!!checked} onChange={onChange} label={title} />
      {children}
    </div>
  )
}
