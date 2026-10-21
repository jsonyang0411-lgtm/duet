"""Lightweight web search support for conversational replies."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import importlib
import json
import logging
import re
import ssl
import time
from urllib.parse import parse_qs, quote_plus, urlencode, unquote, urlparse
from urllib.request import Request, urlopen

try:
    import certifi
except ImportError:  # pragma: no cover - requirements installs certifi
    certifi = None

logger = logging.getLogger(__name__)


def _https_context() -> ssl.SSLContext:
    """Use a bundled CA bundle when Windows' Python store is incomplete."""
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""


class _DuckDuckGoParser(HTMLParser):
    def __init__(self, max_results: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_results = max_results
        self.results: list[WebSearchResult] = []
        self._title_parts: list[str] | None = None
        self._snippet_parts: list[str] | None = None
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        classes = attr.get("class", "")
        if tag == "a" and (
            "result-link" in classes
            or "result__a" in classes
        ):
            self._title_parts = []
            self._href = attr.get("href", "")
        elif tag in {"td", "span", "a"} and (
            "result-snippet" in classes
            or "result__snippet" in classes
        ):
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._title_parts is not None:
            title = _clean_space("".join(self._title_parts))
            url = _normalize_duckduckgo_url(self._href)
            if title and url and len(self.results) < self.max_results:
                self.results.append(WebSearchResult(title=title, url=url))
            self._title_parts = None
            self._href = ""
        if tag in {"td", "span", "a"} and self._snippet_parts is not None:
            snippet = _clean_space("".join(self._snippet_parts))
            if snippet and self.results and not self.results[-1].snippet:
                self.results[-1].snippet = snippet
            self._snippet_parts = None


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(str(text or ""))).strip()


def _normalize_duckduckgo_url(url: str) -> str:
    url = unescape(str(url or "").strip())
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://duckduckgo.com" + url
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return unquote(query["uddg"][0])
    return url


def _at(value, index: int):
    if isinstance(value, list) and 0 <= index < len(value):
        return value[index]
    return None


def _weather_code_desc(code) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "天气情况未知"
    mapping = {
        0: "晴",
        1: "大部晴朗",
        2: "局部多云",
        3: "阴",
        45: "有雾",
        48: "雾凇",
        51: "小毛毛雨",
        53: "毛毛雨",
        55: "较强毛毛雨",
        56: "冻毛毛雨",
        57: "较强冻毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        66: "冻雨",
        67: "较强冻雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        77: "雪粒",
        80: "阵雨",
        81: "较强阵雨",
        82: "强阵雨",
        85: "阵雪",
        86: "强阵雪",
        95: "雷暴",
        96: "雷暴伴小冰雹",
        99: "雷暴伴强冰雹",
    }
    return mapping.get(code, "天气情况未知")


class WebSearchClient:
    """Search wrapper with local trigger detection."""

    _NEGATIVE_RE = re.compile(r"(别搜|不要搜|不用搜|别联网|不要联网|不用联网)")
    _EXPLICIT_RE = re.compile(
        r"(帮我查一下|帮我搜一下|搜一下|搜索一下|查一下|搜索|帮我搜|帮我查|查查|查个|百度一下|联网查|联网搜|网上查|网上搜|查资料)"
    )
    _CURRENT_STRONG_RE = re.compile(
        r"(最新|实时|新闻|公告|价格|汇率|天气|赛程|版本|活动|上线|开服|更新|上映|发售)"
    )
    _CURRENT_TIME_RE = re.compile(r"(今天|今日|现在|目前|刚刚)")
    _PERSONAL_NOW_RE = re.compile(
        r"(你|我|咱|我们|心情|吃饭|睡觉|在干嘛|干嘛呢|忙|累|困)"
    )
    _CASUAL_NOW_RE = re.compile(
        r"^(你)?(现在|目前)?(在吗|在不在|在干嘛|干嘛呢|忙吗|睡了吗)[？?。！!]*$"
    )
    _FILLER_RE = re.compile(
        r"^(麻烦你|麻烦|帮我|帮忙|可以|能不能|能帮我|请你|请|你)?"
    )
    _WEATHER_RE = re.compile(r"(天气|气温|温度|预报|冷不冷|热不热|下雨|降雨)")

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_results: int = 4,
        timeout_sec: float = 8.0,
        region: str = "cn-zh",
        reply_max_chars: int = 120,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_results = max(1, min(8, int(max_results)))
        self.timeout_sec = max(1.0, min(20.0, float(timeout_sec)))
        self.region = region or "cn-zh"
        self.reply_max_chars = max(40, min(300, int(reply_max_chars)))

    @classmethod
    def from_config(cls, config) -> "WebSearchClient":
        return cls(
            enabled=bool(getattr(config, "web_search_enabled", True)),
            max_results=int(getattr(config, "web_search_max_results", 4)),
            timeout_sec=float(getattr(config, "web_search_timeout_sec", 8.0)),
            region=str(getattr(config, "web_search_region", "cn-zh") or "cn-zh"),
            reply_max_chars=int(getattr(config, "web_search_reply_max_chars", 120)),
        )

    def extract_query(self, text: str, *, bot_name: str = "") -> str:
        if not self.enabled:
            return ""

        original = _clean_space(text)
        if not original or self._NEGATIVE_RE.search(original):
            return ""
        if self._CASUAL_NOW_RE.fullmatch(original):
            return ""

        normalized = original
        if bot_name:
            normalized = normalized.replace(f"@{bot_name}", "").replace(bot_name, "")
        normalized = _clean_space(normalized)

        explicit = self._EXPLICIT_RE.search(normalized)
        strong_current = self._CURRENT_STRONG_RE.search(normalized)
        time_current = self._CURRENT_TIME_RE.search(normalized)
        currentish = bool(strong_current or time_current)
        if not explicit and not currentish:
            return ""

        if not explicit and self._PERSONAL_NOW_RE.search(normalized):
            return ""

        query = self._EXPLICIT_RE.sub(" ", normalized)
        query = self._FILLER_RE.sub("", query)
        query = re.sub(r"^\s*(一下|一下子)\s*", "", query)
        query = re.sub(r"(是什么|是啥|怎么样|如何|吗|呢|呀|啊)[？?。！!]*$", "", query)
        query = _clean_space(query.strip(" ，,。？?！!：:；;"))
        if len(query) < 2:
            return ""
        return query[:120]

    def search(self, query: str) -> list[WebSearchResult]:
        query = _clean_space(query)
        if not query:
            return []

        start = time.monotonic()
        weather_results = self._search_weather(query)
        if weather_results:
            logger.info(
                "Weather lookup: query=%r results=%d elapsed=%.2fs",
                query, len(weather_results), time.monotonic() - start,
            )
            return weather_results[:self.max_results]

        for searcher in (self._search_with_ddgs, self._search_duckduckgo_html):
            try:
                results = searcher(query)
                if results:
                    logger.info(
                        "Web search: query=%r results=%d elapsed=%.2fs",
                        query, len(results), time.monotonic() - start,
                    )
                    return results[:self.max_results]
            except Exception as e:
                logger.debug("Web search provider failed: %s", e)
        logger.warning("Web search returned no results: %r", query)
        return []

    def _search_weather(self, query: str) -> list[WebSearchResult]:
        if not self._WEATHER_RE.search(query):
            return []

        day_offset, day_label = self._extract_weather_day(query)
        location = self._extract_weather_location(query)
        if not location:
            return [
                WebSearchResult(
                    title="天气查询需要城市",
                    url="https://open-meteo.com/",
                    snippet="用户询问天气但没有提供城市；请简短反问要查哪个城市。",
                )
            ]

        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
            "name": location,
            "count": 1,
            "language": "zh",
            "format": "json",
        })
        geo = self._get_json(geo_url)
        places = geo.get("results") or []
        if not places:
            return []

        place = places[0]
        lat = place.get("latitude")
        lon = place.get("longitude")
        display_name = place.get("name") or location
        country = place.get("country") or ""
        admin = place.get("admin1") or ""
        if lat is None or lon is None:
            return []

        weather_url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
            "latitude": lat,
            "longitude": lon,
            "current": ",".join([
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "weather_code",
                "wind_speed_10m",
            ]),
            "daily": ",".join([
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
            ]),
            "timezone": "auto",
            "forecast_days": max(1, min(4, day_offset + 1)),
        })
        weather = self._get_json(weather_url)
        current = weather.get("current") or {}
        daily = weather.get("daily") or {}

        now_desc = _weather_code_desc(current.get("weather_code"))
        day_desc = _weather_code_desc(_at(daily.get("weather_code"), day_offset))
        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        high = _at(daily.get("temperature_2m_max"), day_offset)
        low = _at(daily.get("temperature_2m_min"), day_offset)
        rain = _at(daily.get("precipitation_probability_max"), day_offset)

        place_label = " ".join(
            part for part in (country, admin, display_name)
            if part and part != display_name
        )
        if place_label:
            place_label = f"{place_label} {display_name}"
        else:
            place_label = str(display_name)

        parts = [f"{place_label}{day_label}天气：{day_desc or now_desc}"]
        if day_offset == 0 and temp is not None:
            parts.append(f"当前{temp}°C")
        if day_offset == 0 and feels is not None:
            parts.append(f"体感{feels}°C")
        if high is not None and low is not None:
            parts.append(f"{low}~{high}°C")
        if day_offset == 0 and humidity is not None:
            parts.append(f"湿度{humidity}%")
        if rain is not None:
            parts.append(f"降雨概率{rain}%")
        if day_offset == 0 and wind is not None:
            parts.append(f"风速{wind}km/h")

        return [
            WebSearchResult(
                title=f"{place_label}{day_label}天气",
                url="https://open-meteo.com/",
                snippet="，".join(parts) + "。数据源：Open-Meteo。",
            )
        ]

    def _extract_weather_day(self, query: str) -> tuple[int, str]:
        text = str(query or "")
        if "大后天" in text:
            return 3, "大后天"
        if "后天" in text:
            return 2, "后天"
        if "明天" in text or "明日" in text:
            return 1, "明天"
        return 0, "今日"

    def _extract_weather_location(self, query: str) -> str:
        text = _clean_space(query)
        text = re.sub(
            r"(今天|今日|明天|明日|后天|大后天|现在|目前|当地|本地|实时|天气|气温|温度|预报|怎么样|如何|冷不冷|热不热|会不会|下雨|降雨|查一下|查查|的)",
            " ",
            text,
        )
        text = _clean_space(text.strip(" ，,。？?！!：:；;"))
        return text[:80]

    def _get_json(self, url: str) -> dict:
        request = Request(
            url,
            headers={
                "User-Agent": "webot/1.0 (+https://github.com/cancelGuMu/webot)",
                "Accept": "application/json",
            },
        )
        with urlopen(
            request,
            timeout=self.timeout_sec,
            context=_https_context(),
        ) as response:
            raw = response.read(256_000).decode("utf-8", errors="replace")
        return json.loads(raw)

    def format_for_prompt(self, query: str, results: list[WebSearchResult]) -> str:
        if not results:
            return (
                f"联网搜索请求：{query}\n"
                "联网搜索结果：没有搜到可靠结果。回答时不要编造实时信息，可以说没查到。"
            )

        lines = [
            f"联网搜索请求：{query}",
            "联网搜索结果（这些是刚搜到的资料，请优先依据它们回答；不要编造结果里没有的细节）：",
        ]
        for i, item in enumerate(results[:self.max_results], start=1):
            source = urlparse(item.url).netloc.replace("www.", "")
            snippet = item.snippet[:160].rstrip()
            line = f"{i}. {item.title}"
            if source:
                line += f"｜来源：{source}"
            if snippet:
                line += f"｜摘要：{snippet}"
            lines.append(line)
        lines.append("请用人设口吻简短回答；如果用户要来源，可以提来源名。")
        return "\n".join(lines)

    def _search_with_ddgs(self, query: str) -> list[WebSearchResult]:
        ddgs_cls = None
        for module_name in ("ddgs", "duckduckgo_search"):
            try:
                module = importlib.import_module(module_name)
                ddgs_cls = getattr(module, "DDGS")
                break
            except Exception:
                continue
        if ddgs_cls is None:
            return []

        with ddgs_cls() as ddgs:
            rows = ddgs.text(
                query,
                region=self.region,
                safesearch="moderate",
                max_results=self.max_results,
            )
            return [
                WebSearchResult(
                    title=_clean_space(row.get("title", "")),
                    url=_normalize_duckduckgo_url(row.get("href") or row.get("url") or ""),
                    snippet=_clean_space(row.get("body") or row.get("snippet") or ""),
                )
                for row in rows or []
                if row.get("title") and (row.get("href") or row.get("url"))
            ]

    def _search_duckduckgo_html(self, query: str) -> list[WebSearchResult]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        with urlopen(request, timeout=self.timeout_sec) as response:
            html = response.read(512_000).decode("utf-8", errors="replace")
        parser = _DuckDuckGoParser(self.max_results)
        parser.feed(html)
        return parser.results
