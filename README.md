# webot

> 商务微信号的网关：把私聊里的任务转给 Mac 上的 Claude Code，把已登记达人的私聊交给 Mac 起草、运营审批后再发。

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%2F11%20%7C%20macOS-blue?style=flat-square&logo=windows" alt="Platform" />
  <img src="https://img.shields.io/badge/python-3.13%2B-green?style=flat-square&logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/license-MIT-yellow?style=flat-square" alt="License" />
</p>

> ## 使用声明
> 
> 本项目仅供学习交流使用。严禁用于发送垃圾信息、骚扰他人、诈骗钓鱼等违法违规行为。使用者自行承担一切后果与风险。

---

## 它做什么

webot 登录在一个商务微信号上，只做这几件事：

- **Claude 中转**：允许的人在私聊里发「cc 任务」，机器人把任务放进队列，Mac 上的 worker（`tools/claude_relay/worker.py`）取走交给 Claude Code，做完把结果发回私聊。
- **达人副驾驶**（`PRM_COPILOT`）：已登记达人的私聊不回任何话，静默交给 Mac 起草，草稿在控制台「待审批」里等运营发送、修改或丢弃；合作单、寄样、物流、清理收件信息都在控制台里。
- **微信边缘**（`PRM_EDGE`）：这台电脑只替 Mac 上的业务服务收发名单里达人的微信（协议见 `edge-protocol.md`）。
- **管理员命令**：管理员（`ADMIN_WXID`）在私聊里发 `查状态`、`添加好友 号码 [附言]`、`同意好友`、`自动同意好友 开|关`、`帮助`。

**它从不陪聊**：没有聊天模型、不需要任何 AI 密钥。所有群消息（@ 它、叫它的名字也一样）和其他人的私聊只记录、不回复。

---

## 控制台

打开 EXE 后会弹出网页控制台（只监听本机），左侧六页：

- **运行状态**：机器人在不在线、处理了多少消息、Mac worker 在不在
- **中转代理**：中转任务列表（可取消）、Mac 端怎么接、中转设置（token、端口、谁能用）
- **待审批**：达人的回复草稿，点「发送」才会发出去
- **合作单**：登记 / 停用达人，合作阶段、寄样、物流
- **微信连接**：微信后端、读哪些私聊和群、数据目录、OCR 备选
- **运行日志**

电脑微信不在线的那几段（哪段时间、要在手机上看一遍）在每页顶上提醒，点「看过了」才消失。

首次打开是三步设置：微信密钥 → 微信设置 → 中转设置。

---

## 安装

### Windows

```bash
pip install -r requirements.txt
cd ui && npm install && npm run build && cd ..
python desktop.py
```

打包成 EXE：`pyinstaller build.spec`，输出 `dist/webot.exe`。

前提：电脑上已登录微信。机器人要读的群需要先添加到通讯录（群聊右上角 `···` → 开启「添加到通讯录」）；微信后端至少要读到一个群才会启动，填一个测试群就够。

### macOS（实验）

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-macos.txt
cd ui && npm install && npm run build && cd ..
python desktop_mac.py
```

需要在系统设置中给终端授权「辅助功能」权限。`desktop_mac.py` 默认写入
`WECHAT_BACKEND=mac_hybrid`，通过 WeFlow 直接读取本地 WCDB，并使用辅助功能发送消息；
不需要额外启动消息读取服务。

首次配置 WeFlow 的本地读取能力时，在项目根目录运行：

```bash
python tools/macos/weflow_setup.py extract-keys
python tools/macos/weflow_setup.py import-keys
python tools/macos/weflow_setup.py diagnose
```

密钥会保存在微信账户目录的 `all_keys.json`。若普通提取没有结果，可在重启微信后使用：

```bash
python tools/macos/weflow_setup.py extract-keys-restart-hook
```

`WECHAT_BACKEND=mac_ui` 是只依赖辅助功能的实验性回退模式；需要数据库直读时使用
`WECHAT_BACKEND=mac_hybrid`。

### Mac 上的 worker

Claude 中转和达人副驾驶的起草都由 Mac 上的 worker 完成，安装和配置见 `tools/claude_relay/README.md`。

---

## 配置

设置都在控制台里改，保存后重启机器人生效；所有配置项和说明见 `.env.example`。

---

## 常见问题

**需要 AI 密钥吗？**
不需要。机器人自己不调用任何模型；Claude Code 跑在 Mac 上。

**群里 @ 它会怎样？**
不会有任何回复，消息只记录。

**微信能最小化吗？**
读消息不受影响，发消息时微信窗口需要可见。

**电脑微信退出了（重启电脑、升级微信、被挤下线），这段时间达人的消息怎么办？**
电脑微信重新登录后，默认不会把退出期间发到手机上的消息同步过来，机器人也就读不到。扫码登录时，在手机的确认页上勾选「同步最近的聊天记录」，这段时间的消息就会同步到电脑，机器人会自动补交成草稿（2026-10-28 真机确认）。所以电脑微信要一直登录着；不得不退出时，重新登录一定要勾选同步。

机器人会记下电脑微信哪段时间不在线（运行中看到它掉线，或者启动时发现它和机器人一起停过），控制台每页顶上一直提醒到你点「看过了」：请按提醒在手机微信上把那段时间达人的私聊看一遍。电脑微信重新登录、同步进来以后，机器人不用重启就会把这段时间的消息补交成草稿（一律先审）。用微信边缘（`PRM_EDGE`）时，Mac 的控制台也有同样的提醒。被挤下线时机器人能不能认出来，还在等真机确认。

**机器人停了一会儿，消息会丢吗？**
只要电脑微信一直登录着就不会。机器人重启后，会把停机期间已登记达人发来的消息补交成草稿（`PRM_CATCHUP_HOURS`，默认 12 小时以内）：晚于 5 分钟读到的草稿一律要人审，并注明「机器人没在读微信时收到」；更早的只记录，合作单上提醒运营去看。

**开专属佣金时某一款一直开不成？**
在「待审批」里对这一款的草稿点「不发」，机器人这一轮就跳过它，接着派下一款；合作单上会显示「开好 k/n」。
以后要补开没开好的款：把合作单的物流改回「运输中」，再标「已签收」，机器人只问还没开好的那几款。

**平台动作的结果「不确定」，同一款再批被拒了？**
结果不确定（平台可能已经建了）时，同一款商品不能再批，免得重复建。先到平台后台核对：
确实没有，就在那份标红的草稿上点「我已处理」，再批准新的；已经有了，就对新草稿点「不发」，再点「我已处理」。

**草稿标红，说「和另一个聊天同名」？**
微信里有两个聊天叫同一个名字（比如达人的昵称和某个群名一样），机器人没法确定发给谁，就不发。请手动在微信里发这条，或者在微信里给这位达人设一个不重复的备注，重启机器人后就能照常发。

---

## 许可证

MIT © [cancelGuMu](https://github.com/cancelGuMu)
