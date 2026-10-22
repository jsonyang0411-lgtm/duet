import { useState } from 'react'
import { useField } from '../store/config'
import { PageHead, Card, Field, Note, Seg, ChipsInput, Switch } from './ui'
import { CfgSeg } from './cfg'
import { WECHAT_BACKENDS, GroupsCard, DataSection } from './ConfigPanel'
import { AdvancedFields } from './AdvancedPanel'

// 微信连接: how the bot reads and sends WeChat messages. Everything here is a
// main setting (store/config.jsx, saved with the bottom save bar) except the OCR
// fallback, which is an advanced switch with its own save bar.

export default function WechatPanel() {
  return (
    <div className="main-scroll scroll">
      <div className="page">
        <PageHead no="02" en="WECHAT" title="微信连接" sub="机器人怎么读微信、听谁的消息、读不了数据库时怎么办" />
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
            中转只认私聊；群聊在这里只用来让机器人启动和给 OCR 备选读取用。不想让机器人在群里说话，就只填一个测试群。
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

/** Private chats: the relay listens to the allowed people's private chats only. */
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
          <div className="f-hint">中转的「cc 任务」都在私聊里发，必须打开。<span className="f-key">PRIVATE_CHAT_ENABLED</span></div>
        </div>
        <Switch checked={!!enabled} onChange={setEnabled} label="读取私聊" />
      </div>
      {!enabled && <Note kind="warn">私聊关着：机器人收不到你发的 cc 任务。</Note>}
      {enabled && (
        <div className="stack" style={{ gap: 10, marginTop: 12 }}>
          <Field label="读哪些私聊" envKey="PRIVATE_CHATS" hint="建议只填你自己（微信昵称或 wxid）：其他人的私聊不是中转任务，机器人会当普通聊天处理">
            <div><Seg small value={mode} onChange={(v) => { setWantSome(v === 'some'); if (v === 'all') setValue([]) }} options={[{ value: 'all', label: '所有私聊' }, { value: 'some', label: '只读这些人' }]} /></div>
          </Field>
          {mode === 'some' && <ChipsInput split={false} value={names} onChange={setValue} placeholder="输入对方的微信昵称或 wxid 后回车（一次一个）" />}
          {mode === 'all' && <Note kind="warn">所有私聊都会被读取。不在「谁能用」名单里的人发 cc 不会执行，但他们的私聊会被当作普通聊天回复。</Note>}
        </div>
      )}
    </Card>
  )
}
