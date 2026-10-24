"""达人商务模块的业务参数。数值的来历见 handoff.md 第 6 节的决策表。"""

# 佣金（D5，2026-10-24 暂定）：审批单默认 25%；执行时超过上限一律拒绝
DEFAULT_COMMISSION_RATE = 0.25
MAX_COMMISSION_RATE = 0.25

# 审批单有效期：过期后不能再执行
APPROVAL_TTL_SECONDS = 4 * 3600
