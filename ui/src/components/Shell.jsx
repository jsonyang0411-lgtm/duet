import { useState } from 'react'
import {
  Pulse, ListMagnifyingGlass, GearSix, SquaresFour, SlidersHorizontal, UsersThree,
  CheckSquare, TerminalWindow, Moon, Sun, Play, Stop, ArrowClockwise, Handshake,
} from '@phosphor-icons/react'
import { useStatus, fmtDuration } from '../store/status'
import { useConfig } from '../store/config'
import { useToast } from '../store/toast'
import { pageOfKey } from '../lib/schema'
import { useTheme } from '../lib/theme'
import { navigate } from '../lib/router'
import { LogoGlyph, FigurePlaceholder } from './art'
import { useInfluencerAttention } from './InfluencerPanel'

export const NAV = [
  { id: 'status', label: '运行状态', en: 'STATUS', icon: Pulse },
  { id: 'trace', label: '回复记录', en: 'TRACE', icon: ListMagnifyingGlass },
  {
    id: 'config', label: '系统配置', en: 'CONFIG', icon: GearSix,
    sub: [
      { id: 'ai', label: 'AI 服务' },
      { id: 'identity', label: '身份与群聊' },
      { id: 'persona', label: '人设' },
      { id: 'data', label: '数据与日志' },
      { id: 'sandbox', label: '提示词沙箱' },
    ],
  },
  { id: 'modules', label: '功能模块', en: 'MODULES', icon: SquaresFour },
  { id: 'advanced', label: '高级开关', en: 'ADVANCED', icon: SlidersHorizontal },
  { id: 'members', label: '群友昵称', en: 'MEMBERS', icon: UsersThree },
  { id: 'todo', label: '群聊待办', en: 'TODO', icon: CheckSquare },
  {
    id: 'influencer', label: '达人审批', en: 'CREATOR', icon: Handshake,
    sub: [
      { id: 'approvals', label: '审批单' },
      { id: 'inbox', label: '待处理' },
      { id: 'contacts', label: '达人名单' },
    ],
  },
  { id: 'logs', label: '运行日志', en: 'LOGS', icon: TerminalWindow },
]

export function TopBar({ route }) {
  const { theme, toggle } = useTheme()
  const nav = NAV.find((n) => n.id === route.page)
  return (
    <header className="topbar">
      <a className="brand" href="#/status" aria-label="webot 控制台：回到运行状态">
        <span className="brand-mark"><LogoGlyph /></span>
        <span className="brand-name"><b>WEBOT</b><small>微信机器人控制台</small></span>
      </a>
      {nav && (
        <div className="top-title" aria-hidden="true">
          <span className="latin">{nav.en}</span>
        </div>
      )}
      <div className="top-actions">
        <RunPill />
        <button
          type="button" className="icon-btn"
          onClick={toggle}
          title={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'}
          aria-label={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'}
        >
          {theme === 'dark' ? <Sun /> : <Moon />}
        </button>
      </div>
    </header>
  )
}

function RunPill() {
  const { conn, data, running, busy, start, stop } = useStatus()
  let dot = 'dot', title, sub = ''
  if (conn === 'connecting') { title = '连接中…' }
  else if (conn === 'offline') { title = '控制台服务离线'; dot = 'dot need' }
  else if (running) {
    title = '运行中'; dot = 'dot cy live'
    sub = `${fmtDuration(data?.uptime_sec)} · 已处理 ${data?.messages_processed ?? 0} 条`
  } else if (data?.error) { title = '已停止 · 出错'; dot = 'dot need' }
  else { title = '已停止' }
  const err = conn === 'offline' || (!running && data?.error)
  return (
    <div className={`run-pill${err ? ' err' : ''}`} role="group" aria-label="机器人运行状态">
      <span className="rp-ico"><span className={dot} /></span>
      <a className="rp-text" href="#/status" style={{ color: 'inherit', textDecoration: 'none' }}>
        <b>{title}</b>{sub && <span className="rp-sub num">{sub}</span>}
      </a>
      {conn === 'online' && (running
        ? <button type="button" className="rp-btn" onClick={stop} disabled={!!busy}><Stop weight="fill" />{busy === 'stop' ? '停止中…' : '停止'}</button>
        : <button type="button" className="rp-btn go" onClick={start} disabled={!!busy}><Play weight="fill" />{busy === 'start' ? '启动中…' : '启动'}</button>)}
    </div>
  )
}

export function Rail({ route }) {
  const influencerCount = useInfluencerAttention()
  return (
    <nav className="rail" aria-label="控制台导航">
      <div className="rail-head">
        <span className="sec-deco s" aria-hidden="true"><i className="sec-no">01</i><i className="sec-sl">/</i><i className="sec-en">CONSOLE</i></span>
        <span className="label" style={{ marginLeft: 4 }}>控制台</span>
        <span className="sec-rule" aria-hidden="true" />
      </div>
      <div className="nav">
        {NAV.map((n) => {
          const active = route.page === n.id
          const Ico = n.icon
          return (
            <div key={n.id}>
              <button
                type="button" className="nav-item"
                aria-current={active ? 'page' : undefined}
                title={n.label}
                onClick={() => navigate(n.sub ? `${n.id}/${n.sub[0].id}` : n.id)}
              >
                <Ico weight={active ? 'bold' : 'regular'} />
                <span className="ni-label">{n.label}</span>
                {n.id === 'influencer' && influencerCount > 0 && (
                  <span className="ni-badge num" aria-label={`${influencerCount} 项等你处理`}>{influencerCount}</span>
                )}
                <span className="ni-en" aria-hidden="true">{n.en}</span>
              </button>
              {active && n.sub && (
                <div className="nav-sub">
                  {n.sub.map((s) => (
                    <button
                      key={s.id} type="button"
                      aria-current={route.sub === s.id ? 'true' : undefined}
                      onClick={() => navigate(`${n.id}/${s.id}`)}
                    >{s.label}</button>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
      <Dock />
    </nav>
  )
}

/** Bottom-left assistant window. The figure is a placeholder for now. */
function Dock() {
  const { conn, data, running } = useStatus()
  const { draft, dirtyKeys } = useConfig()
  const name = (draft?.bot_display_name || '').trim() || 'webot'
  let line
  if (conn === 'connecting') line = <>正在连接控制台服务…</>
  else if (conn === 'offline') line = <>连不上控制台服务。</>
  else if (data?.error && String(data.error).includes('KEY_MISSING')) line = <>读不到微信密钥，要重新获取。</>
  else if (!running && data?.error) line = <>上次启动出错了。</>
  else if (dirtyKeys.length) line = <>有 <em>{dirtyKeys.length}</em> 项设置还没保存。</>
  else if (running) line = <>在岗中，已经处理 <em>{data?.messages_processed ?? 0}</em> 条消息。</>
  else line = <>没在跑，点右上角「启动」。</>
  return (
    <div className="dock" aria-label="助手">
      <div className="linebox" role="status">
        <div className="lb-name">{name}<span className="latin">WEBOT</span></div>
        <div className="lb-line">{line}</div>
      </div>
      <div className="bay" aria-hidden="true">
        <span className="ring" /><span className="skew" /><span className="skew-2" /><span className="vtext">WEBOT</span>
      </div>
      <div className="figure" aria-hidden="true"><FigurePlaceholder /></div>
      <span className="shadow" aria-hidden="true" />
      <span className="figure-tag" title="立绘还没定，先用剪影占位">立绘待定</span>
    </div>
  )
}

export function SaveBar() {
  const { dirtyKeys, errors, save, saving, discard, ready } = useConfig()
  const status = useStatus()
  const toast = useToast()
  const [restarting, setRestarting] = useState(false)
  if (!ready || !dirtyKeys.length) return null
  const errorCount = Object.keys(errors).length
  const pages = [...new Set(dirtyKeys.map(pageOfKey))]
  const doSave = async (restart) => {
    try {
      await save()
    } catch (e) {
      toast.err(`保存失败：${e.message}`)
      return
    }
    if (!(restart && status.running)) {
      toast.ok(status.running ? '已保存。机器人在运行，停止再启动后生效' : '已保存，下次启动机器人时生效', { ms: 6000 })
      return
    }
    setRestarting(true)
    try {
      await status.restart()
      toast.ok('已保存，机器人已重启，新设置已生效')
    } catch (e) {
      toast.err(`已保存，但重启没有成功：${e.message}`)
    } finally {
      setRestarting(false)
    }
  }
  return (
    <div className={`savebar${errorCount ? ' err' : ''}`} role="region" aria-label="未保存的设置">
      <span className="sb-count" aria-hidden="true">{String(dirtyKeys.length).padStart(2, '0')}</span>
      <div className="sb-text">
        <span>{dirtyKeys.length} 项修改未保存{errorCount ? <span className="sb-err"> · {errorCount} 项填得不对，改好才能保存</span> : null}</span>
        <small className="ellipsis">{pages.join('、')} · 保存后重启机器人生效</small>
      </div>
      <button type="button" className="btn on-slab" onClick={discard} disabled={saving}>放弃修改</button>
      {status.running && (
        <button type="button" className="btn on-slab" onClick={() => doSave(true)} disabled={saving || restarting || !!errorCount}>
          <ArrowClockwise />{restarting ? '重启中…' : '保存并重启'}
        </button>
      )}
      <button type="button" className="btn primary chc" onClick={() => doSave(false)} disabled={saving || !!errorCount}>
        {saving ? '保存中…' : '保存配置'}
      </button>
    </div>
  )
}
