import { useCallback, useEffect, useMemo, useState } from 'react'
import { CheckCircle, Trash, ArrowUUpLeft, MagnifyingGlass, ArrowClockwise, CheckSquare, GearSix } from '@phosphor-icons/react'
import { api, get } from '../api'
import { useToast } from '../store/toast'
import { useGroups } from '../lib/groups'
import { navigate } from '../lib/router'
import { PageHead, Seg, Select, Empty, Note, Confirm } from './ui'

// Group to-dos recorded from chat ("记一下…"). Switching the feature on/off and
// its trigger words live in 功能模块 → 群聊待办; this page only manages items.

const TABS = [
  { value: 'active', label: '进行中' },
  { value: 'completed', label: '已完成' },
  { value: 'deleted', label: '已删除' },
]
const STATUS_TAG = { active: ['进行中', 'cy'], completed: ['已完成', 'ok'], deleted: ['已删除', 'off'] }

const when = (ts) => (ts > 0 ? new Date(ts * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '')

export default function TodoManager() {
  const toast = useToast()
  const groups = useGroups()
  const [tab, setTab] = useState('active')
  const [chat, setChat] = useState('')
  const [search, setSearch] = useState('')
  const [items, setItems] = useState(null)
  const [counts, setCounts] = useState({})
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [confirm, setConfirm] = useState(null) // {action, label}

  const searching = search.trim().length > 0
  const nameOf = useCallback((id) => (groups.list || []).find((g) => g.chat_id === id)?.group_name || id, [groups.list])

  const load = useCallback(async () => {
    try {
      const [d, c] = await Promise.all([
        get('/api/todos', { status: searching ? 'all' : tab, chat_id: chat, search: search.trim() }),
        get('/api/todos/counts', { chat_id: chat }),
      ])
      setItems(d.items || [])
      setCounts(c.counts || {})
      setErr('')
    } catch (e) { setErr(e.message) }
  }, [tab, chat, search, searching])

  useEffect(() => {
    const t = setTimeout(load, searching ? 300 : 0)
    return () => clearTimeout(t)
  }, [load, searching])

  const act = async (action, chatId, target = '') => {
    setBusy(`${action}:${chatId}:${target}`)
    try {
      const d = await api('/api/todos/action', { method: 'POST', body: { action, chat_id: chatId, target: String(target) } })
      if (d.ok) toast.ok(d.reply || '已完成操作')
      else toast.err(d.reply || d.error || '操作没有成功')
    } catch (e) { toast.err(e.message) } finally {
      setBusy('')
      load()
    }
  }

  const tabs = TABS.map((t) => ({ ...t, count: counts[t.value] ?? 0 }))
  const shown = useMemo(() => items || [], [items])

  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead
          no="02" en="TODO" title="群聊待办" sub="群友在群里记下的待办。开关和触发说法在「功能模块 → 群聊待办」"
          actions={<>
            <button type="button" className="btn sm" onClick={() => navigate('modules/todo')}><GearSix />设置</button>
            <button type="button" className="btn sm" onClick={load}><ArrowClockwise />刷新</button>
          </>}
        />
        <div className="filters">
          <Seg small label="状态" value={searching ? '' : tab} onChange={(v) => { setSearch(''); setTab(v) }} options={tabs} />
          <div style={{ width: 220 }}>
            <Select value={chat} onChange={setChat} aria-label="群聊" options={[{ value: '', label: '全部群聊' }, ...(groups.list || []).map((g) => ({ value: g.chat_id, label: g.group_name || g.chat_id }))]} />
          </div>
          <div className="input-wrap grow" style={{ minWidth: 180 }}>
            <span className="addon" style={{ borderLeft: '1px solid var(--line-input)', borderRight: 0 }}><MagnifyingGlass /></span>
            <input className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索待办内容（会搜所有状态）" />
          </div>
        </div>
        <div className="row" style={{ margin: '10px 0' }}>
          <span className="f-hint grow">{searching ? `搜索结果 ${shown.length} 条（所有状态）` : `${tabs.find((t) => t.value === tab)?.label} ${shown.length} 条`}</span>
          {!searching && tab === 'completed' && (
            <button type="button" className="btn sm danger" disabled={!chat || !shown.length} title={chat ? '' : '先选一个群'} onClick={() => setConfirm({ action: 'clear_completed', label: '清空已完成' })}>
              <Trash />清空本群已完成
            </button>
          )}
          {!searching && tab === 'deleted' && (
            <button type="button" className="btn sm danger" disabled={!chat || !shown.length} title={chat ? '' : '先选一个群'} onClick={() => setConfirm({ action: 'clear_deleted', label: '彻底删除' })}>
              <Trash />彻底删除本群已删除
            </button>
          )}
        </div>
        {!searching && (tab === 'completed' || tab === 'deleted') && !chat && shown.length > 0 && (
          <Note>批量清空是按群进行的：先在上面选一个群。</Note>
        )}
        {err && <Note kind="need">读取失败：{err}</Note>}
        {items && !shown.length && !err && (
          <Empty icon={CheckSquare} title={searching ? '没有找到' : '这里还没有待办'}>
            {searching ? '换个关键词试试。' : '群友在群里说「记一下……」就会记成一条待办。'}
          </Empty>
        )}
        {shown.length > 0 && (
          <div className="todo-list">
            {shown.map((it) => {
              const [st, kind] = STATUS_TAG[it.status] || [it.status, 'off']
              const key = `${it.chat_id}:${it.id}`
              const b = (a) => busy === `${a}:${it.chat_id}:${it.display_order}`
              return (
                <div key={key} className={`todo-row s-${it.status}`}>
                  <span className="td-no num">#{it.display_order}</span>
                  <div className="td-main">
                    <div className="row wrap" style={{ gap: 6 }}>
                      {searching && <span className={`tag ${kind}`}>{st}</span>}
                      {!chat && <span className="tag">{nameOf(it.chat_id)}</span>}
                    </div>
                    <p className="td-text">{it.content}</p>
                    <div className="td-meta">
                      {it.creator_name && <span>{it.creator_name} 记于 {when(it.created_at)}</span>}
                      {it.completed_by_name && <span>{it.completed_by_name} 完成于 {when(it.completed_at)}</span>}
                      {it.deleted_by_name && <span>{it.deleted_by_name} 删除于 {when(it.deleted_at)}</span>}
                    </div>
                  </div>
                  <div className="td-act">
                    {it.status === 'active' && (
                      <>
                        <button type="button" className="btn sm" disabled={!!busy} onClick={() => act('complete', it.chat_id, it.display_order)}><CheckCircle />{b('complete') ? '…' : '完成'}</button>
                        <button type="button" className="btn sm ghost" disabled={!!busy} onClick={() => act('delete', it.chat_id, it.display_order)}><Trash />删除</button>
                      </>
                    )}
                    {it.status === 'deleted' && (
                      <button type="button" className="btn sm" disabled={!!busy} onClick={() => act('restore', it.chat_id, it.display_order)}><ArrowUUpLeft />恢复</button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
        {confirm && (
          <Confirm
            title={`${confirm.label}「${nameOf(chat)}」？`} danger confirmLabel={confirm.label}
            onCancel={() => setConfirm(null)}
            onConfirm={async () => { const a = confirm.action; setConfirm(null); await act(a, chat) }}
          >
            {confirm.action === 'clear_completed'
              ? '这个群所有已完成的待办都会被清掉，不能恢复。'
              : '这个群所有已删除的待办都会被彻底删掉，不能再恢复。'}
          </Confirm>
        )}
      </div>
    </div>
  )
}
