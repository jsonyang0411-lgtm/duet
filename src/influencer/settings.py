"""达人商务模块的业务参数和开关。数值的来历见 handoff.md 第 6 节的决策表。"""

import os
from typing import Dict, Optional

# 佣金（D5、D37）：2026-10-26 用户定起步 20%，带货表现好再涨，最高 25%；执行时超过上限一律拒绝
DEFAULT_COMMISSION_RATE = 0.20
MAX_COMMISSION_RATE = 0.25

# 审批单有效期：过期后不能再执行。运营每天 10:00、16:00 集中审批（R6），
# 晚上来的申请要能等到第二天，所以给 72 小时；批准时会重新查一次库存（Q1）
APPROVAL_TTL_SECONDS = 72 * 3600

# 执行一张审批单正常只要几秒。开始执行超过这个时间还停在"执行中"，就认为执行被中断了
# （断电、强制退出），运营人工到聚水潭和小红书核对后，可以在控制台把它标成已执行或失败（B23）。
# 没到这个时间不让标，免得和还在跑的执行撞上
STUCK_EXECUTION_SECONDS = 10 * 60

# 收件人的姓名、电话、地址和达人原话，在审批单处理完（驳回、执行完成、失败或过期）
# 90 天后清除，只留统计信息（Q3）
PERSONAL_INFO_RETENTION_SECONDS = 90 * 24 * 3600

# 索样意图的关键词。商品清单先写死，接入聚水潭后改为从商品目录读取；
# 长的放在前面，先匹配到更具体的商品（"VC精华液"先于"精华"）
# 以前规则对话里没补齐的索样申请（老库里的），超过这个时间删掉
DRAFT_TTL_SECONDS = 24 * 3600

# 谈佣金的固定话术，同一位达人在这段时间内最多放进待审批一次，免得刷屏
HOLDING_REPLY_INTERVAL_SECONDS = 6 * 3600

# 达人要求提高佣金：上限写死在 MAX_COMMISSION_RATE，不交给模型谈（2026-10-25 用户定）。
# 识别到以后，固定话术（handler.COMMISSION_REPLY）作为系统消息放进待审批（prm_bridge），并记进「待处理」。
# 同一位达人在 HOLDING_REPLY_INTERVAL_SECONDS 内再提，就不再重复放，只记待处理
COMMISSION_WORDS = ("佣金", "点位", "返点", "提成", "分成", "点数", "佣")
COMMISSION_RAISE_WORDS = (
    "提高", "提一", "提点", "调高", "上调", "加点", "加一", "加个", "涨", "高点", "高一", "高些",
    "更高", "再高", "太低", "有点低", "偏低", "低了", "少了", "再多", "多给", "多一", "能不能高", "可以高",
)

# 资质：D30（2026-10-26）起不看粉丝数，寄几款按聚水潭的销售额分档；审批单上的粉丝数只作参考，不拦单


# 寄样上限（D5，数字还没定）：环境变量不设就是不限。超过上限的审批单在控制台上标出来，不能批准
#   INFLUENCER_MAX_SAMPLES_PER_PERSON：同一位达人在 INFLUENCER_SAMPLE_WINDOW_DAYS 天内最多寄几单
#   INFLUENCER_MAX_SAMPLES_PER_DAY：所有达人每天一共最多寄几单
# 算的是已执行和执行中的单
DEFAULT_SAMPLE_WINDOW_DAYS = 30


def _positive_int_env(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def sample_limits() -> Dict[str, Optional[int]]:
    return {
        "per_person": _positive_int_env("INFLUENCER_MAX_SAMPLES_PER_PERSON"),
        "window_days": _positive_int_env("INFLUENCER_SAMPLE_WINDOW_DAYS") or DEFAULT_SAMPLE_WINDOW_DAYS,
        "per_day": _positive_int_env("INFLUENCER_MAX_SAMPLES_PER_DAY"),
    }


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
