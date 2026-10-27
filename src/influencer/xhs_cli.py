"""小红书 CLI 适配器：同事的千帆商家后台命令行工具（仓库 doupi-org/xiaohongshu-engineering）。

用到它的两个命令（2026-10-26 读过代码和文档）：
- 查达人：`direct-plans --find-buyer <小红书号或昵称> --raw`，返回买手的 user_id 和粉丝数（fan_cnt），
  查不到点赞（所以资质只看粉丝，D16）。
- 绑佣金：`direct-plan --action create --name <计划名> --buyer-id <user_id> --item <千帆商品ID>:<佣金%>
  --long-term --execute`，即千帆的"定向计划"：指定的达人在自己的选品里能看到这款商品，带货按这个佣金结算，
  佣金由我们出。CLI 先读状态、只发一次、读回核对，`VERIFIED` 才算成功（退出码 0）。
  计划名用审批单号：同名的计划 CLI 会拒绝再建，所以同一张审批单不会绑两次。
  不发推广链接（D17），达人在选品里自己找。

运行条件：Node ≥ 22；开着带调试端口的 Chrome 并登录了千帆（CLI 通过它发请求）。登录过期 CLI 退出码是 77，
这里报"千帆登录失效"，要人到那个 Chrome 里重新登录。遇到平台风控 CLI 会停下，这里也不重试。

配置（环境变量）：
- XHS_CLI_PATH：CLI 仓库目录，或其中的 bin/xhs.mjs
- XHS_NODE_PATH：node 的路径，默认在 PATH 里找
- INFLUENCER_XHS_ITEMS_FILE：产品 → 千帆商品 ID 的对照表（JSON），默认 data/xhs_items.json，
  模板见 docs/xhs_items.example.json。键可以是审批单上的商品名，也可以是聚水潭 SKU

模拟模式只能手动打开（D6）：构造时传 mock_mode=True，或设置环境变量 INFLUENCER_MOCK=1。
没打开时，CLI 用不了、执行失败或输出看不懂，都抛 XHSCLIError 或返回失败，绝不返回假数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .settings import mock_enabled

logger = logging.getLogger(__name__)

# 建定向计划时 CLI 要先读名单、买手、佣金规则和商品资格，再发送、跟任务、读回，给足时间
CLI_TIMEOUT_SECONDS = 180
EXIT_LOGIN = 77
PLAN_NAME_MAX = 35  # 千帆定向计划名称的长度上限
DEFAULT_ITEMS_FILE = "data/xhs_items.json"

# 小红书用户 ID：24 位十六进制；主页链接 /user/profile/<ID> 里的就是它
_USER_ID_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{24})(?![0-9a-fA-F])")
# 千帆商品 ID：字母和数字。对照表里填的不是这种（比如照抄了模板的提示文字）就当没填
_ITEM_ID_RE = re.compile(r"^[0-9A-Za-z]{6,64}$")


class XHSCLIError(RuntimeError):
    """小红书 CLI 不可用，或者执行失败。"""


def plan_name(packet_id: str) -> str:
    return f"寄样-{packet_id}"[:PLAN_NAME_MAX]


def rate_text(rate: float) -> str:
    """0.25 → "25"，0.125 → "12.5"：CLI 要的是百分数，最多一位小数。"""
    percent = round(rate * 100, 1)
    return str(int(percent)) if percent == int(percent) else str(percent)


class XiaohongshuCLIClient:
    def __init__(
        self,
        cli_path: str = "",
        mock_mode: Optional[bool] = None,
        *,
        node_path: str = "",
        items_file: str = "",
    ):
        raw = (cli_path or os.getenv("XHS_CLI_PATH", "")).strip()
        script = Path(raw) if raw else None
        self.script = script / "bin" / "xhs.mjs" if script is not None and script.is_dir() else script
        self.cli_path = str(self.script) if self.script is not None else "（没有配置 XHS_CLI_PATH）"
        self.node_path = node_path or os.getenv("XHS_NODE_PATH", "") or shutil.which("node") or ""
        self.items_file = Path(items_file or os.getenv("INFLUENCER_XHS_ITEMS_FILE", "") or DEFAULT_ITEMS_FILE)
        self.is_available = bool(self.node_path) and self.script is not None and self.script.is_file()
        # None 表示按环境变量 INFLUENCER_MOCK 决定；找不到 CLI 不会自动进入模拟模式
        self.mock_mode = mock_enabled() if mock_mode is None else mock_mode
        if self.mock_mode:
            logger.warning("⚠️ [XHS] 小红书 CLI 处于模拟模式：达人数据是假的，不会真实绑定佣金")

    # ---- 查达人 ----

    async def get_creator_stats(self, xhs_account: str) -> Dict[str, Any]:
        """达人的粉丝数和买手 user_id。找不到这位达人、或者找到多位分不清时返回空字典（审批单标待核实）。"""
        if self.mock_mode:
            logger.info("[XHS Mock] 查询达人数据")
            return {"follower_count": 35000, "user_id": "0" * 24, "mock": True}
        buyer = await self._find_buyer(xhs_account)
        if buyer is None:
            return {}
        fans = buyer.get("fan_cnt")
        return {
            "follower_count": fans if isinstance(fans, int) and not isinstance(fans, bool) else None,
            "user_id": buyer["user_id"],
        }

    async def resolve_buyer_id(self, xhs_account: str) -> str:
        """绑佣金要的买手 user_id：主页链接里直接有；只有小红书号时去千帆搜，必须正好找到一位。"""
        match = _USER_ID_RE.search(xhs_account or "")
        if match:
            return match.group(1).lower()
        buyer = await self._find_buyer(xhs_account)
        if buyer is None:
            raise XHSCLIError(f"在千帆里按【{xhs_account}】找不到唯一的一位达人：请在审批单上填达人的小红书主页链接")
        return buyer["user_id"]

    async def _find_buyer(self, xhs_account: str) -> Optional[Dict[str, Any]]:
        query = (xhs_account or "").strip()
        id_match = _USER_ID_RE.search(query)
        if not query or (query.startswith("http") and not id_match):
            return None  # xhslink 短链里没有 ID，也搜不了
        code, data, stderr = await self._run("direct-plans", "--find-buyer", id_match.group(1) if id_match else query, "--raw")
        if code != 0:
            raise XHSCLIError(self._failure("查达人", code, data, stderr))
        rows = [b for b in self._list(data, "buyers") if isinstance(b, dict) and b.get("user_id")]
        if id_match:
            rows = [b for b in rows if str(b["user_id"]).lower() == id_match.group(1).lower()]
        return rows[0] if len(rows) == 1 else None

    # ---- 绑佣金 ----

    def commission_problem(self, product_name: str, sku_id: str, xhs_account: str) -> str:
        """批准前就能发现的问题（不调 CLI）：返回原因，没问题返回空字符串。
        在聚水潭建单之前检查，免得出库单建了才发现佣金绑不上。"""
        if self.mock_mode:
            return ""
        if not self.is_available:
            return self.readiness()["detail"]
        if not self.item_id_for(product_name, sku_id):
            return f"对照表 {self.items_file} 里没有【{product_name}】（SKU {sku_id}）对应的千帆商品 ID"
        if (xhs_account or "").startswith("http") and not _USER_ID_RE.search(xhs_account):
            return "小红书账号是短链，查不到达人：请填达人的主页链接（带 /user/profile/ 的那种）或小红书号"
        return ""

    def item_id_for(self, product_name: str, sku_id: str) -> str:
        try:
            table = json.loads(self.items_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(table, dict):
            return ""
        for key in (product_name, sku_id):
            value = str(table.get(key, "")).strip() if key else ""
            if _ITEM_ID_RE.match(value):
                return value
        return ""

    async def set_exclusive_commission(
        self, xhs_uid: str, sku_id: str, rate: float, idempotency_key: str = "", product_name: str = "",
    ) -> Dict[str, Any]:
        """给达人开一个定向计划：这款商品、这个佣金、长期有效。

        idempotency_key 传审批单号，用作计划名：同名计划 CLI 拒绝再建，所以重复调用不会绑两次。
        成功返回 {"success": True, "plan_id": 计划组 ID}；失败返回 {"success": False, "error": 原因}。
        """
        if self.mock_mode:
            logger.info(f"[XHS Mock] 绑定专属佣金: {rate_text(rate)}%")
            return {
                "success": True,
                "plan_id": f"PLAN-{abs(hash(xhs_uid)) % 100000}",
                "rate": rate,
                "message": f"小红书定向推广佣金已生效 ({rate_text(rate)}%)（模拟）",
                "mock": True,
            }
        problem = self.commission_problem(product_name, sku_id, xhs_uid)
        if problem:
            return {"success": False, "error": problem}
        buyer_id = await self.resolve_buyer_id(xhs_uid)
        name = plan_name(idempotency_key or sku_id)
        item = f"{self.item_id_for(product_name, sku_id)}:{rate_text(rate)}"
        code, data, stderr = await self._run(
            "direct-plan", "--action", "create", "--name", name, "--buyer-id", buyer_id,
            "--item", item, "--long-term", "--execute",
        )
        rows = self._list(data, "items")
        overall = (data.get("summary") or {}).get("overall") if isinstance(data, dict) else None
        if code == 0 and overall in ("VERIFIED", "NO_CHANGE"):
            plan_id = next((r.get("targetId") for r in rows if isinstance(r, dict) and r.get("targetId")), None)
            return {"success": True, "plan_id": str(plan_id or name), "plan_name": name}
        return {"success": False, "error": self._failure("建定向计划", code, data, stderr)}

    # ---- 状态 ----

    def readiness(self) -> Dict[str, Any]:
        """给控制台看的状态：能不能用、是不是模拟模式、为什么不能用。千帆有没有登录要到调用时才知道。"""
        if self.mock_mode:
            return {"ready": True, "mock": True, "detail": "模拟模式：达人数据是假的，不会真实绑定佣金"}
        if not self.node_path:
            return {"ready": False, "mock": False, "detail": "找不到 node（要 22 以上），可以用环境变量 XHS_NODE_PATH 指定"}
        if self.script is None or not self.script.is_file():
            return {
                "ready": False, "mock": False,
                "detail": f"找不到小红书 CLI（{self.cli_path}），用环境变量 XHS_CLI_PATH 指定 CLI 仓库的目录",
            }
        if not self.items_file.is_file():
            return {
                "ready": False, "mock": False,
                "detail": f"找不到产品对照表 {self.items_file}（模板 docs/xhs_items.example.json），绑不了佣金",
            }
        return {"ready": True, "mock": False, "detail": f"小红书 CLI：{self.cli_path}（要开着调试 Chrome 并登录千帆）"}

    # ---- 调用 ----

    async def _run(self, *args: str) -> Tuple[int, Any, str]:
        """执行一次 CLI，返回 (退出码, stdout 里的 JSON, stderr)。退出码不是 0 也照样返回，
        由调用方读结果；CLI 用不了、超时、输出不是 JSON、千帆登录失效时抛 XHSCLIError。"""
        if not self.is_available:
            raise XHSCLIError(f"小红书 CLI 用不了：{self.readiness()['detail']}（也没有打开模拟模式 INFLUENCER_MOCK=1）")
        proc = await asyncio.create_subprocess_exec(
            self.node_path, str(self.script), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise XHSCLIError(
                f"小红书 CLI 超过 {CLI_TIMEOUT_SECONDS} 秒没有返回（{args[0]}）：请求可能已经发出，先到千帆核对，不要直接重试"
            )
        err = stderr.decode(errors="replace").strip()
        if proc.returncode == EXIT_LOGIN:
            raise XHSCLIError("千帆登录失效：到运行 CLI 的那台电脑的调试 Chrome 里重新登录千帆，再重试")
        try:
            data = json.loads(stdout.decode() or "null")
        except ValueError as exc:
            raise XHSCLIError(f"小红书 CLI 的输出不是 JSON（退出码 {proc.returncode}）：{err[:200] or exc}") from exc
        return proc.returncode, data, err

    @staticmethod
    def _list(data: Any, key: str) -> List[Any]:
        if isinstance(data, dict):
            value = data.get(key)
            if value is None and isinstance(data.get("result"), dict):
                value = data["result"].get(key)
            if isinstance(value, list):
                return value
        return []

    @classmethod
    def _failure(cls, what: str, code: int, data: Any, stderr: str) -> str:
        """把 CLI 的失败说成人话：每一行的判定和说明，没有就用 stderr 的最后一行。"""
        rows = [r for r in cls._list(data, "items") if isinstance(r, dict)]
        parts = [f"{r.get('outcome')}：{str(r.get('detail') or '')[:160]}" for r in rows if r.get("outcome")]
        if not parts and stderr:
            parts = [stderr.splitlines()[-1][:200]]
        reason = "；".join(parts) or "没有说明"
        hint = "，请求可能已经到达千帆，先核对再处理，不要直接重试" if any(
            w in reason for w in ("INDETERMINATE", "ACCEPTED_UNVERIFIED", "OUTCOME_UNKNOWN")
        ) else ""
        return f"小红书 CLI {what}失败（退出码 {code}）：{reason}{hint}"
