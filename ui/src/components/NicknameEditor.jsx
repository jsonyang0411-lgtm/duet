import { useCallback, useEffect, useMemo, useState } from 'react'
import { UsersThree, MagnifyingGlass, CheckCircle, WarningCircle } from '@phosphor-icons/react'
import { api, get } from '../api'
import { loadGroups } from '../lib/groups'
import { PageHead, Empty, Note } from './ui'

// Custom nicknames: what the bot calls a member. Saved per person (wxid), so a
// nickname applies in every group; leaving the box empty removes it.

export default function NicknameEditor() {
  const [groups, setGroups] = useState(null)
  const [gErr, setGErr] = useState('')
  const [chat, setChat] = useState('')
  const [members, setMembers] = useState(null)
  const [mErr, setMErr] = useState('')
  const [q, setQ] = useState('')

  useEffect(() => {
    loadGroups(true).then((list) => {
      setGroups(list)
      if (list.length) setChat((c) => c || list[0].chat_id)
    }, (e) => { setGroups([]); setGErr(e.message) })
  }, [])

  const loadMembers = useCallback(async (id) => {
    setMembers(null); setMErr('')
    try { const d = await get('/api/nicknames', { chat_id: id }); setMembers(d.members || []) } catch (e) { setMErr(e.message); setMembers([]) }
  }, [])
  useEffect(() => { if (chat) loadMembers(chat) }, [chat, loadMembers])

  const shown = useMemo(() => {
    const n = q.trim().toLowerCase()
    return (members || []).filter((m) => !n || [m.display_name, m.nickname, m.wxid].some((x) => String(x || '').toLowerCase().includes(n)))
  }, [members, q])

  const current = (groups || []).find((g) => g.chat_id === chat)

  return (
    <div className="split members">
      <div className="main-scroll scroll">
        <div className="page wide">
          <PageHead no="02" en="MEMBERS" title="群友昵称" sub="给群友起个机器人用的称呼。按人保存，在所有群里都生效；清空就是不用自定义昵称" />
          {mErr && <Note kind="need">读取群成员失败：{mErr}</Note>}
          {!chat && groups && !groups.length && (
            <Empty icon={UsersThree} title="还没有群聊记录">{gErr || '机器人运行一段时间、收到群消息以后，群和成员会出现在这里。'}</Empty>
          )}
          {chat && (
            <>
              <div className="row" style={{ marginBottom: 10 }}>
                <h2 style={{ fontSize: 'var(--fs-lg)' }} className="ellipsis">{current?.group_name || chat}</h2>
                <span className="tag">{members ? members.length : '…'} 人</span>
                <span className="spacer" />
                <div className="input-wrap" style={{ width: 240 }}>
                  <span className="addon" style={{ borderLeft: '1px solid var(--line-input)', borderRight: 0 }}><MagnifyingGlass /></span>
                  <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="找人" style={{ height: 32 }} />
                </div>
              </div>
              {!members && !mErr && <div className="row"><span className="spin" />读取成员…</div>}
              {members && !members.length && !mErr && <Empty title="这个群还没有消息记录" />}
              {shown.length > 0 && (
                <table className="table nick-table">
                  <thead><tr><th>群里显示的名字</th><th>微信 ID</th><th style={{ width: 260 }}>机器人怎么称呼他</th></tr></thead>
                  <tbody>{shown.map((m) => <MemberRow key={m.wxid} m={m} />)}</tbody>
                </table>
              )}
            </>
          )}
        </div>
      </div>
      <aside className="aside scroll" aria-label="群聊列表">
        <div className="aside-inner" style={{ padding: '18px 10px' }}>
          <div className="label" style={{ padding: '0 10px 8px' }}>群聊 {groups ? groups.length : ''}</div>
          {!groups && <div className="row" style={{ padding: 10 }}><span className="spin" />读取群聊…</div>}
          <div className="list">
            {(groups || []).map((g) => (
              <button key={g.chat_id} type="button" className="list-row" aria-current={g.chat_id === chat ? 'true' : undefined} onClick={() => setChat(g.chat_id)}>
                <span className="grow ellipsis">{g.group_name || g.chat_id}</span>
                <span className="num dim" style={{ fontSize: 'var(--fs-xs)' }}>{g.member_count}</span>
              </button>
            ))}
          </div>
        </div>
      </aside>
    </div>
  )
}

function MemberRow({ m }) {
  const [value, setValue] = useState(m.nickname || '')
  const [saved, setSaved] = useState(m.nickname || '')
  const [state, setState] = useState('') // '' | saving | ok | err
  const [err, setErr] = useState('')
  const save = async () => {
    const v = value.trim()
    if (v === saved) return
    setState('saving')
    try {
      const d = await api('/api/nicknames', { method: 'POST', body: { wxid: m.wxid, nickname: v } })
      if (d.ok === false) throw new Error(d.error || '保存失败')
      setSaved(v); setValue(v); setState('ok')
      setTimeout(() => setState((s) => (s === 'ok' ? '' : s)), 2000)
    } catch (e) { setErr(e.message); setState('err') }
  }
  return (
    <tr>
      <td>{m.display_name || <span className="dim">（没有名字）</span>}</td>
      <td className="mono dim" style={{ fontSize: 'var(--fs-xs)' }}>{m.wxid}</td>
      <td>
        <div className="row" style={{ gap: 6 }}>
          <input
            className="input" style={{ height: 30, fontSize: 'var(--fs-sm)' }}
            value={value} placeholder="不设置"
            aria-invalid={state === 'err' ? 'true' : undefined}
            onChange={(e) => { setValue(e.target.value); if (state === 'err') setState('') }}
            onBlur={save}
            onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
          />
          {state === 'saving' && <span className="spin" />}
          {state === 'ok' && <CheckCircle weight="fill" style={{ color: 'var(--ok-ink)', flex: 'none' }} aria-label="已保存" />}
          {state === 'err' && <WarningCircle weight="fill" style={{ color: 'var(--need-ink)', flex: 'none' }} title={err} aria-label={`保存失败：${err}`} />}
        </div>
      </td>
    </tr>
  )
}
