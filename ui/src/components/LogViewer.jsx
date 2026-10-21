import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, Eraser, Pause, Play, MagnifyingGlass } from '@phosphor-icons/react'
import { get } from '../api'
import { PageHead, Seg } from './ui'

// The last 500 lines of data/bot.log (/api/logs), refreshed every 2 s.
// Lines without a timestamp (tracebacks) belong to the line above them.

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR']

function shape(entries) {
  let level = 'INFO'
  return entries.map((e, i) => {
    if (e.ts) { level = e.level; return { ...e, i, cont: false } }
    return { ...e, i, level, cont: true }
  })
}

export default function LogViewer() {
  const [lines, setLines] = useState(null)
  const [note, setNote] = useState('')
  const [err, setErr] = useState('')
  const [level, setLevel] = useState('ALL')
  const [q, setQ] = useState('')
  const [paused, setPaused] = useState(false)
  const [follow, setFollow] = useState(true)
  const [clearMark, setClearMark] = useState(null)
  const box = useRef(null)

  const load = useCallback(async () => {
    try {
      const d = await get('/api/logs')
      setLines(shape(d.logs || []))
      setNote(d.message || '')
      setErr('')
    } catch (e) { setErr(e.message) }
  }, [])

  useEffect(() => {
    load()
    if (paused) return undefined
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [load, paused])

  const visible = useMemo(() => {
    if (!lines) return []
    let list = lines
    if (clearMark) {
      let at = -1
      for (let i = list.length - 1; i >= 0; i--) if (list[i].raw === clearMark) { at = i; break }
      if (at >= 0) list = list.slice(at + 1)
    }
    const needle = q.trim().toLowerCase()
    return list.filter((l) => (level === 'ALL' || l.level === level) && (!needle || l.raw.toLowerCase().includes(needle)))
  }, [lines, level, q, clearMark])

  const counts = useMemo(() => {
    const c = { ALL: 0, DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0 }
    for (const l of lines || []) { if (!l.cont) { c.ALL++; c[l.level] = (c[l.level] || 0) + 1 } }
    return c
  }, [lines])

  useLayoutEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight
  }, [visible, follow])

  const onScroll = () => {
    const el = box.current
    if (!el) return
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
  }

  return (
    <div className="main-scroll logs-page">
      <div className="page wide logs-wrap">
        <PageHead
          no="02" en="LOGS" title="运行日志" sub="data/bot.log 的最后 500 行，每 2 秒刷新"
          actions={<>
            <button type="button" className="btn sm" onClick={() => setPaused((p) => !p)}>{paused ? <><Play />继续刷新</> : <><Pause />暂停刷新</>}</button>
            <button type="button" className="btn sm" onClick={() => setClearMark(lines?.length ? lines[lines.length - 1].raw : null)} title="只是不显示现在这些行，不会删除日志文件"><Eraser />清屏</button>
          </>}
        />
        <div className="filters">
          <Seg small label="级别" value={level} onChange={setLevel} options={[
            { value: 'ALL', label: '全部', count: counts.ALL },
            ...LEVELS.map((l) => ({ value: l, label: { DEBUG: '调试', INFO: '一般', WARNING: '警告', ERROR: '错误' }[l], count: counts[l] })),
          ]} />
          <div className="input-wrap grow" style={{ minWidth: 180 }}>
            <span className="addon" style={{ borderLeft: '1px solid var(--line-input)', borderRight: 0 }}><MagnifyingGlass /></span>
            <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索日志" />
          </div>
        </div>
        <div className="term slab">
          <div className="term-head" aria-hidden="true">
            <span className="latin">BOT.LOG</span>
            <span className="term-n num">{visible.length} 行</span>
            {paused && <span className="tag warn">已暂停</span>}
            {clearMark && <button type="button" className="link" style={{ color: 'var(--cy-hi)' }} onClick={() => setClearMark(null)}>显示清屏前的行</button>}
          </div>
          <div className="term-body scroll" ref={box} onScroll={onScroll} role="log" aria-label="运行日志">
            {err && <div className="tl err">读取失败：{err}</div>}
            {lines && !lines.length && <div className="tl dim">{note || '还没有日志。启动机器人后会出现在这里。'}</div>}
            {lines && lines.length > 0 && !visible.length && <div className="tl dim">没有符合条件的行</div>}
            {visible.map((l) => (
              <div key={l.i} className={`tl lv-${l.level}${l.cont ? ' cont' : ''}`}>
                {!l.cont && <span className="tl-ts">{l.ts.slice(11)}</span>}
                {!l.cont && <span className="tl-lv">{l.level}</span>}
                {!l.cont && l.module && <span className="tl-mod">{l.module}</span>}
                <span className="tl-msg">{l.cont ? l.raw : l.msg}</span>
              </div>
            ))}
          </div>
          {!follow && (
            <button type="button" className="term-jump" onClick={() => setFollow(true)}><ArrowDown />回到最新</button>
          )}
        </div>
      </div>
    </div>
  )
}
