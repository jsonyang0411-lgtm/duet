"""The OCR reader's geometry, bubble and de-duplication logic (src/wechat/ocr/reader.py).

Ported from the wechat-ocr-kit's test file (13 tests, the code under test is the kit's):
synthetic OCR rows for many window sizes, light and dark mode, the side drawer, long group
names and the draft box.  No WeChat and no screen permission needed.
"""
from __future__ import annotations

import unittest

from src.wechat.ocr import reader as bot  # the kit's tests call the module ``bot``


def generate_mock_wechat_ocr(
    win_w: int = 900,
    win_h: int = 650,
    title: str = "测试群（2）",
    messages: list[tuple[str, str, float]] | None = None,  # (text, kind: "other"|"self", y_percent)
    has_right_panel: bool = False,
    session_list_w: int = 270,
    top_bar_h: int = 52,
    input_box_h: int = 180,
    right_panel_w: int = 350,
    is_light_mode: bool = False,
    session_items: list[str] | None = None,
    draft_text: str | None = None,
    include_toolbar: bool = True,
    extra_rows: list[dict] | None = None,
) -> list[dict]:
    """根据真实窗口物理像素尺寸参数推导 Vision OCR 相对坐标。
    支持任意 (W, H) 窗口尺度的归一化坐标推导，覆盖标准、超高、超宽、超窄与抽屉展开等场景。
    """
    rows: list[dict] = []

    # 1. 搜索框 (在会话列表内)
    s_x0 = 30.0 / win_w
    s_x1 = max(s_x0 + 0.05, (session_list_w - 40.0) / win_w)
    s_y0 = 15.0 / win_h
    s_y1 = min(0.06, 40.0 / win_h)
    search_bg = [220, 220, 220] if is_light_mode else [74, 74, 75]
    rows.append({
        "t": "搜索",
        "x0": round(s_x0, 4),
        "x1": round(s_x1, 4),
        "y0": round(s_y0, 4),
        "y1": round(s_y1, 4),
        "bg": search_bg,
    })

    # 2. 会话列表项 (在会话列表内，用于验证窄窗隔离)
    default_items = session_items if session_items is not None else ["张三", "下午开会"]
    item_y = 70.0
    for it in default_items:
        i_x0 = 40.0 / win_w
        i_x1 = (session_list_w - 20.0) / win_w
        i_y0 = item_y / win_h
        i_y1 = (item_y + 22.0) / win_h
        item_y += 30.0
        item_bg = [240, 240, 240] if is_light_mode else [40, 40, 41]
        rows.append({
            "t": it,
            "x0": round(i_x0, 4),
            "x1": round(i_x1, 4),
            "y0": round(i_y0, 4),
            "y1": round(i_y1, 4),
            "bg": item_bg,
        })

    # 3. 顶部会话标题 (在会话列表以右)
    t_x0 = (session_list_w + 25.0) / win_w
    t_x1 = t_x0 + min(len(title) * 16.0, 260.0) / win_w
    t_y0 = 15.0 / win_h
    t_y1 = min(0.065, (top_bar_h - 10.0) / win_h)
    title_bg = [240, 240, 240] if is_light_mode else [40, 40, 41]
    rows.append({
        "t": title,
        "x0": round(t_x0, 4),
        "x1": round(t_x1, 4),
        "y0": round(t_y0, 4),
        "y1": round(t_y1, 4),
        "bg": title_bg,
    })

    # 4. 右侧抽屉面板
    if has_right_panel:
        rp_x0 = (win_w - right_panel_w) / win_w
        rp_bg = [240, 240, 240] if is_light_mode else [40, 40, 41]
        rows.append({
            "t": "搜一搜",
            "x0": round(rp_x0 + 15.0 / win_w, 4),
            "x1": round(rp_x0 + 75.0 / win_w, 4),
            "y0": round(15.0 / win_h, 4),
            "y1": round(40.0 / win_h, 4),
            "bg": rp_bg,
        })
        rows.append({
            "t": "X",
            "x0": round(1.0 - 30.0 / win_w, 4),
            "x1": round(1.0 - 10.0 / win_w, 4),
            "y0": round(15.0 / win_h, 4),
            "y1": round(40.0 / win_h, 4),
            "bg": rp_bg,
        })
        rows.append({
            "t": "相关聊天记录",
            "x0": round(rp_x0 + 20.0 / win_w, 4),
            "x1": round(0.96, 4),
            "y0": round(90.0 / win_h, 4),
            "y1": round(120.0 / win_h, 4),
            "bg": rp_bg,
        })
    else:
        rows.append({
            "t": "•",
            "x0": round(1.0 - 30.0 / win_w, 4),
            "x1": round(1.0 - 10.0 / win_w, 4),
            "y0": round(15.0 / win_h, 4),
            "y1": round(40.0 / win_h, 4),
            "bg": [40, 40, 41],
        })

    # 5. 底部工具栏与输入框
    toolbar_y0 = (win_h - input_box_h) / win_h
    if include_toolbar:
        tb_x0 = (session_list_w + 20.0) / win_w
        tb_x1 = tb_x0 + 40.0 / win_w
        tb_y0 = toolbar_y0
        tb_y1 = tb_y0 + 22.0 / win_h
        tb_bg = [245, 245, 245] if is_light_mode else [30, 30, 30]
        rows.append({
            "t": "表情",
            "x0": round(tb_x0, 4),
            "x1": round(tb_x1, 4),
            "y0": round(tb_y0, 4),
            "y1": round(tb_y1, 4),
            "bg": tb_bg,
        })

    # 6. 输入框草稿 (若有)
    if draft_text:
        d_x0 = (session_list_w + 25.0) / win_w
        d_x1 = d_x0 + min(len(draft_text) * 16.0, 350.0) / win_w
        d_y0 = toolbar_y0 + 35.0 / win_h
        d_y1 = d_y0 + 22.0 / win_h
        d_bg = [255, 255, 255] if is_light_mode else [30, 30, 30]
        rows.append({
            "t": draft_text,
            "x0": round(d_x0, 4),
            "x1": round(d_x1, 4),
            "y0": round(d_y0, 4),
            "y1": round(d_y1, 4),
            "bg": d_bg,
        })

    # 7. 聊天消息气泡 (根据传入的 y_percent 精确布局)
    chat_left_px = session_list_w + 25.0
    for text, kind, y_pct in (messages or []):
        if kind == "self":
            bg = [121, 212, 163]
            m_len = min(len(text) * 16.0, 400.0)
            right_boundary_px = (win_w - right_panel_w) if has_right_panel else win_w
            m_x1 = (right_boundary_px - 40.0) / win_w
            m_x0 = m_x1 - (m_len / win_w)
        else:
            bg = [255, 255, 255] if is_light_mode else [60, 60, 60]
            m_x0 = chat_left_px / win_w
            m_x1 = m_x0 + min(len(text) * 16.0, 400.0) / win_w
        m_y0 = y_pct
        m_y1 = m_y0 + (26.0 / win_h)
        rows.append({
            "t": text,
            "x0": round(m_x0, 4),
            "x1": round(m_x1, 4),
            "y0": round(m_y0, 4),
            "y1": round(m_y1, 4),
            "bg": bg,
        })

    if extra_rows:
        rows.extend(extra_rows)

    return rows


class TestTier2BoundaryMultiScaleMatrix(unittest.TestCase):
    """Tier 2: 标准尺寸、超高大窗(80%提问)、超宽屏(x0=0.185)、极限窄窗(会话防污染)、抽屉展开/折叠、Emoji长群名、浅色草稿隔离。"""

    def test_scale_standard_900x650(self):
        """标准尺寸 (900x650) 回归测试：中间气泡与标题正常提取。"""
        mock_rows = generate_mock_wechat_ocr(
            win_w=900,
            win_h=650,
            title="测试群（2）",
            messages=[("小豆 查一下今天推广ROI", "other", 0.35)],
        )
        title = bot.chat_title(mock_rows)
        self.assertEqual(title, "测试群（2）")
        self.assertTrue(title.startswith("测试群"))

        msgs = bot.other_messages(mock_rows, want="other")
        self.assertEqual(msgs, ["小豆 查一下今天推广ROI"])

    def test_scale_ultra_tall_1000x1200_at_80_percent(self):
        """超高大窗 (1000x1200) 底部最新提问捕获测试：验证 y0=0.79 处提问 100% 捕获，对比硬编码 0.72 必失真。"""
        # 1000x1200 窗口下，输入框高度约 180px，工具栏顶位于 (1200-180)/1200 = 0.85
        # 最新提问位于 y0 = 0.79, y1 = 0.8117 (处于 80% 高度)
        mock_tall_rows = generate_mock_wechat_ocr(
            win_w=1000,
            win_h=1200,
            title="极客创新群",
            messages=[
                ("历史消息 1", "other", 0.20),
                ("小豆 昨天的销售额怎么样？", "other", 0.79),
            ],
            draft_text="这是一条未发草稿",
        )
        geom = bot.resolve_chat_geometry(mock_tall_rows)

        # 核心断言 1: chat_y1 放宽至 0.85 左右，必须完全覆盖 y0=0.79 的最新提问
        self.assertGreaterEqual(geom.chat_y1, 0.81, "chat_y1 必须覆盖 80% 高度处的最新提问")

        # 核心断言 2: other_messages 100% 捕获底部最新提问
        msgs = bot.other_messages(mock_tall_rows, want="other")
        self.assertIn("小豆 昨天的销售额怎么样？", msgs)
        self.assertNotIn("这是一条未发草稿", msgs, "输入框草稿绝不能被误吞")

        # 对照验证：原硬编码 CHAT_Y1 = 0.72 必定导致该提问丢失
        legacy_lost = [r for r in mock_tall_rows if 0.07 < r["y0"] < 0.72 and "昨天的销售额" in r["t"]]
        self.assertEqual(len(legacy_lost), 0, "旧硬编码 0.72 必然丢失 80% 处的最新提问")

    def test_scale_ultra_wide_1600x700(self):
        """超宽大屏 (1600x700) 左边界对齐测试：验证 x0=0.185 处提问不误切，对比硬编码 0.22 必误杀。"""
        # 1600x700 窗口下，会话列表仅占 16.8%，聊天气泡起始于 x0 = 0.1844
        mock_wide_rows = generate_mock_wechat_ocr(
            win_w=1600,
            win_h=700,
            title="超宽大屏测试群",
            messages=[("小豆 帮我查库存", "other", 0.20)],
        )
        geom = bot.resolve_chat_geometry(mock_wide_rows)

        # 核心断言 1: chat_x0 动态收缩至 0.15~0.17
        self.assertLess(geom.chat_x0, 0.185, "chat_x0 必须放宽至 0.185 以左")

        # 核心断言 2: 完整捕获位于 x0=0.185 的消息
        msgs = bot.other_messages(mock_wide_rows, want="other")
        self.assertEqual(msgs, ["小豆 帮我查库存"])

        # 对照验证：原硬编码 CHAT_X0 = 0.22 必定误杀该消息
        legacy_lost = [r for r in mock_wide_rows if r["x0"] > 0.22 and "帮我查库存" in r["t"]]
        self.assertEqual(len(legacy_lost), 0, "旧硬编码 0.22 必然误杀超宽屏下的气泡")

    def test_scale_narrow_window_650x500_session_isolation(self):
        """极限窄窗 (650x500) 防会话列表误侵入测试：会话列表占 40%，验证联系人严格隔离。"""
        # 650x500 窗口下，会话列表占 260px (40%)，会话项位于 x0 <= 0.37，真实聊天气泡位于 x0 = 0.4385
        mock_narrow_rows = generate_mock_wechat_ocr(
            win_w=650,
            win_h=500,
            title="极限窄窗群",
            session_list_w=260,
            session_items=["财务张三", "退款审批通知"],
            extra_rows=[
                # 模拟会话列表右侧的摘要/时间文字（位于 x0=0.25，原 CHAT_X0=0.22 会严重误读）
                {"t": "下午两点开会", "x0": 0.250, "x1": 0.360, "y0": 0.180, "y1": 0.210, "bg": [40, 40, 41]}
            ],
            messages=[("小豆 窄窗提问", "other", 0.30)],
        )
        geom = bot.resolve_chat_geometry(mock_narrow_rows)

        # 核心断言 1: chat_x0 动态推导并紧贴会话列表右界 (>= 0.38)
        self.assertGreaterEqual(geom.chat_x0, 0.38, "chat_x0 必须推导至会话列表项以右")

        # 核心断言 2: 会话列表项绝不流入消息列表，仅捕获真实提问
        msgs = bot.other_messages(mock_narrow_rows, want="other")
        self.assertEqual(msgs, ["小豆 窄窗提问"])
        for item in ["财务张三", "下午两点开会", "退款审批通知"]:
            self.assertNotIn(item, msgs)

        # 对照验证：旧硬编码 0.22 会把会话列表右侧内容（x0=0.25）当作聊天气泡读入
        legacy_leak = [r for r in mock_narrow_rows if r["x0"] > 0.22 and r["t"] == "下午两点开会"]
        self.assertGreater(len(legacy_leak), 0, "旧硬编码 0.22 会导致会话项误侵入")

    def test_scale_drawer_expanded_vs_collapsed(self):
        """右侧抽屉展开（占用 350px）vs 折叠自适应测试。"""
        # 1. 展开状态测试
        mock_expanded = generate_mock_wechat_ocr(
            win_w=900,
            win_h=650,
            title="抽屉测试群",
            has_right_panel=True,
            right_panel_w=350,
            messages=[("小豆 查数据", "other", 0.30)],
        )
        geom_exp = bot.resolve_chat_geometry(mock_expanded)
        self.assertLessEqual(geom_exp.chat_x1, 0.63, "展开抽屉时 chat_x1 必须紧凑截断在抽屉左侧")

        msgs_exp = bot.other_messages(mock_expanded, want="other")
        self.assertEqual(msgs_exp, ["小豆 查数据"])
        self.assertNotIn("搜一搜", msgs_exp)
        self.assertNotIn("相关聊天记录", msgs_exp)

        # 2. 折叠状态测试
        mock_collapsed = generate_mock_wechat_ocr(
            win_w=900,
            win_h=650,
            title="抽屉测试群",
            has_right_panel=False,
            messages=[("小豆 查数据", "other", 0.30)],
        )
        geom_col = bot.resolve_chat_geometry(mock_collapsed)
        self.assertGreaterEqual(geom_col.chat_x1, 0.95, "抽屉折叠时 chat_x1 应扩展至右边缘")

    def test_scale_long_emoji_group_title(self):
        """Emoji 长群名与人数统计后缀清洗验证。"""
        # 1. clean_group_name 清洗末尾人数统计后缀
        raw_titles = [
            ("🚀 极客创新与核心业务群（32人）", "🚀 极客创新与核心业务群"),
            ("🔥 研发攻坚小组 (100人)", "🔥 研发攻坚小组"),
            ("微信运营交流群 (2)", "微信运营交流群"),
            ("测试大群（2）", "测试大群"),
            ("无后缀正常群名", "无后缀正常群名"),
        ]
        for raw, expected in raw_titles:
            self.assertEqual(bot.clean_group_name(raw), expected)

        # 2. CleanedTitle 双向匹配与 startswith 支持
        cleaned_obj = bot.CleanedTitle("🚀 极客创新与核心业务群（32人）")
        self.assertEqual(cleaned_obj, "🚀 极客创新与核心业务群（32人）")
        self.assertEqual(cleaned_obj, "🚀 极客创新与核心业务群")
        self.assertTrue(cleaned_obj.startswith("🚀 极客创新"))
        self.assertTrue(cleaned_obj.startswith("🚀 极客创新与核心业务群"))

        # 3. OCR 模拟提取
        mock_rows = generate_mock_wechat_ocr(
            win_w=1000,
            win_h=700,
            title="🚀 极客创新与核心业务群（32人）",
            messages=[("小豆 大家好", "other", 0.30)],
        )
        extracted_title = bot.chat_title(mock_rows)
        self.assertEqual(extracted_title.cleaned, "🚀 极客创新与核心业务群")
        self.assertTrue(extracted_title.startswith("🚀 极客创新"))

    def test_scale_light_mode_draft_input_box_isolated(self):
        """浅色模式纯白输入框草稿文字严格隔离防误吞。"""
        mock_light_rows = generate_mock_wechat_ocr(
            win_w=900,
            win_h=650,
            title="浅色模式测试群",
            is_light_mode=True,
            messages=[("小豆 今天天气怎么样？", "other", 0.50)],
            draft_text="小豆 帮我清空所有数据库",
        )
        msgs = bot.other_messages(mock_light_rows, want="other")
        self.assertEqual(msgs, ["小豆 今天天气怎么样？"])
        self.assertNotIn("小豆 帮我清空所有数据库", msgs, "输入框白色草稿绝不能被当作提问误吞")


class TestAdversarialGeometryAndScale(unittest.TestCase):
    """对抗测试：极端多尺度窗口、超高大屏(H>=2000px)、超宽大屏(W>=2560px)、浅色草稿隔离与密集联系人。"""

    def test_adversarial_extreme_tall_windows(self):
        """测试 500x2500, 800x2000, 600x2400, 1000x3000 等极端超高窗口下，底部最新提问(88%~92%高度)能否被 100% 捕获。"""
        tall_configs = [
            (500, 2500, 180, 0.90, "500x2500 极限极窄超高窗，提问在 90% 高度"),
            (800, 2000, 160, 0.88, "800x2000 超高窗，提问在 88% 高度"),
            (600, 2400, 150, 0.91, "600x2400 超高窗，提问在 91% 高度"),
            (1000, 3000, 180, 0.92, "1000x3000 3K垂直带状屏，提问在 92% 高度"),
        ]

        failures = []
        for win_w, win_h, input_h, q_y_pct, desc in tall_configs:
            q_text = f"小豆 极限高窗测试 {win_w}x{win_h} y={q_y_pct}"
            rows = generate_mock_wechat_ocr(
                win_w=win_w,
                win_h=win_h,
                title="超高极限测试群",
                input_box_h=input_h,
                messages=[(q_text, "other", q_y_pct)],
                include_toolbar=True,
            )

            geom = bot.resolve_chat_geometry(rows)
            msgs = bot.other_messages(rows, geometry=geom)

            if q_text not in msgs:
                failures.append({
                    "config": (win_w, win_h, q_y_pct),
                    "desc": desc,
                    "geom": geom,
                    "extracted": msgs,
                })

        self.assertEqual(len(failures), 0, f"发现超高窗口下气泡遗漏漏洞！共 {len(failures)} 处失真: {failures}")

    def test_adversarial_extreme_wide_windows(self):
        """测试 3000x500, 2560x600, 3840x800 等极端超宽窗口下（含无搜索栏与会话列表极窄场景），左边界自适应性。"""
        wide_configs = [
            (3000, 500, 270, True, "3000x500 带搜索栏标准会话列"),
            (3000, 500, 220, False, "3000x500 无搜索栏(OCR丢失)极窄会话列(x0=0.073)"),
            (3840, 800, 250, True, "3840x800 4K带状超宽屏(x0=0.065)"),
            (2560, 600, 200, False, "2560x600 2K超宽屏无搜索栏(x0=0.078)"),
        ]

        failures = []
        for win_w, win_h, session_w, has_search, desc in wide_configs:
            q_text = f"小豆 超宽窗口测试 {win_w}x{win_h}"
            rows = generate_mock_wechat_ocr(
                win_w=win_w,
                win_h=win_h,
                title="超宽极限测试群",
                session_list_w=session_w,
                messages=[(q_text, "other", 0.40)],
            )
            if not has_search:
                rows = [r for r in rows if "搜索" not in r["t"]]

            geom = bot.resolve_chat_geometry(rows)
            msgs = bot.other_messages(rows, geometry=geom)

            if q_text not in msgs:
                failures.append({
                    "config": (win_w, win_h, session_w, has_search),
                    "desc": desc,
                    "geom": geom,
                    "extracted": msgs,
                })

        self.assertEqual(len(failures), 0, f"发现超宽窗口下气泡遗漏漏洞！共 {len(failures)} 处失真: {failures}")

    def test_adversarial_light_mode_draft_isolation(self):
        """测试浅色模式下，当工具栏为纯图标无文字 OCR 时，多行纯白草稿文字物理切除隔离。"""
        scenarios = [
            (True, "小豆 这是标准带表情工具栏的草稿", [], "浅色模式带文字工具栏草稿"),
            (False, "小豆 纯图标工具栏(无文字OCR)单行草稿", [], "浅色模式纯图标工具栏单行草稿"),
            (False, "小豆 纯图标工具栏多行草稿第一行", [
                {"t": "草稿第二行：查看大盘数据", "x0": 0.35, "x1": 0.60, "y0": 0.82, "y1": 0.85, "bg": [255, 255, 255]},
                {"t": "草稿第三行：确认", "x0": 0.35, "x1": 0.50, "y0": 0.86, "y1": 0.89, "bg": [255, 255, 255]},
            ], "浅色模式纯图标工具栏多行草稿干扰"),
        ]

        failures = []
        for inc_tb, draft, extras, desc in scenarios:
            real_msg = "小豆 历史正常气泡提问"
            rows = generate_mock_wechat_ocr(
                win_w=900,
                win_h=650,
                title="浅色草稿对抗测试群",
                is_light_mode=True,
                include_toolbar=inc_tb,
                draft_text=draft,
                extra_rows=extras,
                messages=[(real_msg, "other", 0.35)],
            )

            geom = bot.resolve_chat_geometry(rows)
            msgs = bot.other_messages(rows, geometry=geom)

            draft_leaked = any(draft in m for m in msgs)
            extras_leaked = any(any(ex["t"] in m for m in msgs) for ex in extras)

            if draft_leaked or extras_leaked:
                failures.append({
                    "desc": desc,
                    "inc_tb": inc_tb,
                    "draft": draft,
                    "leaked_msgs": [m for m in msgs if m != real_msg],
                    "geom": geom,
                })

        self.assertEqual(len(failures), 0, f"发现浅色模式草稿误吞漏洞！共 {len(failures)} 处泄漏: {failures}")

    def test_adversarial_dense_sessions_and_unicode(self):
        """测试 40+ 密集联系人及 Emoji 特殊 Unicode 群名清洗与聊天消息隔离。"""
        complex_titles = [
            "🚀【豆皮科技】核心VIP数据大盘作战室 🌟✨🎉 (256人)",
            "🔥运营大群🚀 (999+)",
            "电商交流群 [32人]",
            "技术支持组(5)",
            "Data_Ops·数据运营小组（100人）",
            "🤖 小豆测试：指令集群 ⚙️",
        ]

        dense_items = [
            f"会话联系人_{i}_{'🔥' if i % 2 == 0 else '📊'}"
            for i in range(40)
        ]

        failures = []
        for title in complex_titles:
            q_text = f"小豆 针对群 {title[:6]} 的提问"
            rows = generate_mock_wechat_ocr(
                win_w=1200,
                win_h=800,
                title=title,
                session_items=dense_items,
                messages=[(q_text, "other", 0.45)],
            )

            geom = bot.resolve_chat_geometry(rows)
            extracted_title = bot.chat_title(rows, geometry=geom)
            msgs = bot.other_messages(rows, geometry=geom)

            raw_match = (extracted_title == title)
            clean_match = extracted_title.startswith(bot.clean_group_name(title))
            msg_ok = (q_text in msgs)
            leaked_contacts = [m for m in msgs if any(c in m for c in dense_items)]

            if not (raw_match or clean_match) or not msg_ok or leaked_contacts:
                failures.append({
                    "title": title,
                    "extracted_title": str(extracted_title),
                    "raw_match": raw_match,
                    "clean_match": clean_match,
                    "msg_ok": msg_ok,
                    "leaked_contacts": leaked_contacts,
                })

        self.assertEqual(len(failures), 0, f"密集会话项与 Unicode 测试失败: {failures}")


class TestDedupAndNormalize(unittest.TestCase):
    def test_normalize_ignores_spaces_and_punctuation(self):
        self.assertEqual(bot.normalize_text("Hello， World！ 你 好"), bot.normalize_text("hello,world你好"))

    def test_dedup_sliding_window(self):
        d = bot.MessageDeduplicator(max_size=3)
        for k in "abcd":
            d.add(k)
        self.assertFalse(d.is_handled("a"))   # 最早的被淘汰
        self.assertTrue(d.is_handled("b", "x"))
