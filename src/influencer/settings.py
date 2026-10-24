"""达人商务模块的业务参数和开关。数值的来历见 handoff.md 第 6 节的决策表。"""

import os

# 佣金（D5，2026-10-24 暂定）：审批单默认 25%；执行时超过上限一律拒绝
DEFAULT_COMMISSION_RATE = 0.25
MAX_COMMISSION_RATE = 0.25

# 审批单有效期：过期后不能再执行
APPROVAL_TTL_SECONDS = 4 * 3600

# 资质门槛：只用来在审批单上标注"达标 / 不达标"，不自动拒绝（D4）。
# 与备忘录"粉丝 + 点赞破千"的说法不一致，待确认（B17）
MIN_FOLLOWERS = 10000
MIN_RECENT_LIKES = 1000


def mock_enabled() -> bool:
    """模拟模式只能手动打开（D6）：设置环境变量 INFLUENCER_MOCK=1。"""
    return os.getenv("INFLUENCER_MOCK", "").strip().lower() in ("1", "true", "yes", "on")
