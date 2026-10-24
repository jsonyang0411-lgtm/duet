#!/usr/bin/env python3
"""把达人商务智能体的备忘录（Markdown）转成一个能离线打开的网页。

备忘录以 docs/达人商务智能体_架构设计与落地备忘录.md 为准，网页是从它生成的，
不要直接改网页。改了备忘录以后重新生成（tests/test_memo_html.py 会检查两者是否一致）::

    python3 tools/memo_html.py

只认备忘录里用到的写法：标题、段落、引用、无序和有序列表、表格、代码块、分隔线，
以及行内的代码、粗体、网址和核实标注〔…〕。只用标准库，网页不引用任何外部资源。
"""

from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "达人商务智能体_架构设计与落地备忘录.md"
TARGET = SOURCE.with_suffix(".html")

_URL = re.compile(r"https?://[^\s〔〕（）()，。、；\"<>]+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CHECK = re.compile(r"〔([^〕]+)〕")
_HEADING = re.compile(r"(#{1,3})\s+(.*)")
_BULLET = re.compile(r"[-*]\s+")
_NUMBERED = re.compile(r"\d+\.\s+")


def inline(text: str) -> str:
    """行内格式。核实标注〔日期 · 方式〕显示成小标签，没验证过的用醒目的颜色；
    标注里也可能有代码（文件名），所以先找出整个标注，再处理里面的内容。"""
    out, pos = [], 0
    for match in _CHECK.finditer(text):
        out.append(_plain(text[pos:match.start()]))
        kind = " warn" if "未验证" in match.group(1) else ""
        out.append(f'<span class="check{kind}">{_plain(match.group(1))}</span>')
        pos = match.end()
    out.append(_plain(text[pos:]))
    return "".join(out)


def _plain(text: str) -> str:
    """按反引号切开：代码部分原样转义，其余部分处理粗体和网址。"""
    out = []
    for index, part in enumerate(text.split("`")):
        if index % 2:
            out.append(f"<code>{html.escape(part)}</code>")
            continue
        part = html.escape(part, quote=False)
        part = _BOLD.sub(r"<strong>\1</strong>", part)
        out.append(_URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', part))
    return "".join(out)


def _starts_block(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith(("```", "#", ">", "|"))
        or stripped == "---"
        or bool(_BULLET.match(stripped) or _NUMBERED.match(stripped))
    )


def _cells(row: str) -> list:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _table(rows: list) -> str:
    header, body = rows[0], rows[1:]
    if body and set(body[0].replace("|", "").strip()) <= set("-: "):
        body = body[1:]  # 表头下面的分隔行
    head = "".join(f"<th>{inline(cell)}</th>" for cell in _cells(header))
    lines = "".join(
        "<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in _cells(row)) + "</tr>" for row in body
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{lines}</tbody></table></div>'


def convert(markdown: str):
    """返回 (一级标题, 正文 HTML, 目录)。目录是 [(锚点, 二级标题)]。"""
    lines = markdown.splitlines()
    title, body, toc = "", [], []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
        elif stripped.startswith("```"):
            end = i + 1
            while end < len(lines) and not lines[end].strip().startswith("```"):
                end += 1
            body.append("<pre><code>" + html.escape("\n".join(lines[i + 1:end])) + "</code></pre>")
            i = end + 1
        elif stripped == "---":
            body.append("<hr>")
            i += 1
        elif _HEADING.match(stripped):
            marks, text = _HEADING.match(stripped).groups()
            if len(marks) == 1:
                title = text
            elif len(marks) == 2:
                anchor = f"s{len(toc) + 1}"
                toc.append((anchor, text))
                body.append(f'<h2 id="{anchor}">{inline(text)}</h2>')
            else:
                body.append(f"<h3>{inline(text)}</h3>")
            i += 1
        elif stripped.startswith(">"):
            quoted = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quoted.append(lines[i].strip()[1:].strip())
                i += 1
            body.append("<blockquote>" + "".join(f"<p>{inline(q)}</p>" for q in quoted if q) + "</blockquote>")
        elif stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            body.append(_table(rows))
        elif _BULLET.match(stripped) or _NUMBERED.match(stripped):
            marker = _NUMBERED if _NUMBERED.match(stripped) else _BULLET
            items = []
            while i < len(lines) and marker.match(lines[i].strip()):
                items.append(marker.sub("", lines[i].strip(), count=1))
                i += 1
            tag = "ol" if marker is _NUMBERED else "ul"
            body.append(f"<{tag}>" + "".join(f"<li>{inline(item)}</li>" for item in items) + f"</{tag}>")
        else:
            paragraph = []
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                paragraph.append(lines[i].strip())
                i += 1
            body.append(f"<p>{inline(''.join(paragraph))}</p>")
    return title, "\n".join(body), toc


STYLE = """
:root {
  --bg: #f6f5f1; --card: #ffffff; --fg: #1f2328; --muted: #5b616b; --line: #d9d7d0;
  --head: #efede7; --accent: #0b6e99; --accent-bg: #e7f2f7;
  --check-bg: #ecebe6; --check-fg: #4a4f57; --warn-bg: #fbecd2; --warn-fg: #8a5300;
  --code-bg: #efede7;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15171a; --card: #1c1f23; --fg: #e4e6e9; --muted: #9aa1ab; --line: #33373d;
    --head: #23272c; --accent: #5bb8e3; --accent-bg: #16303c;
    --check-bg: #2a2e34; --check-fg: #b8bec7; --warn-bg: #3a2c12; --warn-fg: #f0c06a;
    --code-bg: #262a30;
  }
}
:root[data-theme="dark"] {
  --bg: #15171a; --card: #1c1f23; --fg: #e4e6e9; --muted: #9aa1ab; --line: #33373d;
  --head: #23272c; --accent: #5bb8e3; --accent-bg: #16303c;
  --check-bg: #2a2e34; --check-fg: #b8bec7; --warn-bg: #3a2c12; --warn-fg: #f0c06a;
  --code-bg: #262a30;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.75 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
    "Microsoft YaHei", "Noto Sans SC", sans-serif;
}
.layout { display: grid; grid-template-columns: 240px minmax(0, 860px); gap: 40px; justify-content: center; padding: 40px 24px 80px; }
nav.toc { position: sticky; top: 24px; align-self: start; font-size: 14px; }
nav.toc .label { font-weight: 600; color: var(--muted); margin-bottom: 8px; }
nav.toc ol { list-style: none; margin: 0; padding: 0; border-left: 2px solid var(--line); }
nav.toc a { display: block; padding: 4px 0 4px 12px; color: var(--muted); text-decoration: none; line-height: 1.5; }
nav.toc a:hover { color: var(--accent); }
main { min-width: 0; }
.title-row { display: flex; align-items: flex-start; gap: 16px; }
h1 { flex: 1; font-size: 28px; line-height: 1.35; margin: 0 0 20px; }
#theme { flex: none; margin-top: 4px; padding: 4px 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--card); color: var(--muted); font: inherit; font-size: 13px; cursor: pointer; }
h2 { font-size: 22px; line-height: 1.4; margin: 44px 0 14px; padding-bottom: 8px; border-bottom: 1px solid var(--line); scroll-margin-top: 16px; }
h3 { font-size: 18px; margin: 28px 0 10px; }
p { margin: 10px 0; }
a { color: var(--accent); word-break: break-all; }
strong { font-weight: 650; }
hr { display: none; }
ul, ol { padding-left: 1.4em; margin: 10px 0; }
li { margin: 6px 0; }
blockquote { margin: 16px 0; padding: 10px 16px; background: var(--accent-bg); border-left: 4px solid var(--accent); border-radius: 0 6px 6px 0; }
blockquote p { margin: 4px 0; }
code { font: 0.88em/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--code-bg); padding: 1px 5px; border-radius: 4px; word-break: break-all; }
pre { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; overflow-x: auto; }
pre code { background: none; padding: 0; font-size: 13px; line-height: 1.6; word-break: normal; white-space: pre; }
.table-wrap { overflow-x: auto; margin: 14px 0; border: 1px solid var(--line); border-radius: 8px; background: var(--card); }
table { width: 100%; border-collapse: collapse; font-size: 14px; line-height: 1.65; }
th, td { text-align: left; vertical-align: top; padding: 9px 12px; border-bottom: 1px solid var(--line); }
th { background: var(--head); font-weight: 600; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
.check { display: inline-block; margin: 0 2px; padding: 0 7px; border-radius: 4px; background: var(--check-bg); color: var(--check-fg); font-size: 0.82em; line-height: 1.7; }
.check.warn { background: var(--warn-bg); color: var(--warn-fg); }
footer { margin-top: 56px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; }
@media (max-width: 1100px) {
  .layout { grid-template-columns: minmax(0, 860px); gap: 0; padding: 24px 16px 60px; }
  nav.toc { position: static; margin-bottom: 8px; padding: 12px 16px; background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
  nav.toc ol { border-left: 0; columns: 2; column-gap: 24px; }
  nav.toc a { padding: 3px 0; }
}
@media (max-width: 600px) {
  h1 { font-size: 22px; }
  nav.toc ol { columns: 1; }
  table { min-width: 560px; }
}
@media print {
  nav.toc, #theme { display: none; }
  .layout { display: block; padding: 0; }
  body { background: #fff; }
}
"""

SCRIPT = """
(function () {
  var root = document.documentElement, key = 'memo-theme', button = document.getElementById('theme');
  try { var saved = localStorage.getItem(key); if (saved) root.setAttribute('data-theme', saved); } catch (e) {}
  button.addEventListener('click', function () {
    var current = root.getAttribute('data-theme');
    var dark = current ? current === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
    try { localStorage.setItem(key, dark ? 'light' : 'dark'); } catch (e) {}
  });
})();
"""


def render(markdown: str) -> str:
    title, body, toc = convert(markdown)
    links = "".join(f'<li><a href="#{anchor}">{inline(text)}</a></li>' for anchor, text in toc)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>达人商务智能体备忘录</title>
<style>{STYLE}</style>
</head>
<body>
<div class="layout">
<nav class="toc" aria-label="目录"><div class="label">目录</div><ol>{links}</ol></nav>
<main>
<div class="title-row"><h1>{inline(title)}</h1><button type="button" id="theme" title="切换浅色 / 深色">深浅色</button></div>
{body}
<footer>这个网页由 <code>tools/memo_html.py</code> 从 <code>docs/{SOURCE.name}</code> 生成，内容以 Markdown 原文为准，不要直接修改网页。</footer>
</main>
</div>
<script>{SCRIPT}</script>
</body>
</html>
"""


def main() -> None:
    # 统一用 \n 换行，Windows 上生成的文件和 Mac 上的一样
    with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(SOURCE.read_text(encoding="utf-8")))
    print(f"已生成：{TARGET}")


if __name__ == "__main__":
    main()
