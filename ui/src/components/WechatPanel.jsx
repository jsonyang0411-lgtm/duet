import { useEffect, useState } from 'react'
import { FolderOpen, MagnifyingGlass, ArrowUp, Folder, CheckCircle } from '@phosphor-icons/react'
import { get, post } from '../api'
import { useConfig, useField } from '../store/config'
import { useGroups } from '../lib/groups'
import { PageHead, Card, Field, Note, Dialog, Seg, ChipsInput, Switch, TextInput } from './ui'
import { CfgSeg } from './cfg'
import { AdvancedFields } from './AdvancedPanel'

// 微信连接: how the bot reads and sends WeChat messages. Everything here is a
// main setting (store/config.jsx, saved with the bottom save bar) except the OCR
// fallback, which is an advanced switch with its own save bar.

// The literal option values are checked by tests/test_macos_adaptation.py.
export const WECHAT_BACKENDS = [
  { value: 'wcdb', label: 'WCDB 直读（Windows）' },
  { value: 'mac_hybrid', label: 'macOS WeFlow' },
  { value: 'mac_ui', label: 'macOS 界面' },
]

export default function WechatPanel() {
  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead no="02" en="WECHAT" title="微信连接" sub="机器人怎么读微信、读谁的消息、读不了数据库时怎么办" />
        <div className="stack" style={{ gap: 16 }}>
          <Card title="微信后端" eyebrow="BACKEND">
            <CfgSeg
              k="wechat_backend" label="怎么读写微信" envKey="WECHAT_BACKEND" options={WECHAT_BACKENDS}
              hint="Windows 用 WCDB 直读本地数据库；macOS 上选 WeFlow（读数据库）或界面（只靠窗口）"
            />
          </Card>
          <PrivateCard />
          <GroupsCard />
          <Note kind="cy">
            机器人从不在群里说话：群消息只记录，@ 它、叫它的名字都不会回复。中转、达人副驾驶和管理员命令都只认私聊。微信后端至少要读一个群才会启动，填一个测试群就够。
          </Note>
          <section className="stack" style={{ gap: 12 }} aria-label="读取备选">
            <AdvancedFields only={['ocr']} intro={false} />
          </section>
          <DataSection />
        </div>
      </div>
    </div>
  )
}

/** Private chats: the relay, the creators and the admin all write in private chats. */
function PrivateCard() {
  const [enabled, setEnabled] = useField('private_chat_enabled')
  const [value, setValue] = useField('private_chats')
  const names = Array.isArray(value) ? value : []
  const [wantSome, setWantSome] = useState(false)
  const mode = names.length || wantSome ? 'some' : 'all'
  return (
    <Card title="私聊" eyebrow="PRIVATE">
      <div className="toggle-row">
        <div className="tr-text">
          <span className="tr-title">读取私聊</span>
          <div className="f-hint">中转的 cc 任务、达人的消息、管理员命令都在私聊里，必须打开。<span className="f-key">PRIVATE_CHAT_ENABLED</span></div>
        </div>
        <Switch checked={!!enabled} onChange={setEnabled} label="读取私聊" />
      </div>
      {!enabled && <Note kind="warn">私聊关着：机器人收不到你发的 cc 任务。</Note>}
      {enabled && (
        <div className="stack" style={{ gap: 10, marginTop: 12 }}>
          <Field label="读哪些私聊" envKey="PRIVATE_CHATS" hint="建议只填会发 cc 任务的人（微信昵称或 wxid）；登记过的达人不用填。其他人的私聊机器人只记录、不回复">
            <div><Seg small value={mode} onChange={(v) => { setWantSome(v === 'some'); if (v === 'all') setValue([]) }} options={[{ value: 'all', label: '所有私聊' }, { value: 'some', label: '只读这些人' }]} /></div>
          </Field>
          {mode === 'some' && <ChipsInput split={false} value={names} onChange={setValue} placeholder="输入对方的微信昵称或 wxid 后回车（一次一个）" />}
          {mode === 'all' && <Note kind="warn">所有私聊都会被读取并记录。不在「谁能用」名单里的人发 cc 不会执行，机器人也不回他们。</Note>}
        </div>
      )}
    </Card>
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
    <Card title="读取哪些群" eyebrow="GROUPS">
      <Field envKey="WECHAT_GROUPS" hint="只有列出的群会被读取；群消息只记录，机器人不回复。群名要和微信里完全一致">
        <div><Seg
          label="读取范围" value={mode}
          onChange={(v) => { setWantSome(v === 'some'); if (v === 'all') setValue([]) }}
          options={[{ value: 'all', label: '全部群聊' }, { value: 'some', label: '只读这些群' }]}
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
