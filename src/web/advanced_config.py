"""Advanced switches the console can read and write (GET/POST /api/advanced).

The main settings page goes through /api/config, which only knows a fixed set
of keys. The switches of sending, the Claude relay, the PRM copilot, the OCR
fallback and the decision trace are edited here. This module is the allow-list
for them: type, range, default, a Chinese label and help text, what each one
depends on, and where it has an effect. The ranges follow
``src/config.py::_validate_config``; ``validate_updates`` checks each value
against this table and then runs ``_validate_config`` on the merged result, so a
value the bot would refuse at startup is never written.
"""

from __future__ import annotations

import re

from src.config import _parse_switch, _split_names, _validate_config

ONOFF = ("off", "on")
WCDB_ONLY = "只对 Windows（WCDB 直读）有效，macOS 后端会忽略"
OCR_BACKENDS = "macOS 的 mac_hybrid 后端和 Windows 的 WCDB 直读后端有效，mac_ui 后端不读数据库，会忽略"

# type: onoff | bool | enum | int | float | text | names | secret

GROUPS: list[dict] = [
    {
        "id": "send", "code": "SEND", "title": "发送方式",
        "desc": "中转回执、结果和审批过的消息怎么发出去。",
        "fields": [
            {"key": "SEND_TYPING_DELAY", "type": "onoff", "default": "off", "label": "像打字一样停一下",
             "help": "每条发出前按长度等 0.4–3.5 秒。"},
            {"key": "SEND_QUIET", "type": "onoff", "default": "off", "label": "安静发送", "platform": WCDB_ONLY,
             "help": "等你停下键盘鼠标再发；单独弹出的聊天窗口直接输入；发完把焦点还给你原来的窗口。"},
            {"key": "SEND_IDLE_SEC", "type": "float", "min": 0, "max": 60, "default": "3", "label": "等你停手多久", "unit": "秒",
             "help": "0 = 不等。", "requires": [{"key": "SEND_QUIET", "is": "on", "text": "安静发送打开时才用"}]},
            {"key": "SEND_IDLE_MAX_WAIT_SEC", "type": "float", "min": 0, "max": 600, "default": "30", "label": "最多等多久", "unit": "秒",
             "help": "到时间就照样发。", "requires": [{"key": "SEND_QUIET", "is": "on", "text": "安静发送打开时才用"}]},
            {"key": "SEND_KEEP_CLIPBOARD", "type": "onoff", "default": "on", "label": "发完还原剪贴板",
             "help": "机器人的文字也不进 Win+V 剪贴板历史。", "requires": [{"key": "SEND_QUIET", "is": "on", "text": "安静发送打开时才用"}]},
            {"key": "SEND_QUEUE", "type": "onoff", "default": "off", "label": "排队发送", "platform": WCDB_ONLY,
             "help": "所有消息由一个线程按顺序发，等待时不占住其他聊天。建议和安静发送、打字停顿一起开。"},
            {"key": "STICKER_PASTE", "type": "enum", "options": ["file", "image"], "default": "file",
             "labels": {"file": "粘贴文件（GIF 能动）", "image": "粘贴静态图片"}, "label": "图片怎么粘贴", "platform": WCDB_ONLY,
             "help": "发图片时，微信把粘贴的文件当附件发的话，换成静态图片。"},
        ],
    },
    {
        "id": "relay", "code": "CC", "title": "Claude 中转",
        "desc": "你在微信里私聊机器人发「cc 任务」，机器人把任务放进队列，Mac 上的 worker（tools/claude_relay/worker.py）"
                "来取走、交给 Claude Code 执行，结果再由机器人发回私聊。需要在「微信连接」里打开私聊。",
        "fields": [
            {"key": "CLAUDE_RELAY", "type": "onoff", "default": "off", "label": "打开中转",
             "help": "打开后机器人会开一个端口等 Mac 来取任务。必须同时填好下面的 token 和「谁能用」。"},
            {"key": "CLAUDE_RELAY_ONLY", "type": "onoff", "default": "off", "label": "只做中转",
             "help": "打开后，能用中转的人在私聊里发的每句话都算任务，不用打 cc；管理员的「添加好友」「查状态」这类命令照常是命令。"
                     "不管开没开，群聊和别人的私聊机器人都不回。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才能用"}]},
            {"key": "ADMIN_WXID", "type": "text", "default": "", "label": "管理员 wxid",
             "help": "你自己的微信 wxid（形如 wxid_xxx）。下面「谁能用」不填时，只有这个人能用中转。"},
            {"key": "CLAUDE_RELAY_WXIDS", "type": "names", "default": "", "label": "谁能用",
             "help": "能用 cc 指令的人的 wxid，逗号分隔；不填 = 只有管理员。只认这些人的私聊；群里的 cc 和别人发的一律不回。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
            {"key": "CLAUDE_RELAY_TOKEN", "type": "secret", "default": "", "label": "token", "generate": True,
             "help": "Mac 来取任务时要带的密码，至少 16 个字符。点「随机生成」后，把同一串填进 Mac 的 ~/.claude-relay/token。"
                     "不要发到聊天里。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
            {"key": "CLAUDE_RELAY_PORT", "type": "int", "min": 1, "max": 65535, "default": "8790", "label": "端口",
             "help": "Mac 连这台电脑的这个端口。Windows 防火墙要放行它（只放行局域网）。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
            {"key": "CLAUDE_RELAY_HOST", "type": "text", "default": "0.0.0.0", "label": "监听地址",
             "help": "0.0.0.0 = 所有网卡；只想让局域网某一块网卡能连时填它的 IP。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
            {"key": "CLAUDE_RELAY_PREFIX", "type": "text", "default": "cc", "label": "指令前缀",
             "help": "私聊里以它开头的消息才算任务，例如「cc 跑一下测试」。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
            {"key": "CLAUDE_RELAY_MAX_CHARS", "type": "int", "min": 20, "max": 5000, "default": "1500", "label": "结果最长", "unit": "字",
             "help": "结果超过就截断，完整内容在 Mac 的 ~/.claude-relay/logs/ 里（打开下面的「长结果发文件」时改成发文件）。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
            {"key": "CLAUDE_RELAY_LONG_FILE", "type": "onoff", "default": "on", "label": "长结果发文件",
             "help": "结果超过上面的字数时，把完整结果存成 txt 文件发到微信里，再发一条带开头摘要的消息。"
                     "只有 Windows（数据库直读）能发文件，其他情况照样截断。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
            {"key": "CLAUDE_RELAY_PROGRESS_MIN", "type": "int", "min": 0, "max": 120, "default": "3",
             "label": "进度提醒间隔", "unit": "分钟",
             "help": "任务做得久时，每隔这么久发一条进度（做到第几步、正在改哪个文件）；排过队的任务开始时也说一声。填 0 = 只发回执和结果。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才用"}]},
        ],
    },
    {
        "id": "prm", "code": "PRM", "title": "达人副驾驶",
        "desc": "已登记达人的私聊：机器人不回任何话，把消息静默交给 Mac 上的 worker 起草回复"
                "（只能读写 prm_workspace 里的知识库和案卷，不能运行命令），草稿进「待审批」，运营点「发送」才会发出去。"
                "要同时打开 Claude 中转和「微信连接」里的私聊；Mac 的 config.json 要配好 prm_workspace。",
        "fields": [
            # No "requires": CLAUDE_RELAY lives in another group; saving checks it (_validate_config).
            {"key": "PRM_COPILOT", "type": "onoff", "default": "off", "label": "打开达人副驾驶",
             "help": "打开后在上面登记达人的 wxid；没登记的人照旧。要先打开 Claude 中转，改完要重启机器人。"},
            {"key": "PRM_AUTO_REGISTER", "type": "onoff", "default": "off", "label": "新好友自动登记成达人",
             "platform": WCDB_ONLY,
             "help": "打开后，机器人认出的每个新加的好友（我们加她、她加我们、机器人自动通过的都算）都自动登记成正式达人，"
                     "和点「登记」一样：加好友以来她发的消息合成一份草稿，在「待审批」里等人看过再发；之后的草稿和别的达人一样"
                     "走自动发送门槛和下班时段自动回复。注意：客户、快递、同事这些不是达人的人也会被登记、收到草稿，第一份审过以后"
                     "夜里还可能被自动回复；在上面的名单里点「停用」去掉一个，停用的不会再被自动登记。管理员和能用 cc 的人从不登记。"
                     "关着 = 和以前一样，由人点「登记」。改完要重启机器人；Mac 上的 worker 不用动。",
             "requires": [{"key": "PRM_COPILOT", "is": "on", "text": "达人副驾驶打开时才用"}]},
            {"key": "PRM_TAKEOVER_MIN", "type": "int", "min": 5, "max": 1440, "default": "120",
             "label": "人工接管多久", "unit": "分钟",
             "help": "运营用这个微信号亲手回了达人（手机或电脑上），或在合作单里点「人工接管」，这段时间里达人的消息只记录、"
                     "不起草；时间到了自动回到副驾驶，也可以随时点「恢复副驾驶」。"},
            {"key": "PRM_HOLDING_MIN", "type": "int", "min": 0, "max": 120, "default": "10",
             "label": "草稿等多久先回一句", "unit": "分钟",
             "help": "草稿在「待审批」里放了这么久还没人处理，先自动回达人一句下面的话（达人每发一轮消息最多一次），草稿继续等。0 = 不回。"},
            {"key": "PRM_HOLDING_TEXT", "type": "text", "default": "收到，我确认一下马上回您", "label": "先回的那句话",
             "help": "1–60 个字。"},
            {"key": "PRM_NIGHT_AUTO_HOURS", "type": "text", "default": "", "label": "下班时段自动回复",
             "help": "填运营下班的时段，例如 18:00-09:00（可以跨过半夜，按这台电脑的时间）；空着 = 关，和以前一样按门槛先审。"
                     "这个时段里 Mac 写好的回复要满足夜间的全部安全条件才直接发给达人：不是投诉；佣金不超过 25%，"
                     "不提坑位费、保底、返现、定金、专属价这类；占位符、kb/ 出处、运单号、文件都对得上；"
                     "模型要求注意的只能是一句不带任何数字的过渡话（合作单上留一条提醒，点「已处理」才消失）。"
                     "平台动作、补读的消息、新好友、撤回、人工接管的照旧等运营；这台电脑的时间和微信对不上时一律不自动发。"
                     "改完要重启机器人；Mac 上的 worker 也要重启一次，草稿任务里才会写明现在是下班时段。"},
            {"key": "PRM_NIGHT_HOLDING_TEXT", "type": "text", "default": "收到～您说的我都记下啦，明早第一时间回复您哈～",
             "label": "下班时段先回的那句话",
             "help": "1–60 个字。下班时段里草稿等了「草稿等多久先回一句」那么久还没人处理，先回达人这一句（上班时段还是上面那句）。"},
            {"key": "PRM_OPENING_AUTO_MIN", "type": "int", "min": 0, "max": 1440, "default": "0",
             "label": "开场白没人处理多久自动发", "unit": "分钟",
             "help": "给新好友的第一句话（她通过了我们的申请、没说话时的开场白；她加我们时只说了一句「我是…」的第一份回复；"
                     "合作单里点「写开场白」写的）在「待审批」里放了这么久，没人点「发送」或「不发」，就照原样发给她；"
                     "下班时段或者自动模式开着时，写好就发。发之前照样检查：佣金超过 25%、坑位费 / 返现这类、说到寄样包邮、"
                     "模型要求注意、文件没收全、她撤回过消息的，照旧等你，卡片上写着为什么。老板拉的群里的开场白一律等你。"
                     "0 = 关，和以前一样开场白一律等你。改完要重启机器人；Mac 上的 worker 也要重启一次，起草时才知道可能自动发。",
             "requires": [{"key": "PRM_COPILOT", "is": "on", "text": "达人副驾驶打开时才用"}]},
            {"key": "PRM_REPLY_PARTS", "type": "int", "min": 1, "max": 5, "default": "3",
             "label": "一条回复最多分几条发", "unit": "条",
             "help": "像真人发微信那样，一条回复分成几条短消息发：草稿里空一行就是分开发，两条之间停一两秒（按字数，像在打字）；"
                     "Mac 写草稿时会被告知最多能分几条。超出的段落并进最后一条。1 = 和以前一样整段一条发。改完要重启机器人。",
             "requires": [{"key": "PRM_COPILOT", "is": "on", "text": "达人副驾驶打开时才用"}]},
            {"key": "PRM_CATCHUP_HOURS", "type": "int", "min": 0, "max": 72, "default": "12",
             "label": "没在读时收到的消息补多久", "unit": "小时",
             "help": "机器人停着（更新 EXE、重启）或数据库重新打开的那段时间里达人发来的消息，重新读到时照样交给副驾驶："
                     "这么多小时以内的照常起草，但一律先审，合作单上会注明是没在读时收到的；更早的只记下，合作单提示有几条没起草、"
                     "请人看一下。那段时间里运营在手机上亲手回的话也会记下。0 = 和以前一样，晚到 5 分钟以上的都当历史不处理。"
                     "改完要重启机器人。"},
            {"key": "PRM_RETENTION_DAYS", "type": "int", "min": 1, "max": 365, "default": "90",
             "label": "处理完多久清除收件信息", "unit": "天",
             "help": "合作单签收满这么多天，或者这么多天没有任何动静（没有消息、没有改动，比如结案、审批没通过），"
                     "地址只留省市、手机号和证件号只留打码的，聊天记录、草稿、中转记录、Mac 上的案卷都一起清。"},
            # 加百应达人微信 (src/prm/friend_adds.py, CODEBASE_REFERENCE 功能 R)
            {"key": "PRM_FRIEND_ADD", "type": "onoff", "default": "off", "label": "加百应达人微信", "platform": WCDB_ONLY,
             "help": "百应邀约来的达人留了微信号 / 手机号：在「合作单」页手动登记，到「待审批 › 加微信」确认一次，机器人就用好友管理"
                     "发一次好友申请（不会自动重试；微信提示操作频繁时自动同意好友也暂停，管理员发「自动同意好友 开」才继续）。"
                     "她通过以后点「是她，登记并关联」。关着时什么都不记、不加。改完要重启机器人。"},
            {"key": "PRM_FRIEND_ADD_DAILY_MAX", "type": "int", "min": 1, "max": 30, "default": "10",
             "label": "每天最多加几个", "unit": "个",
             "help": "控制台和管理员「加达人 编号」加起来，每调一次加好友算一个（没发出去的也算）。管理员自己的「添加好友」不算。",
             "requires": [{"key": "PRM_FRIEND_ADD", "is": "on", "text": "加百应达人微信打开时才用"}]},
            {"key": "PRM_FRIEND_ADD_GAP_SEC", "type": "int", "min": 30, "max": 3600, "default": "120",
             "label": "两次加好友至少隔多久", "unit": "秒",
             "requires": [{"key": "PRM_FRIEND_ADD", "is": "on", "text": "加百应达人微信打开时才用"}]},
            {"key": "PRM_FRIEND_ADD_GREETING", "type": "text", "default": "您好，我是{品牌}，百应上联系您的～",
             "label": "默认附言",
             "help": "{品牌} 是下面的品牌名；填好以后最多 30 个字，加之前可以改。默认不带她的昵称（只说「您好」，2026-11-03），"
                     "每个人的附言一样，她通过以后靠下面的备注认出是谁；写上 {昵称}（她百应昵称的前 8 个字，昵称里有号码时不用）"
                     "附言就各不相同，也能按附言认人。还没发出去的旧附言（「您好〈昵称〉，…」）重启时换成这里的。",
             "requires": [{"key": "PRM_FRIEND_ADD", "is": "on", "text": "加百应达人微信打开时才用"}]},
            {"key": "PRM_FRIEND_ADD_BRAND", "type": "text", "default": "", "label": "附言里的品牌名",
             "help": "只存在这台电脑的 .env 里，不进代码仓库；不填时附言里的「我是{品牌}，」整段不写。最多 20 个字。",
             "requires": [{"key": "PRM_FRIEND_ADD", "is": "on", "text": "加百应达人微信打开时才用"}]},
            {"key": "PRM_FRIEND_ADD_REMARK", "type": "text", "default": "抖音-{昵称}", "empty_ok": True,
             "placeholder": "空着：不设备注", "label": "加好友时给她的备注",
             "help": "她在我们微信里显示的名字（微信的「备注」）。{昵称} 是她的百应昵称（没有昵称、昵称里有号码时用抖音号），"
                     "填好以后最多 16 个字，和别的记录一样时后面加「-抖音号后 4 位」；{昵称} 以外最多 10 个字。机器人在「申请添加朋友」"
                     "窗口里认出「备注」栏才填，认不出就不填、申请照发。她通过以后显示的就是这个名字，认人更准，发消息也不会和别人同名。"
                     "清空保存 = 不设备注。",
             "requires": [{"key": "PRM_FRIEND_ADD", "is": "on", "text": "加百应达人微信打开时才用"}]},
            # 三方群 (src/prm/three_party.py, 2026-10-30)
            {"key": "PRM_GROUP_WITH", "type": "text", "default": "", "empty_ok": True, "platform": WCDB_ONLY,
             "placeholder": "空着：合作单上没有「拉三方群」", "label": "拉三方群时一起拉进群的人",
             "help": "合作单上点「拉三方群」，机器人在电脑微信的「发起群聊」里搜她和这里的人，各自恰好搜到一个才勾上，读一遍已选的人"
                     "没错才点「完成」。写他们在这个微信号通讯录里的名字（设了备注就写备注），多个用逗号隔开，最多 3 个，比如老板。"
                     "只存在这台电脑的 .env 里，不进代码仓库。机器人只建群：不在群里说话，也不改群名。改完要重启机器人。",
             "requires": [{"key": "PRM_COPILOT", "is": "on", "text": "达人副驾驶打开时才用"}]},
            {"key": "PRM_GROUP_NAME", "type": "text", "default": "{品牌}×{昵称}", "label": "建议的群名",
             "help": "群建好以后，合作单上提示你在手机上把群名改成这个（旁边有「复制群名」）。{品牌} 是「附言里的品牌名」，"
                     "{昵称} 是她在名单里的称呼；{昵称} 要有一次，其余最多 12 个字。",
             "requires": [{"key": "PRM_COPILOT", "is": "on", "text": "达人副驾驶打开时才用"}]},
            # 老板拉的群 (src/prm/boss_groups.py, 2026-10-31)
            {"key": "PRM_BOSS_GROUPS", "type": "onoff", "default": "off", "label": "接手老板拉的群", "platform": WCDB_ONLY,
             "help": "老板（上面「拉三方群时一起拉进群的人」里的人）拉了一个有他、达人和这个号的小群，机器人就接手这位达人："
                     "群里除了我们和老板只有一个人时，自动把她登记成达人、写一份在群里打招呼的开场白（一律先审）；有好几个人时，"
                     "在「合作单」页顶上问你哪个是她。之后她在群里说的话照样起草，草稿发在群里（老板也看得到），审批、自动发送门槛、"
                     "下班时段、自动模式都和私聊一样，群里不回「稍等」；老板和别人在群里说的话只给草稿当背景，不回。别的群照旧一律不回。"
                     "只认打开以后新出现的群。改完要重启机器人。",
             "requires": [{"key": "PRM_COPILOT", "is": "on", "text": "达人副驾驶打开时才用"}]},
            {"key": "PRM_BOSS_CALL", "type": "text", "default": "老板", "label": "跟 Mac 怎么称呼老板",
             "help": "老板的名字（「拉三方群时一起拉进群的人」）不出这台电脑：交给 Mac 起草时，群名里、群里的话里他的名字都换成这个"
                     "称呼，草稿里也只这么叫他。1–10 个字，比如「老板」「大老板」。改完要重启机器人。",
             "requires": [{"key": "PRM_BOSS_GROUPS", "is": "on", "text": "接手老板拉的群打开时才用"}]},
            {"key": "PRM_BOSS_GROUP_MAX", "type": "int", "min": 3, "max": 20, "default": "6",
             "label": "老板拉的群最多几个人", "unit": "人",
             "help": "人数超过这个的群当成老板平时的群，不接手。",
             "requires": [{"key": "PRM_BOSS_GROUPS", "is": "on", "text": "接手老板拉的群打开时才用"}]},
        ],
    },
    {
        "id": "ocr", "code": "OCR", "title": "读取备选（OCR）",
        "desc": "数据库读不了的时候（WeFlow 没启动、密钥失效、DLL 打不开、微信升级后读不出来），改成截取微信窗口、用系统自带的文字识别"
                "（macOS 用苹果的，Windows 用系统自带的 Windows OCR）读当前打开的聊天。数据库恢复后自动切回来。" + OCR_BACKENDS + "。",
        "fields": [
            {"key": "OCR_FALLBACK", "type": "onoff", "default": "off", "label": "数据库读不了时用 OCR",
             "platform": OCR_BACKENDS, "platform_label": "macOS / Windows", "platform_backends": ["mac_hybrid", "wcdb"],
             "help": "只读当前打开的那个聊天，而且只读屏幕上显示的那一段；认不出是谁说的话；"
                     "图片、语音、表情读不出来。微信窗口不能最小化。macOS：要给运行机器人的程序「屏幕录制」权限，第一次使用会用 swiftc 编译识别程序"
                     "（需要 Xcode 命令行工具）。Windows：要装中文 OCR 语言包（见 tools/ocr_check.py 的说明）。改完要重启机器人。"},
            {"key": "OCR_FALLBACK_AFTER", "type": "int", "min": 1, "max": 20, "default": "3", "label": "连续读失败几次后切换", "unit": "次",
             "help": "数据库每秒读一次，连续失败这么多次才改用 OCR，避免偶尔一次读失败就切换。",
             "requires": [{"key": "OCR_FALLBACK", "is": "on", "text": "OCR 备选打开时才用"}]},
            {"key": "OCR_POLL_SEC", "type": "float", "min": 1, "max": 60, "default": "4", "label": "多久看一次屏幕", "unit": "秒",
             "help": "每次识别约 0.8 秒。间隔越短回得越快，也越占 CPU。",
             "requires": [{"key": "OCR_FALLBACK", "is": "on", "text": "OCR 备选打开时才用"}]},
        ],
    },
    {
        "id": "trace", "code": "LOG", "title": "决策记录",
        "desc": "每条消息回了没有、为什么，都记一行（只记录，不改变机器人的行为）。用 python tools/trace_report.py 查看。",
        "fields": [
            {"key": "DECISION_TRACE_ENABLED", "type": "bool", "default": "true", "label": "记录决策",
             "help": "关掉以后就没有新的记录了。"},
            {"key": "DECISION_TRACE_CONTENT_CHARS", "type": "int", "min": 0, "max": 1000, "default": "120", "label": "每条记多少字原文", "unit": "字",
             "help": "0 = 不记消息原文。", "requires": [{"key": "DECISION_TRACE_ENABLED", "is": "true", "text": "记录决策打开时才用"}]},
        ],
    },
]

FIELDS: dict[str, dict] = {f["key"]: f for g in GROUPS for f in g["fields"]}
SECRET_KEYS = frozenset(k for k, f in FIELDS.items() if f["type"] == "secret")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


def normalize(key: str, raw: str | None) -> str:
    """The value as the bot reads it, written the canonical way.  An empty value is the default, except for a field
    with ``empty_ok`` that is set but empty (``PRM_FRIEND_ADD_REMARK=``: no remark); unset (``None``) is the default."""
    field = FIELDS[key]
    kind = field["type"]
    value = "" if raw is None else str(raw).strip()
    if value == "" and not (field.get("empty_ok") and raw is not None):
        value = field["default"]
    if kind == "onoff":
        return _parse_switch(value)
    if kind == "bool":
        return "true" if value.lower() == "true" else "false"
    if kind == "enum":
        return value.lower()
    return value


def check_value(key: str, value) -> str:
    """Why ``value`` is not acceptable for ``key`` ("" when it is fine)."""
    field = FIELDS.get(key)
    if field is None:
        return "不是可以在这里修改的设置"
    if not isinstance(value, (str, int, float, bool)) or isinstance(value, bool) and field["type"] not in ("bool", "onoff"):
        return "值的类型不对"
    text = str(value).strip()
    kind = field["type"]
    if _CONTROL.search(str(value)) or len(str(value).splitlines()) > 1:
        return "不能包含换行或控制字符"
    if re.search(r"(^|\s)#", text):
        return "不能包含「 #」（.env 会把后面当成注释）"
    if kind == "onoff":
        return "" if _parse_switch(text) in ONOFF else "只能是 on 或 off"
    if kind == "bool":
        return "" if text.lower() in ("true", "false") else "只能是 true 或 false"
    if kind == "enum":
        return "" if text.lower() in field["options"] else "只能是 " + " / ".join(field["options"])
    if kind in ("int", "float"):
        if not (_INT if kind == "int" else _FLOAT).fullmatch(text):
            return "要填整数" if kind == "int" else "要填数字"
        number = int(text) if kind == "int" else float(text)
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and number < lo:
            return f"不能小于 {lo}"
        if hi is not None and (number >= hi if field.get("max_exclusive") else number > hi):
            return f"要小于 {hi}" if field.get("max_exclusive") else f"不能大于 {hi}"
        return ""
    return ""


def _kwargs(values: dict[str, str]) -> dict:
    """The advanced values parsed the way load_config() parses them."""
    def num(key, cast):
        try:
            return cast(values[key])
        except (TypeError, ValueError):
            return cast(FIELDS[key]["default"])

    out: dict = {}
    for key, field in FIELDS.items():
        name = key.lower()
        kind = field["type"]
        value = values.get(key, field["default"])
        if kind == "int":
            out[name] = num(key, int)
        elif kind == "float":
            out[name] = num(key, float)
        elif kind == "bool":
            out[name] = str(value).strip().lower() == "true"
        elif kind == "onoff":
            out[name] = _parse_switch(value)
        elif kind == "enum":
            out[name] = str(value).strip().lower() or field["default"]
        elif kind == "names":
            out[name] = _split_names(value)
        else:
            out[name] = str(value).strip()
    return out


def validate_updates(updates: dict, current: dict[str, str]) -> dict[str, str]:
    """Errors per key for a POST; empty when the merged values are all valid.

    ``current`` is the effective raw env (shared .env plus .env.local).
    """
    errors: dict[str, str] = {}
    for key, value in updates.items():
        message = check_value(key, value)
        if message:
            errors[key] = message
    if errors:
        return errors
    merged = {key: normalize(key, current.get(key)) for key in FIELDS}
    for key, value in updates.items():
        merged[key] = normalize(key, value)
    try:
        _validate_config(_kwargs(merged))
    except RuntimeError as exc:
        lines = [ln.strip(" -") for ln in str(exc).splitlines()[1:] if ln.strip()]
        for line in lines:
            # the longest key the line starts with: PRM_FRIEND_ADD_BRAND's message is not PRM_FRIEND_ADD's
            hit = next((k for k in sorted(FIELDS, key=len, reverse=True) if line.startswith(k)), None)
            if line.startswith("CLAUDE_RELAY=on needs "):
                # two messages start alike: each belongs to the field it names
                hit = "CLAUDE_RELAY_TOKEN" if "CLAUDE_RELAY_TOKEN" in line else "CLAUDE_RELAY_WXIDS"
            errors[hit or "_"] = line
    return errors


def describe(current: dict[str, str], mask) -> list[dict]:
    """GROUPS with each field's current value (secrets masked by ``mask``)."""
    out = []
    for group in GROUPS:
        fields = []
        for field in group["fields"]:
            raw = current.get(field["key"])
            value = mask(raw or "") if field["type"] == "secret" else normalize(field["key"], raw)
            fields.append({**field, "value": value, "set": bool(str(raw or "").strip())})
        out.append({**group, "fields": fields})
    return out
