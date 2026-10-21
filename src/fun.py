"""趣味功能模块 — 群聊小玩法。"""

import json
import logging
import random
import re
from pathlib import Path

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# ── 默认签文数据（用户未自定义时使用）──────────────────────────────

_DEFAULT_LEVELS = [
    ("大吉", "🟢", [
        "福星在线，连外卖都能准点到",
        "好运爆棚，随手一抽全是惊喜",
        "今日开挂，困难见了都绕路走",
        "钱包回暖，心情直接原地起飞",
        "贵人冒头，关键时刻有人搭把手",
        "灵感喷涌，脑子比咖啡还提神",
        "状态满格，干啥都像顺风局",
        "喜气上头，小目标今天能推进",
        "效率拉满，摸鱼都不耽误正事",
        "万事顺滑，连红灯都像在放假",
        "好运排队，惊喜可能正在路上",
        "气场全开，今天适合大胆冲一把",
        "心想事成，连拖延症都被治好",
        "财运冒泡，奶茶钱有望自己来",
        "人品爆发，抽卡都像开了会员",
        "顺风顺水，麻烦暂时自动静音",
        "快乐加倍，今天笑点格外密集",
        "机会敲门，记得把门开大一点",
        "元气满满，整个人像刚充满电",
        "锦鲤附体，好消息可能连发三条",
    ]),
    ("中吉", "🔵", [
        "稳稳当当，小惊喜藏在日常里",
        "节奏不错，慢慢来反而更顺",
        "好运不吵，悄悄把路铺平了",
        "心态在线，小麻烦基本能搞定",
        "平平安安，今天适合稳步推进",
        "云淡风轻，事情会比想象简单",
        "手感回升，做完一件是一件",
        "靠谱发挥，普通日子也有盼头",
        "小有收获，努力没有白白浪费",
        "气氛不错，聊天办事都挺顺",
        "稳中带甜，生活给了颗小糖",
        "进展缓慢，但方向看起来没跑偏",
        "人缘尚可，关键消息来得刚好",
        "状态回正，适合把欠账慢慢清",
        "风浪不大，今天能平稳靠岸",
        "好运轻敲，别急着把门关上",
        "计划能动，虽然不快但很扎实",
        "小确幸在线，晚饭可能特别香",
        "心里有谱，事情基本不会失控",
        "普通但顺，今天适合安静变好",
    ]),
    ("小吉", "🟡", [
        "勉强能行，别把难度开太高",
        "小风小浪，问题不大但挺烦",
        "运气微亮，像手机还剩百分二十",
        "凑合不错，至少没有突然翻车",
        "缓慢回血，先把今天糊过去",
        "好运摸鱼，偶尔出来打个卡",
        "状态一般，但还能靠意志硬撑",
        "小赚一点，快乐像试用版会员",
        "别太激动，惊喜可能只有半份",
        "能过就行，细节先别太较真",
        "轻微顺利，像电梯刚好没关门",
        "运势及格，主打一个差不多",
        "脑子慢热，下午可能突然上线",
        "事情能成，就是过程有点磨叽",
        "快乐有限，但也不是完全没有",
        "小小转机，别嫌它来得低调",
        "今天不差，适合低成本开心",
        "能量半格，省着点用也够了",
        "小吉小吉，主打一个没白抽",
        "运气路过，顺手留下点零食",
    ]),
    ("末吉", "🟠", [
        "今天适合躺平，少折腾少背锅",
        "运势偏弱，先把基本盘守住",
        "别硬刚了，能拖一拖也是智慧",
        "心累预警，建议降低人生期待",
        "事情有点卡，先喝口水再说",
        "别太上头，今天适合保守操作",
        "能不加戏，就别给生活递剧本",
        "状态掉线，早点休息比硬撑强",
        "小坑不少，走路都建议看脚下",
        "计划缩水，能完成一半也算赢",
        "情绪别炸，世界暂时有点卡顿",
        "今日低电量，社交能省则省",
        "别追求完美，别翻车就算发挥",
        "风向一般，安静待机更划算",
        "灵感请假，脑子暂时不接单",
        "适合装忙，不适合主动揽活",
        "进度随缘，别跟自己死磕",
        "钱包别动，冲动消费容易后悔",
        "先别立旗，今天旗子容易倒",
        "保持低调，熬过今天就是胜利",
    ]),
    ("凶", "🔴", [
        "诸事不宜，早点下班最有性价比",
        "今天别赌，连硬币都可能叛逆",
        "运气休假，重要决定改天再议",
        "水逆感很强，先别挑战高难度",
        "别硬冲了，生活正在加载补丁",
        "脑子罢工，先别写长篇大论",
        "今日易翻车，安全带先系紧",
        "手气很迷，抽卡前请冷静三秒",
        "钱包告急，购物车先别清空",
        "情绪易燃，建议远离无效争论",
        "事情不顺，但至少还能笑一笑",
        "运势掉线，刷新也未必有用",
        "今日不宜嘴快，沉默能省麻烦",
        "麻烦冒头，先装作信号不好",
        "计划易碎，别把希望全压上",
        "状态离谱，适合早点洗洗睡",
        "别立大志，今天先活着就很棒",
        "运气堵车，好消息可能迟到",
        "别碰玄学，连抽签都在叹气",
        "今日主打避险，能苟住就是赢",
    ]),
]

_DEFAULT_WEIGHTS = [12, 23, 30, 20, 15]

_TAROT_CARDS = [
    ("愚者", "新的开始", "可以试试，但别什么都不准备。", "冲动会让你踩空，先慢一点。"),
    ("魔术师", "行动力", "手里的资源够用，先把第一步做起来。", "想法太散，别急着证明自己。"),
    ("女祭司", "直觉", "答案可能已经在心里了，安静一点会看清。", "别只靠猜，重要事还是问清楚。"),
    ("皇后", "滋养", "适合照顾自己，也适合让关系慢慢变软。", "别一味付出，先看看自己累不累。"),
    ("皇帝", "秩序", "把边界和计划定好，事情会稳很多。", "太硬会卡住，偶尔也要听人说话。"),
    ("教皇", "建议", "找靠谱的人问问，按规矩来反而省心。", "别被标准答案绑住，你也可以有自己的路。"),
    ("恋人", "选择", "真心和沟通很重要，别用猜的。", "暧昧或犹豫会拖住你，要想清楚。"),
    ("战车", "推进", "目标明确一点，就能往前冲。", "方向没稳前别硬冲，不然会很累。"),
    ("力量", "温柔的坚持", "别怕，你比自己想的更能撑住。", "忍太久会委屈，别把情绪全压下去。"),
    ("隐者", "沉淀", "先独处想清楚，不用急着给答案。", "别把自己关太久，该说的话还是要说。"),
    ("命运之轮", "转机", "变化快来了，顺势一点会更轻松。", "别赌运气，能准备的先准备好。"),
    ("正义", "公平", "按事实判断，别被情绪带跑。", "有些账要算清，不然心里会一直堵。"),
    ("倒吊人", "换角度", "暂时停一下，换个看法可能就通了。", "别一直牺牲自己，不值得的事要放手。"),
    ("死神", "结束与更新", "旧阶段该收尾了，新东西才进得来。", "舍不得放下，才会一直卡在原地。"),
    ("节制", "平衡", "慢慢调，别一下子用力过猛。", "节奏乱了，先把生活拉回稳定。"),
    ("恶魔", "执念", "看清自己被什么牵着走，就能松一点。", "别被欲望或依赖困住，要留退路。"),
    ("高塔", "突变", "有些崩掉的东西，本来就不够稳。", "别逃避问题，小裂缝会越拖越大。"),
    ("星星", "希望", "还有希望，别把自己看得太低。", "期待别太空，给愿望一点实际行动。"),
    ("月亮", "迷雾", "现在信息不全，别急着下结论。", "容易想太多，先确认事实。"),
    ("太阳", "明朗", "事情会变清楚，心情也会亮一点。", "别太乐观到忽略细节。"),
    ("审判", "回应", "该面对的会有结果，也该给自己一次机会。", "别一直翻旧账，先决定下一步。"),
    ("世界", "完成", "这件事接近收束，会有一个完整答案。", "最后一步别松懈，收尾也很重要。"),
]
_TAROT_LOVE_MARKERS = re.compile(
    r"(正缘|桃花|感情|恋爱|对象|脱单|姻缘|喜欢的人|暧昧|男朋友|女朋友)"
)
_TAROT_TOPIC_RE = re.compile(
    r"(正缘|桃花|感情|恋爱|对象|脱单|姻缘|喜欢的人|暧昧|男朋友|女朋友|"
    r"事业运|事业|工作|财运|钱|考试|学习|学业|运势)"
)
_TAROT_SOURCE_FAST = {"愚者", "魔术师", "战车", "命运之轮", "高塔", "太阳"}
_TAROT_SOURCE_CIRCLE = {"皇后", "教皇", "恋人", "正义", "世界"}
_TAROT_SOURCE_QUIET = {"女祭司", "隐者", "月亮", "星星", "节制"}
_TAROT_SLOW = {"女祭司", "隐者", "倒吊人", "节制", "世界"}
_TAROT_UNSTABLE = {"月亮", "恶魔", "死神", "高塔"}

_LOT_COMMANDS = ("抽签",)
_TAROT_COMMANDS = (
    "我想测一下塔罗牌",
    "我想测一下塔罗",
    "我想测测塔罗牌",
    "我想测测塔罗",
    "我想测塔罗牌",
    "我想测塔罗",
    "帮我抽一张塔罗牌",
    "帮我抽一张塔罗",
    "帮我测一下塔罗牌",
    "帮我测一下塔罗",
    "帮我测测塔罗牌",
    "帮我测测塔罗",
    "帮我测塔罗牌",
    "帮我测塔罗",
    "测一下塔罗牌",
    "测一下塔罗",
    "测测塔罗牌",
    "测测塔罗",
    "测塔罗牌",
    "测塔罗",
    "帮我抽塔罗牌",
    "帮我抽塔罗",
    "抽一张塔罗牌",
    "抽一张塔罗",
    "塔罗占卜",
    "抽塔罗牌",
    "抽塔罗",
    "塔罗牌",
    "塔罗",
)
_TAROT_WORD_RE = re.compile(r"塔罗牌|塔罗")
_TAROT_ACTION_RE = re.compile(r"测测|测|测试|占卜|占|算算|算|抽|看看|看")
_TAROT_DIRECT_ACTION_ONLY_RE = re.compile(
    r"^(?:我想|我也想|我要|我也要|能不能|能帮我|你帮我|帮我|给我|"
    r"麻烦你|麻烦|请你|请)?\s*"
    r"(?:测一下|测测|测|测试|占卜|占一下|占|算一下|算算|算|抽一下|抽)"
    r"\s*(?:一下|一下子|吗|嘛|么)?$"
)
_TAROT_TOPIC_COMMAND_RE = re.compile(
    r"^(?:我想|我也想|我要|我也要|能不能|能帮我|你帮我|帮我|给我|"
    r"麻烦你|麻烦|请你|请)?\s*"
    r"(?:测一下|测一测|测测|测试|测|占卜|占一下|占一占|占|算一下|算一算|算算|算|"
    r"看一下|看一看|看看|看|抽一下|抽一抽|抽)\s*"
    r"(?:一下|一下子)?\s*(?:我们的|我的|我)?\s*"
    r"(?:近期|最近|这个月|未来三个月|三个月内|今年)?\s*(?:的)?\s*"
    r"(?:正缘|桃花|感情|恋爱|对象|脱单|姻缘|喜欢的人|暧昧|男朋友|女朋友|"
    r"事业运|事业|工作|财运|钱|考试|学习|学业|运势)"
)
_TAROT_CLEAN_RE = re.compile(
    r"(我想|我也想|我要|我也要|可以|能不能|能帮我|你帮我|帮我|麻烦你|麻烦|请你|请|给我|"
    r"测一测|算一算|占一占|看一看|抽一抽|"
    r"测一下|测测|测试|测|占卜|占一下|占|算一下|算算|算|抽一张|抽一下|抽|看一下|看看|看|"
    r"塔罗牌|塔罗|一下|一下子|吗|嘛|么)"
)
_TAROT_FOLLOWUP_RE = re.compile(
    r"(你帮我|帮我|给我|测测|测一下|测|算算|算一下|算|看看|看一下|看|"
    r"正缘|姻缘|脱单|财运|感情|事业运|事业|考试|学习|学业|桃花|恋爱|"
    r"男朋友|女朋友|钱|工作|运势)"
)

# ── JSON 文件路径 ──────────────────────────────────────────────────

_LOTS_PATH = Path("data/lots.json")

# ── 缓存 ────────────────────────────────────────────────────────────

_lots_cache: tuple[list, list] | None = None


def _build_default_lots() -> tuple[list, list]:
    """将硬编码的默认数据转为标准化格式（levels 列表 + weights 列表）。"""
    levels = [
        {"name": name, "emoji": emoji, "phrases": list(phrases)}
        for name, emoji, phrases in _DEFAULT_LEVELS
    ]
    return levels, list(_DEFAULT_WEIGHTS)


def load_lots_config() -> dict:
    """读取当前抽签配置，返回标准化 dict。

    加载顺序：JSON 文件 → 代码默认值。

    Returns:
        {"weights": [...], "levels": [{"name": str, "emoji": str, "phrases": [...]}, ...]}
    """
    levels, weights = _get_lots()
    return {"weights": weights, "levels": levels}


def save_lots_config(data: dict) -> None:
    """保存抽签配置到 JSON 文件并清除缓存。

    Args:
        data: {"weights": [...], "levels": [...]}

    Raises:
        ValueError: 格式校验失败时抛出。
    """
    weights = data.get("weights", [])
    levels = data.get("levels", [])

    # ── 恢复默认：空数据 → 删除 JSON 文件 ──────────────────────
    if (not weights or len(weights) == 0) and (not levels or len(levels) == 0):
        if _LOTS_PATH.exists():
            _LOTS_PATH.unlink()
        reset_lots_cache()
        logger.info("抽签配置已恢复为默认值（删除了 lots.json）")
        return

    # ── 校验 ──────────────────────────────────────────────────────
    if not isinstance(weights, list) or not isinstance(levels, list):
        raise ValueError("weights 和 levels 必须是数组")
    if len(weights) != len(levels):
        raise ValueError(
            f"weights 长度 ({len(weights)}) 与 levels 长度 ({len(levels)}) 不一致"
        )
    if len(levels) == 0:
        raise ValueError("至少需要一个等级")
    for w in weights:
        if not isinstance(w, (int, float)) or w <= 0:
            raise ValueError(f"权重必须是正数，得到: {w}")
    for i, level in enumerate(levels):
        if not isinstance(level, dict):
            raise ValueError(f"levels[{i}] 必须是对象")
        if not level.get("name", "").strip():
            raise ValueError(f"levels[{i}] 缺少名称")
        phrases = level.get("phrases", [])
        if not isinstance(phrases, list) or len(phrases) == 0:
            raise ValueError(f"「{level.get('name', i)}」至少需要一条签文")

    # ── 写入 ──────────────────────────────────────────────────────
    _LOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LOTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_LOTS_PATH)

    # 清除缓存
    reset_lots_cache()
    logger.info("抽签配置已保存到 %s", _LOTS_PATH)


def reset_lots_cache() -> None:
    """清除抽签缓存，下次调用 _get_lots() 时重新加载。"""
    global _lots_cache
    _lots_cache = None


def _load_lots_from_json() -> tuple[list, list] | None:
    """从 JSON 文件加载抽签配置。失败返回 None。"""
    if not _LOTS_PATH.exists():
        return None
    try:
        data = json.loads(_LOTS_PATH.read_text(encoding="utf-8"))
        weights = data.get("weights", [])
        levels = data.get("levels", [])

        # 快速校验（不做完整校验——save 时已经校验过；这里只防文件被手动损坏）
        if not weights or not levels or len(weights) != len(levels):
            logger.warning("lots.json 格式无效，使用默认签文")
            return None
        for level in levels:
            if not level.get("phrases"):
                logger.warning("lots.json 存在空签文等级，使用默认签文")
                return None

        return levels, weights
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("无法读取 lots.json (%s)，使用默认签文", e)
        return None


def _get_lots() -> tuple[list, list]:
    """获取当前抽签数据（带缓存）。

    Returns:
        (levels, weights) — levels 是 list[dict]，weights 是 list[float]。
    """
    global _lots_cache
    if _lots_cache is not None:
        return _lots_cache

    loaded = _load_lots_from_json()
    if loaded is not None:
        _lots_cache = loaded
    else:
        _lots_cache = _build_default_lots()

    return _lots_cache


# ── 抽签 ────────────────────────────────────────────────────────


def draw_lots(requester_name: str) -> str:
    """抽签 — 返回带解读的运势结果。

    优先使用 data/lots.json 中的自定义配置，文件不存在时使用默认签文。
    """
    levels_data, weights = _get_lots()

    # 构建签文选项。图标仅用于配置兼容，不会输出到微信。
    lots = [
        (level["name"], level.get("emoji", ""), level["phrases"])
        for level in levels_data
    ]

    (label, _emoji, phrases), = random.choices(lots, weights=weights, k=1)
    phrase = random.choice(phrases)
    return (
        f"@{requester_name} 抽签结果：{label}\n"
        f"{phrase}"
    )


def _strip_command_prefix(text: str, bot_name: str = "") -> str:
    text = str(text or "").strip()
    names = []
    if bot_name:
        names.append(bot_name.strip())
        if len(bot_name.strip()) >= 3:
            names.append(bot_name.strip()[-2:])
    for name in sorted(set(n for n in names if n), key=len, reverse=True):
        text = re.sub(
            rf"^\s*@?{re.escape(name)}[\s,，:：。！!？?]*",
            "",
            text,
        ).strip()
    return text.strip()


def is_lots_command(text: str, bot_name: str = "") -> bool:
    """Return whether text asks for the existing fortune lot."""
    text = _strip_command_prefix(text, bot_name)
    return text in _LOT_COMMANDS


def _clean_tarot_question(text: str) -> str:
    rest = _TAROT_CLEAN_RE.sub(" ", str(text or ""))
    rest = re.sub(r"\s+", " ", rest).strip(" ，,。！!？?:：-")
    rest = re.sub(r"^(?:我的|本人|自己|的)\s*", "", rest)
    return rest.strip(" ，,。！!？?:：-")


def parse_tarot_question(text: str, bot_name: str = "") -> str | None:
    """Parse a tarot command and return the optional question.

    Returns None when the text is not a tarot command; otherwise returns the
    question text, which may be an empty string.
    """
    text = _strip_command_prefix(text, bot_name)
    text = text.strip(" ，,。！!？?")
    for command in _TAROT_COMMANDS:
        if text == command:
            return ""
        if text.startswith(command):
            rest = text[len(command):].strip(" ，,。！!？?:：-")
            return rest
    if _TAROT_WORD_RE.search(text) and _TAROT_ACTION_RE.search(text):
        return _clean_tarot_question(text)
    # Without the word 塔罗 the topic must follow the action directly
    # ("测测我的财运"); "帮我算算房租一共多少钱" or "预算500 送女朋友" merely
    # contain an action character and a topic word somewhere.
    if _TAROT_TOPIC_COMMAND_RE.match(text):
        return _clean_tarot_question(text)
    if _TAROT_DIRECT_ACTION_ONLY_RE.fullmatch(text):
        return ""
    return None


def parse_tarot_followup_question(text: str) -> str | None:
    """Parse short follow-ups after a visible tarot reading."""
    text = str(text or "").strip()
    if not text or len(text) > 80:
        return None
    if not _TAROT_FOLLOWUP_RE.search(text):
        return None
    return _clean_tarot_question(text)


def draw_tarot(requester_name: str, question: str = "") -> str:
    """Draw a three-card major-arcana spread within a three-month window."""
    subject, is_love = _tarot_subject(question)
    cards = random.sample(_TAROT_CARDS, k=3)
    orientations = [random.choice(("正位", "逆位")) for _ in cards]

    lines = [
        f"@{requester_name} 塔罗三张：",
        "先按你心里默念的问题看，只看三个月内。",
        f"问题：{subject}",
    ]

    for index, (card, orientation) in enumerate(zip(cards, orientations)):
        name, keyword, upright, reversed_reading = card
        reading = upright if orientation == "正位" else reversed_reading
        if is_love:
            title, detail = _love_card_detail(index, name, orientation)
        else:
            title, detail = _general_card_detail(index, name, orientation)
        lines.append(
            f"{title}：{name}（{orientation}）｜{keyword}｜{detail}{reading}"
        )

    lines.append(_tarot_summary(cards, orientations, is_love))

    return "\n".join(lines)


def _tarot_subject(question: str) -> tuple[str, bool]:
    question = str(question or "").strip(" ，,。！!？?:：-")
    if not question:
        return "未来三个月内的正缘", True
    if len(question) > 28:
        question = question[:28].rstrip() + "..."
    has_window = any(token in question for token in ("三个月", "3个月", "近期", "最近"))
    subject = question if has_window else f"未来三个月内的{question}"
    return subject, bool(_TAROT_LOVE_MARKERS.search(question))


def _love_card_detail(index: int, name: str, orientation: str) -> tuple[str, str]:
    if index == 0:
        if name in _TAROT_SOURCE_FAST:
            detail = "来得偏突然，可能是临时邀约、线上聊天或一次主动接触。"
        elif name in _TAROT_SOURCE_CIRCLE:
            detail = "更像从共同圈子、朋友介绍或熟人场景里冒出来。"
        elif name in _TAROT_SOURCE_QUIET:
            detail = "不是很张扬，可能从私聊、安静关注或慢慢熟起来开始。"
        else:
            detail = "缘分入口不算直接，先像普通互动，再慢慢有感觉。"
        return "正缘怎么来", detail
    if index == 1:
        if name in _TAROT_SOURCE_FAST and orientation == "正位":
            detail = "动静偏快，像一到四周内会有明显接触。"
        elif name in _TAROT_SLOW or orientation == "逆位":
            detail = "时间偏慢，可能在两到三个月内才变清楚。"
        else:
            detail = "大概一个月左右到两个月内，会有比较值得留意的人。"
        return "大概什么时候", detail
    if name in _TAROT_UNSTABLE or orientation == "逆位":
        detail = "来往会有试探和忽冷忽热，别太快把话说死。"
    elif name in {"恋人", "太阳", "星星", "皇后", "世界"}:
        detail = "会聊得比较自然，容易从分享日常慢慢靠近。"
    else:
        detail = "一开始像普通聊天或帮忙，后面才看得出有没有暧昧。"
    return "你们会怎么来往", detail


def _general_card_detail(index: int, name: str, orientation: str) -> tuple[str, str]:
    if index == 0:
        return "事情怎么起", "起因多半已经在身边，不是完全凭空出现。"
    if index == 1:
        if name in _TAROT_SOURCE_FAST and orientation == "正位":
            detail = "三个月内前段就会有动静。"
        elif name in _TAROT_SLOW or orientation == "逆位":
            detail = "三个月内后段才更明显。"
        else:
            detail = "一个月到两个月之间比较值得留意。"
        return "什么时候有变化", detail
    return "建议", "别只等结果，主动整理信息和选择会更有用。"


def _tarot_summary(cards: list[tuple], orientations: list[str],
                   is_love: bool) -> str:
    reversed_count = sum(1 for item in orientations if item == "逆位")
    names = "、".join(card[0] for card in cards)
    if is_love:
        if reversed_count >= 2:
            return f"合起来看：{names}偏慢热，三个月内有机会，但要先观察别急着认定。"
        return f"合起来看：{names}不算空，三个月内会有能聊起来的人，先从轻松接触开始。"
    if reversed_count >= 2:
        return f"合起来看：{names}提醒你别急，三个月内先稳住节奏。"
    return f"合起来看：{names}有推进感，三个月内可以主动一点。"
