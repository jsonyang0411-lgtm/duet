"""备忘录网页（tools/memo_html.py）：网页要跟 Markdown 原文保持一致，格式要转换完整。"""

import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "memo_html.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("memo_html", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_page_matches_the_memo():
    tool = load_tool()
    expected = tool.render(tool.SOURCE.read_text(encoding="utf-8"))
    assert tool.TARGET.read_text(encoding="utf-8") == expected, (
        "改了备忘录以后要重新生成网页：python3 tools/memo_html.py"
    )


def test_every_table_heading_and_mark_is_converted():
    tool = load_tool()
    source = tool.SOURCE.read_text(encoding="utf-8")
    page = tool.render(source)
    separators = sum(1 for line in source.splitlines() if line.startswith("|---"))
    assert page.count("<table>") == separators
    assert page.count("<h2 ") == sum(1 for line in source.splitlines() if line.startswith("## "))
    for leftover in ("**", "〔", "|---", "\n## ", "\n> "):
        assert leftover not in page, f"网页里还留着没转换的 Markdown：{leftover!r}"


def test_text_is_escaped_and_marks_without_verification_stand_out():
    tool = load_tool()
    assert tool.inline("<b>粗</b> `a<b>` **重点**") == (
        "&lt;b&gt;粗&lt;/b&gt; <code>a&lt;b&gt;</code> <strong>重点</strong>"
    )
    assert 'class="check warn"' in tool.inline("〔告知，未验证〕")
    assert tool.inline("〔10-25 · 测试 `tests/a.py`〕") == (
        '<span class="check">10-25 · 测试 <code>tests/a.py</code></span>'
    )
