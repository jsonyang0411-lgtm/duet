"""Text helpers shared by memory extraction and retrieval.

Everything here is pure and linear-time: no nested quantifiers, bounded
inputs.  The inputs are other people's chat messages and model output.
"""

from __future__ import annotations

import re
import unicodedata

# Quotes and queries never need more than this; callers truncate first so the
# regexes below cannot be fed an unbounded string.
MAX_TEXT_CHARS = 400

# Words that only frame a question about the past.  They are cut out before
# the query is turned into character n-grams so that "上次小明说想去哪里玩" is
# searched as "小明说想去玩".  Longest first (see _NOISE_RE).
NOISE_WORDS = (
    "你还记得", "还记得", "记不记得", "你记得", "记得", "你知道", "我想知道", "告诉我",
    "请问", "问一下", "帮我", "有没有", "是不是", "是谁", "是什么", "什么", "怎么",
    "为什么", "哪个", "哪里", "哪儿", "谁", "上次", "之前", "以前", "刚才", "前面",
    "说过", "提过", "讲过", "聊过", "来着", "一下", "的话",
)
# Single-character particles are deliberately NOT here: cutting "吧" or "哈"
# out of the query would damage words such as 酒吧 and 哈尔滨.
_NOISE_RE = re.compile(
    "|".join(re.escape(w) for w in sorted(NOISE_WORDS, key=len, reverse=True))
)
_RUN_RE = re.compile(r"[0-9a-z㐀-鿿]+")

# A two-character window containing one of these is rarely a content word.
FUNCTION_CHARS = frozenset("的了吗呢吧啊呀嘛是在有我你他她它们这那不也都就还和跟与或但而被把给让对从向为以及")


def clip(text: object, limit: int = MAX_TEXT_CHARS) -> str:
    return str(text or "")[:limit]


def _canon_map(text: object) -> tuple[str, list[int]]:
    """Canonical form of ``text`` plus, for each kept character, its index in the original.

    Letters and digits only, NFKC-folded character by character, so a match in
    the canonical form can be cut out of the original text again.
    """
    original = clip(text, 4000)
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(original):
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if unicodedata.category(c)[0] in ("L", "N"):
                chars.append(c)
                index.append(i)
    return "".join(chars), index


def canon(text: object) -> str:
    """Letters and digits only, NFKC-folded: the form quotes are compared in."""
    return _canon_map(text)[0]


# A quote that spans one of these is two sentences glued together.
SENTENCE_ENDS = frozenset("。！？!?；;\n")
MAX_EXCERPT_CHARS = 120


def sanitize(text: object, limit: int = MAX_TEXT_CHARS) -> str:
    """One line of printable text: whitespace collapsed, every control, format,
    private-use and unassigned character (zero-width, tag characters...) removed."""
    out = []
    for ch in clip(text, limit):
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch)[0] != "C":
            out.append(ch)
    return " ".join("".join(out).split())


def find_excerpt(quote: object, content: object, *, min_chars: int = 3) -> str | None:
    """The slice of ``content`` that ``quote`` stands for, or None.

    The match ignores spacing, punctuation, case and width, but it has to lie
    inside one sentence.  What comes back is the original text (sanitised), not
    the model's rendering of it.
    """
    needle = canon(quote)
    if len(needle) < min_chars:
        return None
    hay, index = _canon_map(content)
    pos = hay.find(needle)
    if pos < 0:
        return None
    original = clip(content, 4000)
    span = original[index[pos]:index[pos + len(needle) - 1] + 1]
    if any(ch in SENTENCE_ENDS for ch in span):
        return None
    return sanitize(span, MAX_EXCERPT_CHARS)


def quote_matches(quote: object, content: object, *, min_chars: int = 3) -> bool:
    """True when ``quote`` is a real single-sentence excerpt of ``content``."""
    return find_excerpt(quote, content, min_chars=min_chars) is not None


_EDGE_PUNCT = " \t.,!?;:~…。，！？、；：·"


def norm_key(subject: object, predicate: object, obj: object) -> str:
    """Identity of a fact: case, width and spacing are ignored, symbols are not
    ("C++" and "C#", "3.5万" and "35万" stay different)."""
    def form(part: object) -> str:
        folded = unicodedata.normalize("NFKC", clip(part, 400)).casefold()
        kept = "".join(ch for ch in folded if not ch.isspace() and unicodedata.category(ch)[0] != "C")
        return kept.strip(_EDGE_PUNCT)
    return "|".join(form(part) for part in (subject, predicate, obj))


# ── query terms ───────────────────────────────────────────────────────────

def content_runs(text: object) -> list[str]:
    """Runs of letters/digits/CJK left after the question scaffolding is removed."""
    folded = unicodedata.normalize("NFKC", clip(text)).casefold()
    return _RUN_RE.findall(_NOISE_RE.sub(" ", folded))


def query_grams(text: object, *, max_grams: int = 40) -> list[str]:
    """Distinct 3-character substrings of the content runs, in order.

    FTS5's trigram index finds exactly these; ORing them and ranking with bm25
    is character n-gram overlap, which stands in for word segmentation.
    """
    grams: list[str] = []
    seen: set[str] = set()
    for run in content_runs(text):
        for i in range(len(run) - 2):
            gram = run[i:i + 3]
            if gram not in seen:
                seen.add(gram)
                grams.append(gram)
                if len(grams) >= max_grams:
                    return grams
    return grams


def short_terms(text: object, *, max_weak: int = 8) -> tuple[list[str], list[str]]:
    """(strong, weak) two-character terms for the LIKE fallback.

    Strong: a whole content run of exactly two characters ("加班").  Weak: two
    character windows inside longer runs that contain no function character.
    The trigram index cannot see either.
    """
    strong: list[str] = []
    weak: list[str] = []
    seen: set[str] = set()
    for run in content_runs(text):
        if len(run) == 2:
            if run not in seen:
                seen.add(run)
                strong.append(run)
            continue
        if len(run) < 3:
            continue
        for i in range(len(run) - 1):
            pair = run[i:i + 2]
            if pair in seen or any(ch in FUNCTION_CHARS for ch in pair):
                continue
            seen.add(pair)
            weak.append(pair)
            if len(weak) >= max_weak:
                break
    return strong[:6], weak


def bigrams(text: object) -> set[str]:
    """Character bigrams of the canonical text, without function-character pairs."""
    base = canon(text)
    return {
        base[i:i + 2] for i in range(len(base) - 1)
        if not any(ch in FUNCTION_CHARS for ch in base[i:i + 2])
    }


def content_bigrams(text: object) -> set[str]:
    """Bigrams inside each content run of a question (never across runs)."""
    pairs: set[str] = set()
    for run in content_runs(text):
        for i in range(len(run) - 1):
            pair = run[i:i + 2]
            if not any(ch in FUNCTION_CHARS for ch in pair):
                pairs.add(pair)
    return pairs


def overlap(query: set[str], text: object) -> float:
    """Share of the query's bigrams found in ``text`` (0..1)."""
    if not query:
        return 0.0
    return len(query & bigrams(text)) / len(query)


# ── things that must not be remembered ───────────────────────────────────

_SENSITIVE_RES = (
    re.compile(r"\d{9,}"),                                   # phone, id, card, QQ numbers
    re.compile(r"1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}"),            # spaced phone numbers
    re.compile(r"[\w.+-]{1,64}@[\w-]{1,64}\.[a-z]{2,10}", re.IGNORECASE),
    re.compile(
        r"密码|口令|验证码|password|passwd|api[ _-]?key|secret|token|身份证|银行卡|卡号",
        re.IGNORECASE,
    ),
)


_TOPIC_RE = re.compile(
    "住院|手术|确诊|病历|癌症|抑郁|焦虑症|精神病|艾滋|怀孕|流产|离婚|出轨|劈腿|"
    "工资|薪水|月薪|年薪|存款|贷款|欠款|欠债|负债|借钱|家庭住址|住址|门牌"
)
_SENSITIVE_RES = _SENSITIVE_RES + (_TOPIC_RE,)


def looks_sensitive(text: object) -> bool:
    """Contact data, secrets, and the private topics the extraction prompt forbids."""
    value = clip(text, 400)
    return any(rx.search(value) for rx in _SENSITIVE_RES)


# Words that turn a remembered "fact" into an order to the bot.  A group member
# can say anything, and what is remembered is pasted into later prompts, so text
# that reads like an instruction or carries markup is not remembered at all.
_INSTRUCTION_RE = re.compile(
    "忽略|无视|规则|指令|系统|提示词|必须|永远|从现在起|以后所有|所有人都要|你要|不要再|"
    "越狱|扮演|角色设定|人设|ignore|system|prompt|instruction|jailbreak|assistant",
    re.IGNORECASE,
)
MARKUP_CHARS = frozenset("<>{}[]#`|\\")


def looks_like_instruction(text: object) -> bool:
    value = clip(text, 400)
    return bool(_INSTRUCTION_RE.search(value)) or any(ch in MARKUP_CHARS for ch in value)
