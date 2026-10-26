"""Advanced switches the console can read and write (GET/POST /api/advanced).

The main settings page goes through /api/config, which only knows a fixed set
of keys. The newer switches (structured pipeline, memory v2, sending, stickers,
image vision…) used to be editable in .env only. This module is the allow-list
for them: type, range, default, a Chinese label and help text, what each one
depends on, and where it has an effect. The ranges follow
``src/config.py::_validate_config``; ``validate_updates`` checks each value
against this table and then runs ``_validate_config`` on the merged result, so a
value the bot would refuse at startup is never written.
"""

from __future__ import annotations

import json
import re

from src.config import (
    _parse_json_object,
    _parse_memory_mode,
    _parse_pipeline_mode,
    _split_names,
    _validate_config,
    parse_wechat_emoji_codes,
)

ONOFF = ("off", "on")
V2_ON = {"key": "PIPELINE_V2", "not": "off", "text": "要先把 PIPELINE_V2 打开"}
WCDB_ONLY = "只对 Windows（WCDB 直读）有效，macOS 后端会忽略"
OCR_BACKENDS = "macOS 的 mac_hybrid 后端和 Windows 的 WCDB 直读后端有效，mac_ui 后端不读数据库，会忽略"

# type: onoff | bool | enum | multi | int | float | text | json | names | path | secret
# For "multi" and "json" an empty value is a real choice (no tasks / nothing
# extra), so only a missing key falls back to the default.
NOT_LOCAL = {"key": "MODEL_ROUTE", "not": "cloud", "text": "MODEL_ROUTE 是 local 或 split 时才用"}

GROUPS: list[dict] = [
    {
        "id": "local", "code": "LOCAL", "title": "本地模型",
        "desc": "把全部或一部分任务交给自己电脑上的模型（Ollama、LM Studio、llama.cpp、vLLM 等兼容 OpenAI 接口的服务）。上线前先运行 python tools/model_check.py 检查一遍。",
        "fields": [
            {"key": "MODEL_ROUTE", "type": "enum", "options": ["cloud", "local", "split"], "default": "cloud",
             "labels": {"cloud": "全部用云端（原来的做法）", "local": "全部用本地", "split": "分工"},
             "label": "模型分工",
             "help": "local：不再需要云端 API Key；split：下面勾选的任务用本地模型，其余用 AI 服务里的云端模型。"},
            {"key": "LOCAL_TASKS", "type": "multi", "options": ["chat", "proactive", "memory", "decide"], "default": "memory",
             "labels": {"chat": "被叫到时的回复", "proactive": "主动发言", "memory": "记忆整理",
                        "decide": "要不要说话的判断"},
             "label": "交给本地模型的任务",
             "help": "记忆整理便宜又不出本机；主动发言调用最频繁。「要不要说话的判断」：拿不准的消息先由本地模型判断，"
                     "决定要回时才让云端写，回复质量不变、云端调用少一半左右；需要回复管线选「两步」。",
             "requires": [{"key": "MODEL_ROUTE", "is": "split", "text": "只在「分工」时起作用"}]},
            {"key": "CLOUD_FALLBACK", "type": "enum", "options": ["off", "local"], "default": "off",
             "labels": {"off": "关", "local": "改用本地模型"},
             "label": "云端出错时",
             "help": "云端额度用完、被限流或连不上时，聊天回复改由本地模型回答，之后 3 分钟内不再试云端。需要填好本地模型。",
             "requires": [{"key": "MODEL_ROUTE", "not": "local", "text": "全部用本地时不需要"}]},
            {"key": "LOCAL_BASE_URL", "type": "text", "default": "http://127.0.0.1:11434/v1", "label": "本地服务地址",
             "help": "Ollama 默认 http://127.0.0.1:11434/v1，LM Studio 是 http://127.0.0.1:1234/v1；也可以是局域网里另一台电脑。",
             "requires": [NOT_LOCAL]},
            {"key": "LOCAL_MODEL", "type": "text", "default": "", "label": "模型名",
             "help": "本地服务里的模型名，例如 qwen3.5:4b。用本地模型时必填。", "requires": [NOT_LOCAL]},
            {"key": "LOCAL_API_KEY", "type": "secret", "default": "", "label": "本地服务密钥",
             "help": "大多数本地服务不检查，可以不填。", "requires": [NOT_LOCAL]},
            {"key": "LOCAL_TIMEOUT_SEC", "type": "float", "min": 5, "max": 600, "default": "90", "label": "最多等多久", "unit": "秒",
             "requires": [NOT_LOCAL]},
            {"key": "LOCAL_MAX_TOKENS", "type": "int", "min": 256, "max": 32768, "default": "2048", "label": "回答最多多少 token",
             "requires": [NOT_LOCAL]},
            {"key": "LOCAL_EXTRA_BODY", "type": "json", "default": '{"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": false}}',
             "label": "请求里附带的参数",
             "help": "一个 JSON 对象，每次请求都带上，用来关掉「思考」。默认值同时带了 Ollama 认的 reasoning_effort 和 llama.cpp / vLLM 认的 chat_template_kwargs；Qwen 不思考时加上 \"temperature\": 0.7, \"top_p\": 0.8 回复更好；留空表示什么都不带。",
             "requires": [NOT_LOCAL]},
            {"key": "LOCAL_PROMPT", "type": "enum", "options": ["standard", "compact"], "default": "standard",
             "labels": {"standard": "和云端一样", "compact": "小模型精简版"},
             "label": "本地模型的提示词",
             "help": "精简版：去掉空的记忆、搜索段落，把「让你做的事就直接做到、不懂别编」放在小模型看得到的位置。4B 实测有硬伤的回复更少。",
             "requires": [NOT_LOCAL]},
        ],
    },
    {
        "id": "pipeline", "code": "PIPE", "title": "回复管线",
        "desc": "让模型按固定格式回答：先判断这句话是不是对它说的、要不要开口，再写回复。先在一个群里试，再推广。",
        "fields": [
            {"key": "PIPELINE_V2", "type": "enum", "options": ["off", "single", "two_step"], "default": "off",
             "labels": {"off": "关（原来的做法）", "single": "一次调用", "two_step": "两步（先问要不要说）"},
             "label": "结构化管线",
             "help": "single：一次调用同时给出「要不要说」和回复；two_step：拿不准的消息先问一次要不要说，再写回复。"},
            {"key": "PIPELINE_V2_GROUPS", "type": "names", "default": "*", "label": "在哪些群用",
             "help": "* 表示所有群；也可以写群名或群 ID（逗号分隔，部分匹配）。加上 private 表示私聊也用。", "requires": [V2_ON]},
            {"key": "BOT_ALIASES", "type": "names", "default": "", "label": "机器人的别名",
             "help": "群友这样叫也算在叫机器人（逗号分隔）。微信昵称和人设名字本来就算。", "requires": [V2_ON]},
            {"key": "FOLLOWUP_MODE", "type": "enum", "options": ["heuristic", "declared"], "default": "heuristic",
             "labels": {"heuristic": "本地规则判断", "declared": "模型自己说要等谁"},
             "label": "回复后的跟聊",
             "help": "declared 时，模型在回复里说明接下来只等提问的人、等任何人还是谁都不等。", "requires": [V2_ON]},
            {"key": "PIPELINE_REWRITE_LONG", "type": "onoff", "default": "off", "label": "太长就重写",
             "help": "回复超过人设的字数上限时，让模型重写一次（多一次调用），而不是直接截断。", "requires": [V2_ON]},
            {"key": "PERSONA_EXAMPLES", "type": "int", "min": 0, "max": 8, "default": "4", "label": "示例对话条数",
             "help": "每次回复给模型看几段最接近的示例对话，学说话方式。0 = 不用。示例文件改了马上生效。", "requires": [V2_ON]},
            {"key": "PERSONA_EXAMPLES_DIR", "type": "path", "default": "data/examples", "label": "示例对话目录",
             "help": "放 <人设名>.txt 示例文件的文件夹。", "requires": [V2_ON]},
            {"key": "PIPELINE_REPAIR", "type": "bool", "default": "true", "label": "答案格式不对时补救一次",
             "help": "排错用。模型给的格式不能用时，多调用一次请它改正。", "requires": [V2_ON]},
            {"key": "PIPELINE_JSON_MODE", "type": "bool", "default": "true", "label": "使用 JSON 模式",
             "help": "排错用。服务商支持时用它的 JSON 模式，不支持会自动退回。", "requires": [V2_ON]},
            {"key": "CLAUDE_CHAT_MODEL", "type": "text", "default": "claude-haiku-4-5-20251001", "label": "Claude 聊天模型",
             "help": "AI 服务选 Claude 时，群聊回复用的模型。", "requires": [{"key": "AI_BACKEND", "is": "claude", "text": "只在 AI 服务选 Claude 时起作用"}]},
        ],
    },
    {
        "id": "proactive", "code": "GATE", "title": "主动发言的判断方式",
        "desc": "主动发言打开时，用什么方式决定要不要插话。",
        "fields": [
            {"key": "PROACTIVE_GATE_MODE", "type": "enum", "options": ["dice", "score"], "default": "dice",
             "labels": {"dice": "掷骰子（按热度概率）", "score": "打分 + 每小时配额"},
             "label": "判断方式",
             "help": "score：没人回答的问题、求助类的话分数高，刚说过话、单纯的表情回应分数低；再加每个群每小时的配额。建议配合 PIPELINE_V2。",
             "requires": [{"key": "PROACTIVE_ENABLED", "is": "true", "text": "要先打开功能模块里的「主动发言」"}]},
            {"key": "PROACTIVE_HOURLY_BUDGET", "type": "int", "min": 0, "max": 120, "default": "6", "label": "每群每小时最多",
             "help": "0 = 不限。", "unit": "次",
             "requires": [{"key": "PROACTIVE_GATE_MODE", "is": "score", "text": "只在打分方式下起作用"}]},
            {"key": "PROACTIVE_SCORE_THRESHOLD", "type": "float", "min": 0.05, "max": 0.95, "default": "0.35", "label": "分数门槛",
             "help": "一条消息的分数够这个数，才去问模型要不要插话。",
             "requires": [{"key": "PROACTIVE_GATE_MODE", "is": "score", "text": "只在打分方式下起作用"}]},
        ],
    },
    {
        "id": "memory", "code": "MEM", "title": "记忆",
        "desc": "后台按时间窗读聊天，请模型整理出有原话为证的事实（昵称、喜好、计划、梗）和摘要，回复时带上和说话人有关的那些。每个窗口一次便宜的 JSON 调用。",
        "fields": [
            {"key": "MEMORY_V2", "type": "onoff", "default": "off", "label": "新版记忆"},
            {"key": "MEMORY_V2_GROUPS", "type": "names", "default": "*", "label": "在哪些群用",
             "help": "* 表示所有群；也可以写群名或群 ID（逗号分隔，部分匹配），加上 private 表示私聊。",
             "requires": [{"key": "MEMORY_V2", "is": "on", "text": "要先打开新版记忆"}]},
            {"key": "MEMORY_WINDOW_MESSAGES", "type": "int", "min": 5, "max": 500, "default": "40", "label": "攒够多少条整理一次", "unit": "条",
             "requires": [{"key": "MEMORY_V2", "is": "on", "text": "要先打开新版记忆"}]},
            {"key": "MEMORY_IDLE_MINUTES", "type": "int", "min": 1, "max": 1440, "default": "20", "label": "安静多久也整理", "unit": "分钟",
             "help": "至少有 8 条没整理的消息时。", "requires": [{"key": "MEMORY_V2", "is": "on", "text": "要先打开新版记忆"}]},
            {"key": "MEMORY_MIN_CONFIDENCE", "type": "float", "min": 0, "max": 1, "default": "0.6", "label": "事实的最低可信度",
             "help": "低于这个可信度的事实不会放进提示词。", "requires": [{"key": "MEMORY_V2", "is": "on", "text": "要先打开新版记忆"}]},
            {"key": "MEMORY_PROMPT_FACTS", "type": "int", "min": 1, "max": 40, "default": "12", "label": "一次最多带几条事实", "unit": "条",
             "requires": [{"key": "MEMORY_V2", "is": "on", "text": "要先打开新版记忆"}]},
            {"key": "MEMORY_EMBEDDINGS", "type": "onoff", "default": "off", "label": "按意思找记忆",
             "help": "「我不能吃虾」也能找到「对海鲜过敏」。需要一个兼容 OpenAI 的向量接口。",
             "privacy": "打开后，记忆里的事实和点名机器人的消息会发给下面填的向量接口（第三方服务）。",
             "requires": [{"key": "MEMORY_V2", "is": "on", "text": "要先打开新版记忆"}]},
            {"key": "EMBEDDING_BASE_URL", "type": "text", "default": "", "label": "向量接口地址",
             "help": "例如 https://api.siliconflow.cn/v1；本机服务不需要密钥时只填这个和模型。",
             "requires": [{"key": "MEMORY_EMBEDDINGS", "is": "on", "text": "打开「按意思找记忆」才用"}]},
            {"key": "EMBEDDING_API_KEY", "type": "secret", "default": "", "label": "向量接口密钥",
             "requires": [{"key": "MEMORY_EMBEDDINGS", "is": "on", "text": "打开「按意思找记忆」才用"}]},
            {"key": "EMBEDDING_MODEL", "type": "text", "default": "", "label": "向量模型",
             "help": "例如 BAAI/bge-m3、text-embedding-3-small。",
             "requires": [{"key": "MEMORY_EMBEDDINGS", "is": "on", "text": "打开「按意思找记忆」才用"}]},
            {"key": "MEMORY_EMBEDDING_MIN_SIM", "type": "float", "min": 0, "max": 1, "max_exclusive": True, "default": "0.55",
             "label": "相似度门槛", "help": "不相关的事实混进来就调高，相关的找不到就调低。",
             "requires": [{"key": "MEMORY_EMBEDDINGS", "is": "on", "text": "打开「按意思找记忆」才用"}]},
        ],
    },
    {
        "id": "send", "code": "SEND", "title": "发送方式",
        "desc": "回复怎么发出去。",
        "fields": [
            {"key": "SEND_BUBBLES", "type": "onoff", "default": "off", "label": "分成两个气泡",
             "help": "管线给出两句话时分两条发，而不是拼成一行。", "requires": [V2_ON]},
            {"key": "SEND_TYPING_DELAY", "type": "onoff", "default": "off", "label": "像打字一样停一下",
             "help": "每条发出前按长度等 0.4–3.5 秒。"},
            {"key": "SEND_QUIET", "type": "onoff", "default": "off", "label": "安静发送", "platform": WCDB_ONLY,
             "help": "等你停下键盘鼠标再发；单独弹出的群窗口直接输入；发完把焦点还给你原来的窗口。"},
            {"key": "SEND_IDLE_SEC", "type": "float", "min": 0, "max": 60, "default": "3", "label": "等你停手多久", "unit": "秒",
             "help": "0 = 不等。", "requires": [{"key": "SEND_QUIET", "is": "on", "text": "安静发送打开时才用"}]},
            {"key": "SEND_IDLE_MAX_WAIT_SEC", "type": "float", "min": 0, "max": 600, "default": "30", "label": "最多等多久", "unit": "秒",
             "help": "到时间就照样发。", "requires": [{"key": "SEND_QUIET", "is": "on", "text": "安静发送打开时才用"}]},
            {"key": "SEND_KEEP_CLIPBOARD", "type": "onoff", "default": "on", "label": "发完还原剪贴板",
             "help": "机器人的文字也不进 Win+V 剪贴板历史。", "requires": [{"key": "SEND_QUIET", "is": "on", "text": "安静发送打开时才用"}]},
            {"key": "SEND_QUEUE", "type": "onoff", "default": "off", "label": "排队发送", "platform": WCDB_ONLY,
             "help": "所有回复由一个线程按顺序发，等待时不占住其他群。建议和安静发送、打字停顿一起开。"},
        ],
    },
    {
        "id": "emoji", "code": "EMO", "title": "表情",
        "desc": "让机器人偶尔用微信自带的小黄脸，或者配一张表情包图片。",
        "fields": [
            {"key": "WECHAT_EMOJI", "type": "onoff", "default": "off", "label": "微信小黄脸",
             "help": "只保留下面列表里的代码，微信不认识的代码会显示成文字。", "requires": [V2_ON]},
            {"key": "WECHAT_EMOJI_CODES", "type": "text", "default": "", "label": "可用的表情代码",
             "help": "例如 捂脸,旺柴,吃瓜。留空用内置列表。用 python tools/sticker_check.py --emoji 看你的微信认哪些。",
             "requires": [{"key": "WECHAT_EMOJI", "is": "on", "text": "微信小黄脸打开时才用"}]},
            {"key": "SEND_STICKERS", "type": "onoff", "default": "off", "label": "表情包图片", "platform": WCDB_ONLY,
             "help": "从表情包文件夹里挑一张配图。先用 python tools/sticker_check.py --init 建好文件夹。", "requires": [V2_ON]},
            {"key": "STICKER_DIR", "type": "path", "default": "data/stickers", "label": "表情包文件夹",
             "requires": [{"key": "SEND_STICKERS", "is": "on", "text": "表情包图片打开时才用"}]},
            {"key": "STICKER_MIN_INTERVAL_SEC", "type": "int", "min": 0, "max": 86400, "default": "600", "label": "同一个群至少间隔", "unit": "秒",
             "requires": [{"key": "SEND_STICKERS", "is": "on", "text": "表情包图片打开时才用"}]},
            {"key": "STICKER_PASTE", "type": "enum", "options": ["file", "image"], "default": "file",
             "labels": {"file": "粘贴文件（GIF 能动）", "image": "粘贴静态图片"}, "label": "怎么粘贴",
             "help": "微信把粘贴的文件当附件发时，换成静态图片。",
             "requires": [{"key": "SEND_STICKERS", "is": "on", "text": "表情包图片打开时才用"}]},
        ],
    },
    {
        "id": "image", "code": "IMG", "title": "图片识别",
        "desc": "群友问图片时看图回答；机器人刚回复某人后，那个人发的第一张图或表情也会判断是不是在接话。需要能看图的模型（例如 OpenAI）。",
        "fields": [
            {"key": "IMAGE_VISION_ENABLED", "type": "bool", "default": "true", "label": "看图回答"},
            {"key": "IMAGE_VISION_LOOKBACK_SEC", "type": "int", "min": 60, "max": 7200, "default": "900", "label": "往回找多久的图", "unit": "秒",
             "requires": [{"key": "IMAGE_VISION_ENABLED", "is": "true", "text": "看图回答打开时才用"}]},
            {"key": "IMAGE_VISION_MAX_BYTES", "type": "int", "min": 1024, "max": 20971520, "default": "5242880", "label": "图片最大", "unit": "字节",
             "help": "默认 5242880（5 MB）。", "requires": [{"key": "IMAGE_VISION_ENABLED", "is": "true", "text": "看图回答打开时才用"}]},
            {"key": "IMAGE_VISION_REPLY_MAX_CHARS", "type": "int", "min": 20, "max": 200, "default": "80", "label": "看图回复最长", "unit": "字",
             "requires": [{"key": "IMAGE_VISION_ENABLED", "is": "true", "text": "看图回答打开时才用"}]},
            {"key": "IMAGE_FOLLOWUP_ENABLED", "type": "bool", "default": "true", "label": "接话的图片也看"},
            {"key": "IMAGE_FOLLOWUP_TTL_SEC", "type": "int", "min": 15, "max": 300, "default": "90", "label": "回复后多久内的图", "unit": "秒",
             "requires": [{"key": "IMAGE_FOLLOWUP_ENABLED", "is": "true", "text": "接话的图片也看打开时才用"}]},
            {"key": "IMAGE_SCREEN_FALLBACK_ENABLED", "type": "bool", "default": "true", "label": "读不到图时截屏",
             "help": "数据库里拿不到图片时，从微信窗口截图。"},
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
             "help": "打开后机器人只处理 cc 任务：群里被 @、关键词、别人的私聊一律不回，不主动发言、不欢迎新人、不整理记忆，"
                     "也不调用任何 AI 模型（不用填 AI 密钥）。",
             "requires": [{"key": "CLAUDE_RELAY", "is": "on", "text": "中转打开时才能用"}]},
            {"key": "ADMIN_WXID", "type": "text", "default": "", "label": "管理员 wxid",
             "help": "你自己的微信 wxid（形如 wxid_xxx）。下面「谁能用」不填时，只有这个人能用中转。"},
            {"key": "CLAUDE_RELAY_WXIDS", "type": "names", "default": "", "label": "谁能用",
             "help": "能用 cc 指令的人的 wxid，逗号分隔；不填 = 只有管理员。只认这些人的私聊，群里的 cc 和别人发的都当普通消息。",
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
        "desc": "已登记达人的私聊消息不再当普通聊天：机器人不回任何话，把消息静默交给 Mac 上的 worker 起草回复"
                "（只能读写 prm_workspace 里的知识库和案卷，不能运行命令），草稿进「待审批」，运营点「发送」才会发出去。"
                "要同时打开 Claude 中转和「微信连接」里的私聊；Mac 的 config.json 要配好 prm_workspace。",
        "fields": [
            # No "requires": CLAUDE_RELAY lives in another group; saving checks it (_validate_config).
            {"key": "PRM_COPILOT", "type": "onoff", "default": "off", "label": "打开达人副驾驶",
             "help": "打开后在上面登记达人的 wxid；没登记的人照旧。要先打开 Claude 中转，改完要重启机器人。"},
            {"key": "PRM_TAKEOVER_MIN", "type": "int", "min": 5, "max": 1440, "default": "120",
             "label": "人工接管多久", "unit": "分钟",
             "help": "运营用这个微信号亲手回了达人（手机或电脑上），或在合作单里点「人工接管」，这段时间里达人的消息只记录、"
                     "不起草；时间到了自动回到副驾驶，也可以随时点「恢复副驾驶」。"},
            {"key": "PRM_HOLDING_MIN", "type": "int", "min": 0, "max": 120, "default": "10",
             "label": "草稿等多久先回一句", "unit": "分钟",
             "help": "草稿在「待审批」里放了这么久还没人处理，先自动回达人一句下面的话（达人每发一轮消息最多一次），草稿继续等。0 = 不回。"},
            {"key": "PRM_HOLDING_TEXT", "type": "text", "default": "收到，我确认一下马上回你", "label": "先回的那句话",
             "help": "1–60 个字。"},
            {"key": "PRM_RETENTION_DAYS", "type": "int", "min": 1, "max": 365, "default": "90",
             "label": "处理完多久清除收件信息", "unit": "天",
             "help": "合作单签收满这么多天，或者这么多天没有任何动静（没有消息、没有改动，比如结案、审批没通过），"
                     "地址只留省市、手机号和证件号只留打码的，聊天记录、草稿、中转记录、Mac 上的案卷都一起清。"},
        ],
    },
    {
        "id": "ocr", "code": "OCR", "title": "读取备选（OCR）",
        "desc": "数据库读不了的时候（WeFlow 没启动、密钥失效、DLL 打不开、微信升级后读不出来），改成截取微信窗口、用系统自带的文字识别"
                "（macOS 用苹果的，Windows 用系统自带的 Windows OCR）读当前打开的聊天。数据库恢复后自动切回来。" + OCR_BACKENDS + "。",
        "fields": [
            {"key": "OCR_FALLBACK", "type": "onoff", "default": "off", "label": "数据库读不了时用 OCR",
             "platform": OCR_BACKENDS, "platform_label": "macOS / Windows", "platform_backends": ["mac_hybrid", "wcdb"],
             "help": "只读当前打开的那个群聊，而且只读屏幕上显示的那一段；认不出是谁说的话，所以只能按 @机器人 或叫名字来触发；"
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
        "desc": "每条消息回了没有、为什么，都记一行（只记录，不改变机器人的行为）。「回复记录」页读的就是它。",
        "fields": [
            {"key": "DECISION_TRACE_ENABLED", "type": "bool", "default": "true", "label": "记录决策",
             "help": "关掉以后「回复记录」页就没有新数据了。"},
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
    """The value as the bot reads it, written the canonical way."""
    field = FIELDS[key]
    kind = field["type"]
    value = "" if raw is None else str(raw).strip()
    if raw is None or (value == "" and kind not in ("multi", "json")):
        value = field["default"]
    if kind == "onoff":
        return _parse_memory_mode(value)
    if kind == "bool":
        return "true" if value.lower() == "true" else "false"
    if kind == "multi":
        return ",".join(t.lower() for t in _split_names(value))
    if key == "PIPELINE_V2":
        return _parse_pipeline_mode(value)
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
        return "" if _parse_memory_mode(text) in ONOFF else "只能是 on 或 off"
    if kind == "bool":
        return "" if text.lower() in ("true", "false") else "只能是 true 或 false"
    if kind == "enum":
        parsed = _parse_pipeline_mode(text) if key == "PIPELINE_V2" else text.lower()
        return "" if parsed in field["options"] else "只能是 " + " / ".join(field["options"])
    if kind == "multi":
        parts = [p.strip().lower() for p in re.split(r"[,，]", text) if p.strip()]
        bad = [p for p in parts if p not in field["options"]]
        return "只能是 " + " / ".join(field["options"]) + " 里的几个" if bad else ""
    if kind == "json":
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return f"不是有效的 JSON：{exc.msg}"
        return "" if isinstance(parsed, dict) else "要是一个 JSON 对象（用 { } 包起来）"
    if key == "LOCAL_BASE_URL" and text and not text.startswith(("http://", "https://")):
        return "要以 http:// 或 https:// 开头"
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
    if kind == "path":
        return "" if text else "不能为空"
    if key == "WECHAT_EMOJI_CODES" and text and not parse_wechat_emoji_codes(text):
        return "没有认出任何表情代码，写成 捂脸,旺柴 这样"
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
            out[name] = _parse_memory_mode(value)
        elif key == "PIPELINE_V2":
            out[name] = _parse_pipeline_mode(value)
        elif kind == "enum":
            out[name] = str(value).strip().lower() or field["default"]
        elif kind == "names":
            out[name] = _split_names(value)
        elif kind == "multi":
            out[name] = [t.lower() for t in _split_names(value)]
        elif kind == "json":
            out[name] = _parse_json_object(value)
        elif key == "WECHAT_EMOJI_CODES":
            out[name] = list(parse_wechat_emoji_codes(value))
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
            hit = next((k for k in FIELDS if line.startswith(k)), None)
            if line.startswith("CLAUDE_RELAY=on needs "):
                # two messages start alike: each belongs to the field it names
                hit = "CLAUDE_RELAY_TOKEN" if "CLAUDE_RELAY_TOKEN" in line else "CLAUDE_RELAY_WXIDS"
            if hit is None and line.startswith("MEMORY_EMBEDDINGS=on"):
                hit = "MEMORY_EMBEDDINGS"
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
