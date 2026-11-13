import { useState } from 'react'
import { UsersThree, CheckCircle, X } from '@phosphor-icons/react'
import { useToast } from '../store/toast'
import { fmtClock } from '../lib/relay'
import { pickBossGroup, ignoreBossGroup, bossGroupsLine, dealAction } from '../lib/prm'
import { Card, Note, Confirm } from './ui'

// 老板拉的群 (PRM_BOSS_GROUPS, src/prm/boss_groups.py; GET /api/prm ``boss_groups``): the boss (PRM_GROUP_WITH) made a
// small WeChat group with a creator who is HIS friend and this account; from then on we handle her. With exactly one
// person there besides him and us the bot takes it over by itself (registers her, links her deal, asks the Mac for a
// group opening that waits in 待审批). With more, the group waits on this card: 「是她」 next to a member takes it over
// with her (POST pick {chat, wxid}, no question: the opening waits for a person anyway), 「这个群不是达人」 drops it for
// good after a confirmation (POST ignore {chat}). One line counts the groups taken over and those waiting (for her to
// join, or for their first lines); ``why_not`` says why it cannot run at all (its buttons are off then); ``warning``
// (a name of PRM_GROUP_WITH no one contact has: nothing is taken over by itself) is a warning above the groups, and
// the buttons stay on (second review 2026-10-31 F3). Off (or a bot without it): nothing at all.
// A group the bot did not take over by itself says why (its own ``why``: she was stopped, registering failed, not sure
// it is a new group, …; the card's ``warning`` is not said again on each group); one without a ``why`` and with more
// than one member listed says more than one may be her.
// (The card keeps its name 「老板拉的群」 whatever ``boss_call`` is: the 待审批 page's note sends operators to it by that
// name.)
// 接手任何合作群 (2026-11-09, src/prm/group_link.py): the admin sent 「接群」 in WeChat and was asked who in a group she is;
// that open question (``manual``, OFFER_SEC long) is a group here too, whatever PRM_BOSS_GROUPS says: 「是她」 next to a
// member links it by hand (the same POST pick {chat, wxid}: no 开场白, what was said there is context), 「不接」 drops
// the question (POST ignore {chat}, no confirmation: nothing is kept of it). With PRM_BOSS_GROUPS off only these show,
// under the name 「合作群」.
// 群自己接上 (PRM_GROUPS_AUTO_LINK, 2026-11-11): a group the bot's own scan found with two or more registered creators in
// it is such a question too (``by`` "scan", no ``until``: it stays until answered): the same 「是她」 / 「不接」 (「不接」: the
// scan leaves the group alone from then on).
// 老板拉的群里机器人自己选人 (PRM_BOSS_GROUP_PICK, 2026-11-13; ``bg.pick``): a pending group with more than one person who
// may be her is taken over by the bot itself (the group's name has one of them, or the first of her side to speak); each
// such group nobody confirmed yet is a row of ``bg.picked``: the deal (its 称呼 from the group's name), the member it hangs
// on and why, 「改挂到这位」 on each other member of her side (the same POST pick {chat, wxid}: the deal moves to that
// member, and a person chose: confirmed) and 「是她的群」 (the deal action group_confirm). The admin's 「接群 <群名>」 of
// such a group is a question here too (``rehook``): 「改挂到这位」 / 「不改」.

/** 她那边的人 (PRM_GROUP_HER_SIDE, 2026-11-11; ``bg.her_side`` / ``bg.max`` from /api/prm): whose lines in a linked group
 *  are not drafted, said truly in both states. ``size``: the group's when known (over ``bg.max`` her side does not
 *  count); ``boss``: a boss may be in it. */
export function othersLine(bg, { size = 0, boss = false } = {}) {
  const max = Number(bg?.max) || 0
  const side = !!bg?.her_side && !(size && max && size > max)
  if (!side) return boss ? '老板和别人说的话不起草。' : '别人说的话不起草。'
  const big = !size && max ? `（群超过 ${max} 人时只起草她本人的）` : ''
  return `她那边别的人（助理、商务）说的话也起草${big}；${boss ? '老板、' : ''}我们自己的人和别的登记过的达人说的话不起草。`
}

/** Who is drafted in a group linked to her (the toast after 「是她」): her, and with PRM_GROUP_HER_SIDE her side too. */
export function draftedWho(bg, size = 0) {
  const max = Number(bg?.max) || 0
  return bg?.her_side && !(size && max && size > max) ? '她和她那边的人' : '她'
}

/** A pending group with no reason of its own and more than one member listed: more than one may be her. */
const unsure = (g) => !g.why && (g.members || []).length > 1

/** The 合作单 page's card, at its top. */
export function BossGroupsCard({ d, onDone }) {
  const toast = useToast()
  const [busy, setBusy] = useState('')                  // the chat of the group being picked or dropped
  const [ignoring, setIgnoring] = useState(null)        // the group whose 「这个群不是达人」 asks first
  const bg = d?.boss_groups
  const manual = bg?.manual || []
  const ours = bg?.ours_warning || ''                   // (PRM_GROUP_OURS: a name of ours no one contact has)
  if (!bg?.enabled && !manual.length && !ours) return null
  const pending = bg.pending || []
  const boss = (bg.boss || []).join('、')
  const named = (g) => (g.name ? `「${g.name}」` : '这个群')
  const run = async (g, call, done) => {
    setBusy(g.chat)
    try {
      await call()
      toast.ok(done)
      setIgnoring(null)
      onDone()
    } catch (e) { toast.err(e.message) } finally { setBusy('') }
  }
  const pick = (g, m) => run(g, () => pickBossGroup(g.chat, m.wxid),
    `已接手${named(g)}：${m.name || m.wxid} 登记成达人，在群里打招呼的开场白写好后在「待审批」里等你看过再发`)
  const ignore = (g) => run(g, () => ignoreBossGroup(g.chat), `记下了：${named(g)}不是达人的群，机器人不管它`)
  const link = (g, m) => run(g, () => pickBossGroup(g.chat, m.wxid),
    `已接手${named(g)}：${m.name || m.wxid}${m.registered ? '' : ' 登记成达人'}，之后${draftedWho(bg, g.size)}在群里说的话会起草，在「待审批」里等你看`)
  const drop = (g) => run(g, () => ignoreBossGroup(g.chat), g.rehook ? `好的，${named(g)}不改了` : `好的，${named(g)}不接了`)
  const rehook = (g, m) => run(g, () => pickBossGroup(g.chat, m.wxid),
    `改好了：${named(g)}的合作单改挂在「${m.name || m.wxid}」身上，这个群也算确认过了`)
  const confirm = (g) => run(g, () => dealAction({ deal_id: g.deal_id, action: 'group_confirm', chat: g.chat }),
    `记下了：${named(g)}是她的群`)
  const onManual = (g) => (m) => (g.rehook ? rehook(g, m) : link(g, m))
  const picked = bg.picked || []
  if (!bg.enabled) {
    return (
      <Card title="合作群" eyebrow="GROUPS" className="prm-boss-groups"
        sub={manual.every((g) => g.by === 'scan') ? '机器人看到的群里不止一位登记过的达人：选哪个是这个群的达人，机器人就接手这个群'
          : '你在微信里发了「接群」：选群里哪个是达人，机器人就接手这个群'}>
        <div className="stack" style={{ gap: 10 }}>
          {ours && <Note kind="warn"><span className="prm-ours-warning">{ours}</span></Note>}
          {manual.map((g) => (
            <ManualGroupRow key={g.chat} g={g} bg={bg} disabled={!!busy} onPick={onManual(g)} onDrop={() => drop(g)} />
          ))}
        </div>
      </Card>
    )
  }
  return (
    <Card title="老板拉的群" eyebrow="BOSS GROUPS" className="prm-boss-groups"
      sub={`老板${boss ? `（${boss}）` : ''}把他微信里的达人和我们这个号拉进的小群：之后由我们跟她对接`}>
      <div className="stack" style={{ gap: 10 }}>
        {manual.map((g) => (
          <ManualGroupRow key={g.chat} g={g} bg={bg} disabled={!!busy} onPick={onManual(g)} onDrop={() => drop(g)} />
        ))}
        {picked.map((g) => (
          <PickedGroupRow key={g.chat} g={g} disabled={!!busy || !!bg.why_not} onMove={(m) => rehook(g, m)}
            onConfirm={() => confirm(g)} />
        ))}
        {bg.why_not && <Note kind="warn">{bg.why_not}</Note>}
        {bg.warning && <Note kind="warn">{bg.warning}</Note>}
        {ours && <Note kind="warn"><span className="prm-ours-warning">{ours}</span></Note>}
        <div className="f-hint">
          群里除了老板和我们只有她一个人、而且看得出是新拉的群时，机器人自己接手：把她登记成达人，写一份在群里打招呼的开场白，等你在「待审批」里看过再发。
          之后她在群里说的话照常起草，和私聊的草稿一个规矩（先审、门槛、下班时段、自动模式都照旧），发在群里，老板也看得到；<span className="prm-others-line">{othersLine(bg, { boss: true })}</span>
          {bg.pick
            ? ' 群里不止一个人可能是她时（她本人、助理、商务…），机器人自己接手：群名里有其中一个人的名字就挂在这个人身上，不然挂在她那边第一个在群里说话的人身上；合作单的名字取群名，她那边的人说的话都一样起草，你确认以前这个群的草稿都等你看。'
            : pending.some(unsure) && ' 群里不止一个人可能是她时，在下面点哪个是她。'}
        </div>
        <div className="prm-boss-counts">{bossGroupsLine(bg)}</div>
        {pending.map((g) => (
          <BossGroupRow key={g.chat} g={g} warning={bg.warning || ''} disabled={!!busy || !!bg.why_not}
            onPick={(m) => pick(g, m)} onIgnore={() => setIgnoring(g)} />
        ))}
      </div>
      {ignoring && (
        <Confirm title={`${named(ignoring)}不是达人的群？`} confirmLabel="不是达人" cancelLabel="再想想" danger
          busy={busy === ignoring.chat} onConfirm={() => ignore(ignoring)} onCancel={() => setIgnoring(null)}>
          这个群从这里去掉，以后不再问：机器人不接手，群里谁说什么都不起草、不回（和别的群一样）。
        </Confirm>
      )}
    </Card>
  )
}

/** The admin's open 「接群」 question (接手任何合作群): the group, how many are in it, until when the question counts, and
 *  who may be her (registered first, tagged 已登记 / 停用过), each with 「是她」; 「不接」 drops it. A deal's own 三方群 has
 *  her alone (``three_party``). */
function ManualGroupRow({ g, bg, disabled, onPick, onDrop }) {
  const until = g.until ? `${fmtClock(g.until)} 前有效` : ''
  const scan = g.by === 'scan'                          // (群自己接上: the bot's own scan asks, not the admin's 「接群」)
  const rehook = !!g.rehook                             // (老板拉的群里机器人自己选人: 「改挂到谁」)
  return (
    <div className="stack prm-boss-group prm-manual-group" style={{ gap: 6 }}>
      <div>
        <b><UsersThree style={{ verticalAlign: -2 }} /> {g.name || '（没有群名）'}</b>{' '}
        <span className="dim">{[g.size ? `${g.size} 人` : '', scan ? (g.why ? '机器人看到的群：没自动接' : '机器人看到的群：里面不止一位登记过的达人，没自动接') : rehook ? '你在微信里发的「接群」：改挂到谁' : '你在微信里发的「接群」', until].filter(Boolean).join(' · ')}</span>
      </div>
      {scan && g.why && <div className="f-hint prm-ask-why">为什么没自动接：{g.why}</div>}
      <div className="f-hint">
        {rehook ? '机器人自己接手的老板拉的群：点哪位，合作单（名字不变）就改挂在 TA 身上，私聊只算 TA 的；群里她那边的人照旧都起草。也可以在微信里回「是 编号」。'
          : g.three_party ? '这是她的三方群（「拉三方群」建的，现在只读）：点「是她」以后她在群里说的话会起草，回复发在群里。'
          : scan ? '点这个群是哪位达人的：机器人接手这个群，群里最近的话当背景，她在等回复就起草一份。不是合作群就点「不接」，机器人以后不再自己接它。'
            : `点哪个是达人（也可以是她的商务）：机器人接手这个群，群里最近的话当背景，她在等回复就起草一份；${othersLine(bg, { size: g.size })}也可以在微信里回「是 编号」。`}
      </div>
      {(g.members || []).map((m) => (
        <div key={m.wxid} className="row prm-boss-member" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <span>{m.name || '（没有名字）'}</span>
          {m.registered && <span className="tag cy">已登记</span>}
          {m.stopped && <span className="tag off">停用过</span>}
          <span className="mono dim">{m.wxid}</span>
          <button type="button" className="btn sm primary" disabled={disabled}
            aria-label={`${m.name || m.wxid} ${rehook ? '改挂到这位' : '是她'}`}
            onClick={() => onPick(m)}><CheckCircle />{rehook ? '改挂到这位' : '是她'}</button>
        </div>
      ))}
      <div>
        <button type="button" className="btn sm ghost" disabled={disabled} onClick={onDrop}><X />{rehook ? '不改' : '不接'}</button>
      </div>
    </div>
  )
}

/** 老板拉的群里机器人自己选人: a group the bot took over by choosing whom its deal hangs on, nobody confirmed yet: the deal
 *  (its 称呼 from the group's name), the member it hangs on and why, 「改挂到这位」 on each other member of her side, and
 *  「是她的群」. */
function PickedGroupRow({ g, disabled, onMove, onConfirm }) {
  const hook = g.hook?.name || '（没有名字）'
  return (
    <div className="stack prm-boss-group prm-picked-group" style={{ gap: 6 }}>
      <div>
        <b><UsersThree style={{ verticalAlign: -2 }} /> {g.name || '（没有群名）'}</b>{' '}
        <span className="dim">{[g.size ? `${g.size} 人` : '', '机器人自己接手的，还没确认'].filter(Boolean).join(' · ')}</span>
      </div>
      <div className="f-hint prm-picked-hook">
        合作单「{g.call}」挂在「{hook}」身上（{g.how}）。群里她那边的人说的话都一样起草，私聊只算挂着的这个人的；确认以前这个群的草稿都等你看。挂错了人就点另一位的「改挂到这位」。
      </div>
      {(g.members || []).map((m) => (
        <div key={m.wxid} className="row prm-boss-member" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <span>{m.name || '（没有名字）'}</span>
          <span className="mono dim">{m.wxid}</span>
          <button type="button" className="btn sm" disabled={disabled} aria-label={`${m.name || m.wxid} 改挂到这位`}
            onClick={() => onMove(m)}><CheckCircle />改挂到这位</button>
        </div>
      ))}
      <div>
        <button type="button" className="btn sm primary" disabled={disabled || !g.deal_id} onClick={onConfirm}>
          <CheckCircle />是她的群</button>
      </div>
    </div>
  )
}

/** One group waiting for a person: its name, when it was found, why the bot did not take it over (its ``why``, unless
 *  it is the card's ``warning`` said above) or that more than one may be her, and who may be her (each with 「是她」). */
function BossGroupRow({ g, warning, disabled, onPick, onIgnore }) {
  const found = g.found_at ? `${fmtClock(g.found_at)} 发现` : ''
  const line = [found, unsure(g) ? '群里不止一个人可能是她，点哪个是她' : ''].filter(Boolean).join('：')
  const why = g.why && g.why !== warning ? g.why : ''
  return (
    <div className="stack prm-boss-group" style={{ gap: 6 }}>
      <div>
        <b><UsersThree style={{ verticalAlign: -2 }} /> {g.name || '（没有群名）'}</b>{' '}
        {line && <span className="dim">{line}</span>}
      </div>
      {why && <Note kind="warn">{why}</Note>}
      {(g.members || []).map((m) => (
        <div key={m.wxid} className="row prm-boss-member" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <span>{m.name || '（没有名字）'}</span>
          <span className="mono dim">{m.wxid}</span>
          <button type="button" className="btn sm primary" disabled={disabled} aria-label={`${m.name || m.wxid} 是她`}
            onClick={() => onPick(m)}><CheckCircle />是她</button>
        </div>
      ))}
      <div>
        <button type="button" className="btn sm ghost" disabled={disabled} onClick={onIgnore}><X />这个群不是达人</button>
      </div>
    </div>
  )
}
