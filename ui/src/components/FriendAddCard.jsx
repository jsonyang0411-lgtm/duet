import { useState } from 'react'
import { UserPlus, X, Eye, EyeSlash, Copy, CheckCircle, DeviceMobile, HandWaving, PencilSimple, LinkSimple, Warning } from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import { friendAddAction, FRIEND_ADD_TAGS, friendAddStatusLine, inviteLine } from '../lib/prm'
import { Card, Note, Confirm, Dialog, Field, TextInput, Seg } from './ui'

// 加百应达人微信 (PRM_FRIEND_ADD, src/prm/friend_adds.py): a creator invited in 百应 left her 微信号 / 手机号. One
// click on 「添加该达人微信」 (after its confirmation) is ONE friend request through the friend manager; nothing is
// ever retried by itself. Numbers are masked unless 显示完整 / 复制号码 is clicked (each look is written down).
// Once she accepted, 「是她，登记并关联」 registers her as a creator with her 抖音号.

const KIND_LABELS = { wechat: '微信号', phone: '手机' }

/** The 待审批 page's group: the open records, one card each (nothing when there are none). */
export function FriendAddList({ d, onDone }) {
  const st = d?.friend_add_status || {}
  const shown = (d?.friend_adds || []).filter((r) => r.listed)
  if (!shown.length) return null
  return (
    <div className="stack prm-friend-adds" style={{ gap: 12 }}>
      <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'baseline' }}>
        <b>加微信 · 百应达人留了联系方式</b>
        <span className="dim">{friendAddStatusLine(st)}</span>
      </div>
      {st.paused_why && (
        <Note kind="need">自动同意好友暂停着（{st.paused_why}）：控制台加好友也停着。过几个小时由管理员发「自动同意好友 开」再继续。</Note>
      )}
      {shown.map((r) => <FriendAddCard key={r.id} r={r} st={st} d={d} onDone={onDone} />)}
    </div>
  )
}

function FriendAddCard({ r, st, d, onDone }) {
  const toast = useToast()
  const [greeting, setGreeting] = useState(r.greeting || '')
  const [use, setUse] = useState(r.has_wechat ? 'wechat' : 'phone')
  const [confirming, setConfirming] = useState(false)
  const [dropping, setDropping] = useState(false)
  const [linking, setLinking] = useState(false)
  const [editing, setEditing] = useState(r.state === 'needs_view')
  const [full, setFull] = useState(null)                 // the full numbers, after 显示完整
  const [busy, setBusy] = useState(false)
  const act = async (action, body = {}, done = '') => {
    setBusy(true)
    try {
      const out = await friendAddAction({ id: r.id, action, ...body })
      if (done) toast.ok(done)
      setConfirming(false); setDropping(false); setLinking(false); setEditing(false)
      onDone()
      return out
    } catch (e) { toast.err(e.message); return null } finally { setBusy(false) }
  }
  const reveal = async () => {
    const out = await act('reveal')
    if (out) setFull({ wechat: out.wechat, phone: out.phone })
    return out
  }
  const copy = async () => {
    const out = full || await reveal()
    const value = out && (out[use] || out.wechat || out.phone)
    if (!value) return
    try { await navigator.clipboard.writeText(value); toast.ok('号码已复制（这次查看记下了）：到手机微信里搜索添加') } catch { toast.err('没能放进剪贴板：点「显示完整」自己抄') }
  }
  const tag = FRIEND_ADD_TAGS[r.state] || 'pend'
  const canAddHere = st.enabled && st.can_add
  const addable = ['offered', 'failed', 'blocked'].includes(r.state)
  const target = r.masked?.[use] || r.masked?.wechat || r.masked?.phone || ''
  const length = [...(greeting || '')].length
  const max = st.greeting_max || 30
  const invite = inviteLine(r.invited)
  return (
    <Card
      title={`加微信 · ${r.nickname || '（没有昵称）'}`}
      eyebrow={`ADD #${r.id}`}
      sub={`抖音号 ${r.aweme_id || '—'}${r.invited?.plan_id ? '（台账对上）' : ''} · ${r.source_label} · ${fmtClock(r.created_at)}`}
      actions={<span className={`tag ${tag}`}>{r.state_label}</span>}
      className={`prm-friend-add ${r.state}`}
    >
      <div className="stack" style={{ gap: 8 }}>
        {invite && <div className="f-hint">邀约：{invite}</div>}
        <ContactLine r={r} full={full} onReveal={reveal} onHide={() => setFull(null)} />
        <StateNote r={r} st={st} />

        {addable && !editing && (
          <div className="stack" style={{ gap: 8 }}>
            {r.has_wechat && r.has_phone && (
              <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                <span className="dim">用哪个加：</span>
                <Seg small value={use} label={`#${r.id} 用哪个加`} onChange={setUse}
                  options={[{ value: 'wechat', label: '微信号' }, { value: 'phone', label: '手机号' }]} />
              </div>
            )}
            {canAddHere && (
              <Field label="附言" hint={`${length}/${max}${r.same_greeting?.length ? ` · 和 #${r.same_greeting.join('、#')} 的附言一样，通过以后认不出是谁：加上她的昵称` : ''}`}
                error={length > max ? `附言最多 ${max} 个字` : ''}>
                <TextInput value={greeting} onChange={setGreeting} aria-label={`#${r.id} 的附言`} />
              </Field>
            )}
            <label className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input type="checkbox" checked={!!r.link_on_accept} disabled={busy} aria-label={`#${r.id} 她通过以后按这份邀约登记`}
                onChange={(e) => act('link_on_accept', { on: e.target.checked })} />
              <span>她通过以后按这份邀约登记成达人（是不是她，还是要你点一下确认）</span>
            </label>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              {canAddHere ? (
                <button type="button" className="btn primary" disabled={busy || !!st.blocked_why || length > max || !target}
                  onClick={() => setConfirming(true)}><UserPlus />添加该达人微信</button>
              ) : (
                <button type="button" className="btn primary" disabled={busy || !(r.has_wechat || r.has_phone)} onClick={copy}><Copy />复制号码</button>
              )}
              <button type="button" className="btn" disabled={busy} onClick={() => act('sent_by_phone', {}, '记下了：已经在手机上发了申请')}><DeviceMobile />我在手机上加了</button>
              <button type="button" className="btn" disabled={busy} onClick={() => act('wait_her', {}, '记下了：等她加我们')}><HandWaving />请她加我们</button>
              <button type="button" className="btn ghost" disabled={busy} onClick={() => setDropping(true)}><X />不加</button>
            </div>
            {canAddHere && st.blocked_why && <div className="dim">{st.blocked_why}</div>}
            {!canAddHere && <div className="dim">{st.enabled ? '这台电脑没有好友管理组件（要 Windows 版微信窗口），只能复制号码在手机上加' : '加百应达人微信没打开（PRM_FRIEND_ADD）：可以复制号码在手机上加'}</div>}
          </div>
        )}

        {editing && <EditNumbers r={r} busy={busy} onSave={(contact) => act('edit', { contact }, '号码已保存，再确认添加')} onCancel={() => setEditing(false)} />}

        {!addable && !editing && (
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            {r.state === 'unsure' && <>
              <button type="button" className="btn primary" disabled={busy} onClick={() => act('sent_already', {}, '记下了：申请发出去了')}><CheckCircle />发出去了</button>
              <button type="button" className="btn" disabled={busy} onClick={() => act('not_sent', {}, '记下了：没发出去，可以再加')}><X />没发出去</button>
            </>}
            {['not_found', 'needs_view', 'waiting'].includes(r.state) && (
              <button type="button" className="btn" disabled={busy} onClick={() => setEditing(true)}><PencilSimple />{r.state === 'not_found' ? '改号' : '填号码'}</button>
            )}
            {r.state === 'not_found' && (
              <button type="button" className="btn" disabled={busy} onClick={() => act('wait_her', {}, '记下了：等她加我们')}><HandWaving />请她加我们</button>
            )}
            {r.can_link && (
              <button type="button" className="btn primary" disabled={busy} onClick={() => setLinking(true)}><LinkSimple />
                {r.state === 'known' ? '选是哪个聊天并关联' : r.state === 'requested' ? '她已经通过了，我来选是哪个聊天'
                  : '她已经加了我们，我来选是哪个聊天'}</button>
            )}
            {r.open && r.state !== 'adding' && r.state !== 'unsure' && (
              <button type="button" className="btn ghost" disabled={busy} onClick={() => setDropping(true)}><X />不加</button>
            )}
          </div>
        )}
      </div>
      {confirming && (
        <Confirm title={`添加 #${r.id} 的微信？`} confirmLabel="确定添加" cancelLabel="取消" busy={busy}
          onConfirm={() => act('add', { use, greeting }, '已开始添加：别动键盘鼠标')} onCancel={() => setConfirming(false)}>
          机器人会在这台电脑的微信里搜索 {target}，发一条好友申请（附言：{greeting || '不带附言'}）。接下来大约 10 秒它会操作微信窗口，请别动键盘鼠标。点一次只发一次申请，不会自动重试。
        </Confirm>
      )}
      {dropping && (
        <Confirm title={`#${r.id} 不加了？`} confirmLabel="不加" cancelLabel="再想想" danger busy={busy}
          onConfirm={() => act('drop', {}, '记下了：不加')} onCancel={() => setDropping(false)}>
          她留的号码会马上清除（只留打码的），这一条不再出现在这里。
        </Confirm>
      )}
      {linking && <LinkPicker r={r} d={d} busy={busy} onLink={(wxid, name) => act('link', { wxid, name },
        r.link_on_accept ? '已登记并关联：她的抖音号记进合作单了' : '记下了是这个聊天')} onCancel={() => setLinking(false)} />}
    </Card>
  )
}

/** What she left, masked; 显示完整 asks the bot (written down) and only this page sees it. */
function ContactLine({ r, full, onReveal, onHide }) {
  const kinds = Object.keys(KIND_LABELS).filter((k) => r.masked?.[k])
  if (!kinds.length) return <div className="dim">她留的：{r.unavailable ? '（这次没读到号码）' : '（没有号码）'}</div>
  const canSee = r.has_wechat || r.has_phone
  return (
    <div>
      她留的：{kinds.map((k) => (
        <span key={k} style={{ marginRight: 12 }}>{KIND_LABELS[k]} <span className="mono">{full ? full[k] || r.masked[k] : r.masked[k]}</span></span>
      ))}
      {r.cleared && !canSee && <span className="dim">（号码已按规定清除）</span>}
      {canSee && !full && <button type="button" className="link" onClick={onReveal}><Eye style={{ verticalAlign: -2 }} />显示完整</button>}
      {full && <button type="button" className="link" onClick={onHide}><EyeSlash style={{ verticalAlign: -2 }} />隐藏</button>}
    </div>
  )
}

function StateNote({ r, st }) {
  switch (r.state) {
    case 'adding': return <Note kind="warn">正在加…（别动键盘鼠标）</Note>
    case 'requested': return <Note kind="cy">已发申请 {fmtClock(r.requested_at)}，等她通过（14 天内）。{r.reason === 'by_phone' ? '（运营在手机上加的）' : ''}</Note>
    case 'not_found': return <Note kind="warn">{r.reason_text || '微信里搜不到：号可能不对，或者她关了「可以通过微信号 / 手机号搜到我」'}</Note>
    case 'unsure': return <Note kind="warn">{r.reason_text}：先去微信「通讯录 › 新的朋友」或她的资料卡看一眼，再点下面的按钮。</Note>
    case 'blocked': return <Note kind="need" icon={Warning}>微信提示「操作过于频繁」，已经停了。自动同意好友和控制台加好友都暂停，过几个小时由管理员发「自动同意好友 开」再继续。{st.paused_why ? '' : '（暂停已经解除，可以再加）'}</Note>
    case 'failed': return <Note kind="warn">上次没发出去：{r.reason_text}。可以再点一次（算新的一次）。</Note>
    case 'needs_view': return <Note kind="warn">她发了联系方式卡片，这次没读到号码：请在百应里打开会话，点卡片上的复制（平台可能记一次「查看」），把号码填到这里。</Note>
    case 'known': return <Note>她已经是微信好友，不用加：选是哪个聊天，登记并关联。</Note>
    case 'waiting': return <Note>等她加我们：她的好友申请到了以后，在「合作单 › 新加的好友」里能看到；是她就点下面的按钮选那个聊天。</Note>
    default: return r.reason_text ? <div className="dim">{r.reason_text}</div> : null
  }
}

function EditNumbers({ r, busy, onSave, onCancel }) {
  const [wechat, setWechat] = useState('')
  const [phone, setPhone] = useState('')
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <Field label="微信号" className="grow"><TextInput mono value={wechat} onChange={setWechat} placeholder={r.masked?.wechat || 'ab_cd123'} aria-label={`#${r.id} 的微信号`} /></Field>
        <Field label="手机号" className="grow"><TextInput mono value={phone} onChange={setPhone} placeholder={r.masked?.phone || '138 0000 0000'} aria-label={`#${r.id} 的手机号`} /></Field>
      </div>
      <div className="row" style={{ gap: 8 }}>
        <button type="button" className="btn primary" disabled={busy || !(wechat.trim() || phone.trim())} onClick={() => onSave({ wechat: wechat.trim(), phone: phone.trim() })}>保存</button>
        {r.state !== 'needs_view' && <button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button>}
      </div>
    </div>
  )
}

/** Which chat she is: the new friends first, then the recent private chats, then the creators registered already. */
function LinkPicker({ r, d, busy, onLink, onCancel }) {
  const candidates = d?.candidates || []
  const parties = (d?.parties || []).filter((p) => p.active)
  const [wxid, setWxid] = useState(r.candidate?.wxid || '')
  const [name, setName] = useState(r.nickname || '')
  const options = [
    ...candidates.filter((c) => c.new).map((c) => ({ wxid: c.wxid, label: `新好友 · ${c.name}`, hint: c.how_label })),
    ...candidates.filter((c) => !c.new).map((c) => ({ wxid: c.wxid, label: c.name, hint: '最近的私聊' })),
    ...parties.map((p) => ({ wxid: p.wxid, label: `已登记 · ${p.name}`, hint: '只补关联' })),
  ]
  return (
    <Dialog title={`#${r.id} 是哪个聊天？`} eyebrow="LINK" onClose={onCancel}
      footer={<>
        <button type="button" className="btn" data-autofocus onClick={onCancel}>取消</button>
        <button type="button" className="btn primary chc" disabled={busy || !wxid.trim()} onClick={() => onLink(wxid.trim(), name.trim())}>
          {r.link_on_accept ? '是她，登记并关联' : '记下是这个聊天'}</button>
      </>}>
      <div className="stack" style={{ gap: 10 }}>
        <div className="dim">选她通过申请以后的那个微信私聊。{r.link_on_accept ? `登记以后她就是达人，抖音号 ${r.aweme_id} 记进合作单，Mac 起草时带上邀约背景。` : '只记下是哪个聊天，不登记成达人。'}</div>
        <div className="stack prm-link-options" style={{ gap: 4, maxHeight: 260, overflow: 'auto' }}>
          {options.map((o) => (
            <label key={o.wxid} className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input type="radio" name={`link-${r.id}`} checked={wxid === o.wxid} onChange={() => setWxid(o.wxid)} aria-label={`选 ${o.label}`} />
              <span>{o.label}</span><span className="mono dim">{o.wxid}</span><span className="dim">{o.hint}</span>
            </label>
          ))}
          {!options.length && <div className="dim">还没有新好友或最近的私聊：可以直接填她的 wxid。</div>}
        </div>
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <Field label="wxid" className="grow"><TextInput mono value={wxid} onChange={setWxid} placeholder="wxid_xxx" aria-label={`#${r.id} 关联的 wxid`} /></Field>
          {r.link_on_accept && <Field label="称呼" className="grow"><TextInput value={name} onChange={setName} aria-label={`#${r.id} 登记的称呼`} /></Field>}
        </div>
      </div>
    </Dialog>
  )
}

/** 合作单 page: an operator types what a creator left in 百应 (the console's own way in, no 百应 inbox needed). */
export function FriendAddPasteCard({ d, onDone }) {
  const toast = useToast()
  const st = d?.friend_add_status || {}
  const [aweme, setAweme] = useState('')
  const [nickname, setNickname] = useState('')
  const [wechat, setWechat] = useState('')
  const [phone, setPhone] = useState('')
  const [greeting, setGreeting] = useState('')
  const [busy, setBusy] = useState(false)
  if (!st.enabled) return null
  const save = async () => {
    setBusy(true)
    try {
      const out = await friendAddAction({ action: 'paste', aweme_id: aweme.trim(), nickname: nickname.trim(),
        contact: { wechat: wechat.trim(), phone: phone.trim() }, greeting: greeting.trim() || undefined })
      toast.ok(out.state === 'waiting' ? `已记下 #${out.id}：没有号码，等她加我们` : `已记下 #${out.id}：到「待审批 › 加微信」确认添加`)
      setAweme(''); setNickname(''); setWechat(''); setPhone(''); setGreeting('')
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy(false) }
  }
  return (
    <Card title="手动登记百应达人留的联系方式" eyebrow="BAIYING" className="prm-friend-paste">
      <div className="stack" style={{ gap: 10 }}>
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <Field label="抖音号" hint="必填，在 Mac 的邀约通知或百应页面上看" className="grow">
            <TextInput mono value={aweme} onChange={setAweme} placeholder="dy_xxx" aria-label="百应达人的抖音号" />
          </Field>
          <Field label="昵称（可选）" className="grow"><TextInput value={nickname} onChange={setNickname} aria-label="百应昵称" /></Field>
        </div>
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <Field label="微信号" className="grow"><TextInput mono value={wechat} onChange={setWechat} aria-label="她留的微信号" /></Field>
          <Field label="手机号" className="grow"><TextInput mono value={phone} onChange={setPhone} aria-label="她留的手机号" /></Field>
        </div>
        <Field label="附言（可选）" hint={`不填就用默认的：${st.default_greeting || ''}（会带上她的昵称）`}>
          <TextInput value={greeting} onChange={setGreeting} aria-label="加微信的附言" />
        </Field>
        <Note>号码都空着也行：记成「等她加我们」。百应页面上卡片里的号码是打码的，要拿完整号码得点卡片上的复制，平台可能记一次「查看」；她在文字里直接打的号码没有这个问题。</Note>
        <div><button type="button" className="btn primary" disabled={busy || !aweme.trim()} onClick={save}><UserPlus />记下来</button></div>
      </div>
    </Card>
  )
}
