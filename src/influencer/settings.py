"""达人商务模块的业务参数和开关。数值的来历见 handoff.md 第 6 节的决策表。"""

import os

# 佣金（D5，2026-10-24 暂定）：审批单默认 25%；执行时超过上限一律拒绝
DEFAULT_COMMISSION_RATE = 0.25
MAX_COMMISSION_RATE = 0.25

# 审批单有效期：过期后不能再执行。运营每天 10:00、16:00 集中审批（R6），
# 晚上来的申请要能等到第二天，所以给 72 小时；批准时会重新查一次库存（Q1）
APPROVAL_TTL_SECONDS = 72 * 3600

# 收件人的姓名、电话、地址和达人原话，在审批单处理完（驳回、执行完成、失败或过期）
# 90 天后清除，只留统计信息（Q3）
PERSONAL_INFO_RETENTION_SECONDS = 90 * 24 * 3600

# 索样意图的关键词。商品清单先写死，接入聚水潭后改为从商品目录读取；
# 长的放在前面，先匹配到更具体的商品（"VC精华液"先于"精华"）
SAMPLE_KEYWORDS = ("样品", "样单", "寄样", "带货", "合作", "试用", "选品", "测品", "推品", "佣金")
PRODUCT_KEYWORDS = ("VC精华液", "冻干粉", "泥膜", "眼霜", "洁面乳", "精华", "面膜")

# 没补齐的索样申请，超过这个时间没有动静就作废，之后的消息按新对话处理
DRAFT_TTL_SECONDS = 24 * 3600

# 资质门槛：只用来在审批单上标注"达标 / 不达标"，不自动拒绝（D4）。
# 原备忘录写的是"粉丝 + 点赞破千"；2026-10-25 用户确认以这里的数值为准（B17、D12）
MIN_FOLLOWERS = 10000
MIN_RECENT_LIKES = 1000


def mock_enabled() -> bool:
    """模拟模式只能手动打开（D6）：设置环境变量 INFLUENCER_MOCK=1。"""
    return os.getenv("INFLUENCER_MOCK", "").strip().lower() in ("1", "true", "yes", "on")


def influencer_chat_ids_from_env() -> set:
    """环境变量 INFLUENCER_CHAT_IDS 里的达人 chat_id（逗号分隔）。

    名单以数据库为准，在控制台维护（D3）。这个变量只在机器人启动时并进名单一次：
    只增不删，控制台里停用的达人不会因此恢复（Q4）。
    """
    raw = os.getenv("INFLUENCER_CHAT_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}
