import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowClockwise, ListMagnifyingGlass, MagnifyingGlass } from '@phosphor-icons/react'
import { get } from '../api'
import { navigate } from '../lib/router'
import { useGroups } from '../lib/groups'
import { PageHead, Seg, Select, Empty, Note, SecDeco } from './ui'

// "为什么回 / 没回": GET /api/decisions reads data/decision_trace.jsonl
// (src/decision_explain.py turns each record into plain sentences).

const ROUTES = {
  at: '@ 机器人', sticky: '上一条 @ 之后的接话', keyword: '唤醒词', followup: '跟聊',
  name_call: '喊了机器人的名字', ambient: '没人叫它（旁听）',
}

const OUTCOMES = {
  reply: ['回复了', 'ok'],
  silent: ['没回复', 'off'],
  ignored: ['忽略', 'off'],
}

export function OutcomeTag({ outcome }) {
  const [text, kind] = OUTCOMES[outcome] || [outcome || '未知', 'pend']
  return <span className={`tag ${kind}`}>{text}</span>
}

export function fmtTime(ts, withDate) {
  if (!ts) return '--:--'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  const t = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  if (!withDate) {
    const today = new Date()
    if (d.toDateString() === today.toDateString()) return t
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${t.slice(0, 5)}`
  }
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${t}`
}

export default function DecisionsPanel({ route }) {
  const selectedId = route.sub ? decodeURIComponent(route.sub) : ''
  const [outcome, setOutcome] = useState('')
  const [chat, setChat] = useState('')
  const [limit, setLimit] = useState('100')
  const [search, setSearch] = useState('')
  const [items, setItems] = useState(null)
  const [extra, setExtra] = useState(null) // a record linked from elsewhere that is not in the list
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const groups = useGroups()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const d = await get('/api/decisions', { limit, outcome, chat_id: chat })
      setItems(d.decisions || [])
      setErr('')
    } catch (e) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }, [limit, outcome, chat])

  useEffect(() => { load() }, [load])

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!items) return null
    if (!q) return items
    return items.filter((d) => [d.content, d.sender, d.chat, d.reason_text].some((x) => String(x || '').toLowerCase().includes(q)))
  }, [items, search])

  const selected = useMemo(() => {
    if (!selectedId) return null
    return (items || []).find((d) => d.message_id === selectedId) || (extra?.message_id === selectedId ? extra : null)
  }, [items, extra, selectedId])

  useEffect(() => {
    if (!selectedId || !items || items.some((d) => d.message_id === selectedId)) return
    get('/api/decisions', { message_id: selectedId, limit: 1 })
      .then((d) => setExtra((d.decisions || [])[0] || null))
      .catch(() => setExtra(null))
  }, [selectedId, items])

  const counts = useMemo(() => {
    const c = { reply: 0, other: 0 }
    for (const d of items || []) (d.outcome === 'reply' ? c.reply++ : c.other++)
    return c
  }, [items])

  return (
    <div className="split">
      <div className="main-scroll scroll">
        <div className="page wide">
          <PageHead
            no="02" en="TRACE" title="回复记录"
            sub="每条消息机器人回了没有、为什么。最新的在最上面"
            actions={<button type="button" className="btn sm" onClick={load} disabled={loading}><ArrowClockwise />{loading ? '刷新中…' : '刷新'}</button>}
          />
          <div className="filters">
            <Seg
              small label="结果" value={outcome} onChange={setOutcome}
              options={[{ value: '', label: '全部' }, { value: 'reply', label: '回复了' }, { value: 'silent', label: '没回复' }, { value: 'ignored', label: '忽略' }]}
            />
            <div style={{ width: 200 }}>
              <Select
                value={chat} onChange={setChat} aria-label="群聊"
                options={[{ value: '', label: '全部群聊' }, ...(groups.list || []).map((g) => ({ value: g.chat_id, label: g.group_name || g.chat_id }))]}
              />
            </div>
            <div style={{ width: 120 }}>
              <Select value={limit} onChange={setLimit} aria-label="条数" options={[{ value: '50', label: '最近 50 条' }, { value: '100', label: '最近 100 条' }, { value: '300', label: '最近 300 条' }]} />
            </div>
            <div className="input-wrap grow" style={{ minWidth: 160 }}>
              <span className="addon" style={{ borderLeft: '1px solid var(--line-input)', borderRight: 0 }}><MagnifyingGlass /></span>
              <input className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="在这些记录里找内容、人名、原因" />
            </div>
          </div>
          {items && <div className="f-hint" style={{ margin: '8px 0 4px' }}>共 {items.length} 条：回复了 {counts.reply}，其他 {counts.other}{search && `；符合搜索的 ${shown.length} 条`}</div>}
          {err && <Note kind="need">读取失败：{err}</Note>}
          {shown && !shown.length && (
            <Empty icon={ListMagnifyingGlass} title={items.length ? '没有符合条件的记录' : '还没有记录'}>
              {items.length ? '换个筛选条件试试。' : '机器人运行后，每条消息的处理过程都会记在这里。如果高级开关里关掉了「决策记录」（DECISION_TRACE_ENABLED），这里就不会有数据。'}
            </Empty>
          )}
          {shown && shown.length > 0 && (
            <div className="trace-list" role="listbox" aria-label="回复记录">
              {shown.map((d, i) => (
                <button
                  type="button" key={`${d.message_id}-${i}`} role="option"
                  className={`trace-row o-${d.outcome}`}
                  aria-selected={d.message_id === selectedId ? 'true' : 'false'}
                  onClick={() => navigate(`trace/${encodeURIComponent(d.message_id || '')}`)}
                >
                  <span className="tr-time num">{fmtTime(d.ts)}</span>
                  <span className="tr-main">
                    <span className="tr-who"><b>{d.sender || '—'}</b><span className="dim"> · {d.chat || '—'}</span></span>
                    <span className="tr-content ellipsis">{d.content || <span className="dim">（没有记录内容）</span>}</span>
                    <span className="tr-why ellipsis">{d.reason_text || d.reason}</span>
                  </span>
                  <OutcomeTag outcome={d.outcome} />
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      <aside className="aside scroll" aria-label="判断详情">
        <div className="aside-inner">
          <div className="row" style={{ marginBottom: 14 }}>
            <SecDeco no="03" en="DETAIL" small />
            <span className="label">判断过程</span>
            <span className="sec-rule" aria-hidden="true" />
          </div>
          {!selected && <Empty title="选一条记录">左边点一条消息，这里会写出它是怎么被注意到的、走到了哪一步、为什么回或不回。</Empty>}
          {selected && <DecisionDetail d={selected} />}
        </div>
      </aside>
    </div>
  )
}

function DecisionDetail({ d }) {
  return (
    <div className="stack">
      <div className="row wrap" style={{ gap: 6 }}>
        <OutcomeTag outcome={d.outcome} />
        <span className="tag num">{fmtTime(d.ts, true)}</span>
      </div>
      <div>
        <div className="label">{d.sender || '—'} · {d.chat || '—'}</div>
        <blockquote className="quote">{d.content || '（没有记录内容）'}</blockquote>
      </div>
      <dl className="kv">
        <dt>原因</dt><dd>{d.reason_text || d.reason || '未记录'}</dd>
        {d.route && <><dt>怎么注意到</dt><dd>{ROUTES[d.route] || d.route}</dd></>}
        {d.ended_by && <><dt>结束于</dt><dd className="mono">{d.ended_by}</dd></>}
      </dl>
      {d.summary?.length > 0 && (
        <div>
          <div className="label" style={{ marginBottom: 6 }}>说明</div>
          <ol className="summary">{d.summary.map((s, i) => <li key={i}>{s}</li>)}</ol>
        </div>
      )}
      {d.trail?.length > 0 && (
        <div>
          <div className="label" style={{ marginBottom: 6 }}>经过的步骤</div>
          <ol className="trail">{d.trail.map((s, i) => <li key={i}><span className="num">{String(i + 1).padStart(2, '0')}</span><code>{s}</code></li>)}</ol>
        </div>
      )}
      <div className="f-hint mono" style={{ wordBreak: 'break-all' }}>message_id: {d.message_id || '—'}</div>
    </div>
  )
}
