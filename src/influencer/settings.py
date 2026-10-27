"""达人商务模块的业务参数和开关。数值的来历见 handoff.md 第 6 节的决策表。"""

import os

# 收件人的姓名、电话、地址和达人原话，90 天后清除（Q3、D10）：老的寄样审批单、待处理事项、
# 以前的对话记录和发送队列，以及原项目表里达人私聊的消息和记忆（store.run_maintenance）
PERSONAL_INFO_RETENTION_SECONDS = 90 * 24 * 3600

# 以前规则对话里没补齐的索样申请（老库里的），超过这个时间删掉
DRAFT_TTL_SECONDS = 24 * 3600


def influencer_chat_ids_from_env() -> set:
    """环境变量 INFLUENCER_CHAT_IDS 里的达人 chat_id（逗号分隔）。

    名单以数据库为准，在控制台维护（D3）。这个变量只在机器人启动时并进名单一次：
    只增不删，控制台里停用的达人不会因此恢复（Q4）。
    """
    raw = os.getenv("INFLUENCER_CHAT_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}
