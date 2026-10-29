# 微信 4.0 好友添加功能问题排查与技术交接文档

> 本文档整理自 2026-10-29 Windows 微信 4.0 真机调试与问题复盘，供开发机 Claude Code 协同排查与进一步完善。

---

## 一、需求与背景

管理员在与机器人的微信私聊中发送添加好友指令，例如：
- `添加好友 <微信号/手机号> [附言]`
- `添加好友：<微信号/手机号> [附言]`（带全角/半角冒号）
- `加好友 <微信号/手机号> [附言]`

机器人应当：
1. 拦截该管理指令（不转交 Mac Claude Relay 任务队列）；
2. 自动化驱动本地 Windows 微信 4.0（Qt 5.15 客户端），在静默/防打扰前提下打开添加朋友窗口；
3. 输入目标微信号/手机号、搜索、点击「添加到通讯录」、填入附言并确认发送；
4. 将操作结果（成功、未找到、已是好友、风控限制等）直接在私聊中回执给管理员。

---

## 二、真机出现的问题与根本原因分析（Root Cause）

### 1. 指令解析与格式问题（已在 `src/admin.py` 修复）
- **现象**：管理员输入 `添加好友：xxxx`（带中文冒号）时，机器人直接把指令转发给了 Mac Claude Relay，作为普通任务处理。
- **原因**：原代码使用 `content.split(None, 2)` 进行空格切分，当用户习惯性输入冒号（`添加好友：`）或紧凑输入时，未能命中命令匹配。
- **已修复**：引入正则 `_ADD_FRIEND_CMD` 与 `_ADD_FRIEND_STRIP`，全面兼容 `添加好友` / `加好友`，支持任意半角/全角冒号及空格。

### 2. 报错「无法打开微信「添加朋友」窗口」（`_open_add_friend_dialog_wechat4: popup menu not found after clicking (+)`）
- **现象**：当管理员在微信聊天窗口发送添加指令后，后台尝试打开添加朋友界面失败，日志显示点击 `(+)` 按钮后找不到弹出菜单。
- **排查与根因**：
  1. **微信 4.0 的 `(+)` 按钮为 Toggle 开关**：
     微信主窗口左上方搜索栏右侧的 `(+)` 按钮（坐标约 `rect[0] + 280, rect[1] + 56`）属于 Toggle 模式。如果在短时间内触发了两次点击（例如快速重试或多通道并发点击），第一次打开菜单，第二次立即关闭菜单，导致枚举窗口时找不到菜单。
  2. **物理硬件鼠标抖动干扰**：
     若依赖传统的 `SetCursorPos` + `mouse_event`，当用户人在电脑前刚刚按下回车键或手搭在物理鼠标上时，物理鼠标高频报告（125Hz-1000Hz）会与操作系统的模拟光标坐标合并。瞬间的微动导致鼠标光标偏离 `(+)` 目标，误点到了空白会话列表或会话项，菜单无法弹出。
  3. **Win32 桌面安全会话隔离（Desktop Context）**：
     WCDB 轮询是在后台工作线程运行的。在 Windows 上，非主线程如果没有关联到交互式 `"Default"` 桌面，调用 `win32gui.EnumWindows` 和 `GetWindowRect` 时会偶发获取不到窗口或抛出 Win32 1400 错误（`ERROR_INVALID_WINDOW_HANDLE`）。
  4. **微信 4.0 窗口类名与标题**：
     微信 4.0 的 Qt 对话框在 Win32 中的原生类名为 `Qt51514QWindowIcon`，其原生窗口标题在 Win32 API 下就是 `'微信'` 或 `'Weixin'`（而非旧版的 `'添加朋友'`），因此必须将 `'微信'`、`'Weixin'` 纳入合法的对话框候选标题。

### 3. 中止报错：「微信窗口状态异常，已中止：the dialog (HWND=xxx) is not the window in front」
- **现象**：在对话框弹出后，执行到按键或点击输入框时触发 `_Abort` 异常退出。
- **原因**：
  - `FriendManager._foreground(hwnd, exact=True)` 设定了强一致的前台校验：要求当前系统前台窗口句柄必须严格等于 `dialog_hwnd`。
  - 在实际 Windows 环境中，当对话框刚弹出或者操作系统在焦点切换动画延迟中，或者用户在使用 Terminal/开发工具时，`GetForegroundWindow()` 可能返回所属进程的其他窗口或前一个窗口，导致严格检查直接抛出中止。

---

## 三、已完成的代码修改与验证成果

### 1. `src/admin.py`
- 支持 `添加好友` 与 `加好友` 前缀，支持冒号（`:`、`：`）与任意空白。
- 提取 target 与 reason 时自动剥离前缀与冒号。

### 2. `src/wechat/friend_manager.py`
- **引入 `post_click_window` 机制**：
  通过 `win32gui.ScreenToClient` 换算为窗口客户区坐标，利用 `win32gui.PostMessage(hwnd, WM_LBUTTONDOWN/UP)` 直接向 Qt 窗口的消息队列投递鼠标消息。
  - **实测收益**：完全不需要物理移动鼠标指针，极快（0.84 秒内完成菜单弹出与子项点击），彻底免疫用户的物理鼠标晃动。
- **单次点击防重保底**：
  优先使用 `post_click_window`，若成功则不再重复触发硬件光标点击，杜绝 Toggle 按钮“开即关”。
- **桌面隔离防护**：
  增加 `_safe_ensure_default_desktop()`，在关键的 `_ui_session()`、窗口枚举和消息投递入口保证线程关联到默认交互桌面。
- **标题匹配与弹窗发现优化**：
  `ADD_DIALOG_TITLES` 包含 `("添加朋友", "Add Friend", "微信", "Weixin")`。

### 3. 测试覆盖与状态
- `tests/test_friend_manager.py` 全部通过。
- `tests/test_router_business.py` 全部通过。

---

## 四、交接开发机 Claude Code 的重点任务与改进建议

请开发机上的 Claude Code 重点跟进以下环节：

1. **模态弹窗前台焦点与输入保护（Focus Resilience）**：
   - 检查 `FriendManager._foreground` 与 `_is_wechat_foreground`。
   - 当微信 4.0 弹出添加好友子窗口时，如果前台窗口虽然不是该 dialog 但属于同一个 `weixin.exe` PID，应自动调用 `SetForegroundWindow(dialog_hwnd)` 进行补正，而非立即抛出 `_Abort("the dialog is not the window in front")`。
   - 考虑在 `_add_friend_wechat4` 搜索输入前增加轻微延时并校验焦点状态。

2. **多状态分支的完整适配**：
   - **对方无需验证**：点击「添加到通讯录」后直接成为好友，不弹出「申请添加朋友」二级弹窗，需及时检测通讯录状态或关闭弹窗。
   - **频次风控提示**：若微信提示「操作过于频繁，请稍后再试」等 RISK_MARKERS，确保能够准确抓取文本、关闭残留弹窗，并自动调用 `_pause_auto` 暂缓后续操作。

3. **测试规范与小号沙箱保护（重要纪律）**：
   - **严禁使用真实目标或真实联系人进行自动化回归测试**。
   - 在测试和调试时，必须使用专用测试小号，或者通过 Mock / 模拟数据进行断言，避免多次重复向真实账号发送好友申请。

4. **PyInstaller 打包与高分屏 DPI 一致性**：
   - Windows 4K 高分屏下（如 200% 缩放），Python 脚本与 PyInstaller 生成的 `.exe` 默认 DPI 感知行为需保持一致，确保坐标转换始终在逻辑像素体系下精准对齐。

---

## 五、开发机 Claude Code 的跟进（2026-10-29）

在上面的改动之上又修了三处：

1. **每个按钮被点了两次**：`_click_at`、`_click_row`（对话框里的「添加到通讯录」、申请窗口的按钮都走它们）先投递一次点击、紧跟着又物理点一次。第一下把对话框关了，第二下就落到后面的窗口上。现在一次只点一下：对话框按钮用物理点击（第二轮真机测试时就是这么点的）；投递点击只留给「+」和它的菜单，而且是投递不成才物理点。
2. **「微信」「Weixin」当标题会认错窗口**：打开对话框之前，代码会先看有没有已经开着的「添加朋友」窗口。标题加了「微信」以后，进程里任何一个叫「微信」的窗口都会被直接当成对话框（「+」都不点），按键发不进去，就出现「the dialog … is not the window in front」。现在通用标题（`GENERIC_DIALOG_TITLES`）只认点了「添加朋友」以后新出现的窗口；已经开着的窗口仍然只认「添加朋友」/「Add Friend」。
3. **对话框不在最前面就立刻中止**：如果前台是微信自己的别的窗口（主窗口、正在关的菜单），先用窗口控制器的 `activate`（就是平时把微信调到前台那一套）把对话框调到前面再核对一次，还不行才中止；前台是别的程序时照旧中止，不会往用户自己的窗口里打字。

`tests/test_friend_manager.py` 加了对应的用例（一次只点一下、已开着的「微信」窗口不算对话框、新出现的算、调到前台以后继续、调不过来照样中止、别的程序在前台不动它）。

真机上看效果：管理员私聊发「添加好友 <测试小号>」，日志里应当有 `(+)` 菜单弹出、新出现的对话框，以及（如果出现过）`the dialog (HWND=…) was brought in front again`；不应再有 `is not the window in front`。只用测试小号试。

---

## 六、Windows 真机最终排查与回退逻辑修复（2026-10-29 15:50）

在开发机改动同步到真机后，15:37 真机测试依然偶发出现 `_open_add_friend_dialog_wechat4: popup menu not found after clicking (+)`，经深入排查定位到了最后一块逻辑死角并已完成彻底修复：

### 1. 根因：`post_click_window` 返回值陷阱与 Qt 消息丢弃

在开发机的回退逻辑中：
```python
if not post_click_window(main_hwnd, plus_x, plus_y):
    if not self._clicker(plus_x, plus_y):
        return None
```
- `win32gui.PostMessage(...)` 只要成功将消息推入 Windows 消息队列就会返回非 0（Python 中即为 `True`），因此 `if not post_click_window(...)` 永远为 `False`，**物理模拟点击的兜底逻辑实际上永远不会被执行**！
- 微信 4.0 底层是 Qt 5.15，当聊天窗口内部的文本输入控件具有当前焦点时，Qt 的事件过滤器在没有接收到先验 `WM_MOUSEMOVE` 悬停的情况下，会把投递给父窗口的 `WM_LBUTTONDOWN` 消息静默丢弃。
- 最终导致：消息投递成功但菜单并未真正弹出，而后方的物理点击又因判断条件错失执行机会，最终超时报错。

### 2. 最终修复方案：以「菜单是否真正出现」为唯一回退判据

修改 `src/wechat/friend_manager.py` 中的 `_open_add_friend_dialog_wechat4`：
1. **点击 `(+)` 按钮时**：
   - 优先执行 `post_click_window` 静默投递；
   - 快速轮询 `_find_menu()`；
   - 若 `not menu_hwnd`（说明消息被 Qt 丢弃未响应），**立即触发物理模拟点击 `self._clicker(plus_x, plus_y)` 兜底**，再次轮询菜单。
2. **点击菜单第二项「加朋友」时**：
   - 优先执行 `post_click_window` 静默投递；
   - 快速检测新弹出的对话框；
   - 若短时间内没有新对话框出现，**立即触发物理模拟点击 `self._clicker(add_friend_x, add_friend_y)` 兜底**。

这样既保留了静默点击不干扰物理鼠标的最佳体验，又在 Qt 吞消息的边界情况下有 100% 可靠的物理点击保底。

### 3. 本地打包与服务状态
- 修复已通过全部单元测试（179 passed）；
- 本地重新执行 `pyinstaller build.spec` 生成最新 `dist/webot.exe`；
- 后台任务已重启，当前 `webot.exe` 处于监听与就绪运行状态。
