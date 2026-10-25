"""给 Claude（`claude -p`）用的达人工具，按 MCP 协议走 stdio。

一位达人的一次对话启动一个服务进程，chat_id 由启动参数固定：模型只能查、只能提交这一位达人的事，
换不了人。工具只有这几样，都不花钱、不发货、不改佣金：
- check_inventory：查库存（不给模型看成本价）
- submit_sample_request：信息齐了提交寄样申请，生成审批单，等运营在控制台批准（R2）
- get_request_status：这位达人的申请和物流状态（运单号）
- hand_over_to_operator：拿不准的事交给运营
建单、绑佣金、生成推广链接只在运营批准后由代码执行；佣金上限写死在代码里，也没有改佣金的工具。

启动：python -m src.influencer.mcp_server --chat-id <达人 chat_id> --db <数据库路径> [--nickname <昵称>]
给 claude -p 用的配置见 mcp_config()；允许的工具名见 ALLOWED_TOOLS。
协议只实现了工具需要的部分：initialize、ping、tools/list、tools/call。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .extract import extract_xhs_account
from .handler import InfluencerBusinessHandler, _mask_phone
from .store import INBOX_MESSAGE, SOURCE_BOT, InfluencerStore
from .types import STATUS_LABELS, GateStatus, SampleRequestDraft

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_NAME = "influencer"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# 提交申请以后提醒模型的话：审批前不对达人做承诺（B1、R2）
_NO_PROMISE = "不要向达人承诺发货时间、快递或佣金；告诉达人已提交审核、审核结果会第一时间通知。"


class ToolError(Exception):
    """工具调用失败，原因会原样告诉模型。"""


class InfluencerTools:
    """绑定在一位达人身上的工具。"""

    def __init__(self, handler: InfluencerBusinessHandler, chat_id: str, nickname: str = ""):
        self.handler = handler
        self.store = handler.store
        self.chat_id = chat_id
        self.nickname = nickname

    # ---- 工具 ----

    def check_inventory(self, product: str) -> Dict[str, Any]:
        product = _text(product)
        if not product:
            raise ToolError("要先知道达人想要哪款商品")
        inventory = asyncio.run(self.handler.erp.check_inventory(product))
        in_stock = bool(inventory.get("in_stock")) and inventory.get("available_quantity", 0) > 0
        return {
            "product_name": inventory.get("product_name") or product,
            "in_stock": in_stock,
            "available_quantity": inventory.get("available_quantity", 0) if in_stock else 0,
        }

    def submit_sample_request(
        self,
        product: str,
        recipient_name: str,
        recipient_phone: str,
        shipping_address: str,
        xhs_account: str = "",
        evidence: str = "",
    ) -> Dict[str, Any]:
        """信息齐了才生成审批单。缺东西、缺货时不生成，返回该怎么问达人。"""
        self._require_influencer()
        draft = SampleRequestDraft(
            chat_id=self.chat_id,
            product=_text(product),
            recipient_name=_text(recipient_name),
            recipient_phone=_text(recipient_phone),
            shipping_address=_text(shipping_address),
            xhs_account=extract_xhs_account(_text(xhs_account)) or _text(xhs_account),
        )
        if _text(evidence):
            draft.evidence.append(_text(evidence))

        existing = self._pending_for(draft.product)
        if existing is not None:
            return {
                "submitted": False,
                "already_submitted": True,
                "packet_id": existing.packet_id,
                "message": f"这位达人的【{existing.requested_product}】申请已经在等审核了，不要重复提交。{_NO_PROMISE}",
            }

        with self.handler._chat_lock(self.chat_id):
            follow_up = asyncio.run(self.handler._ask_for_what_is_missing(draft))
            if follow_up is not None:
                # 缺货时处理器已经记进了「待处理」；这里只告诉模型还差什么
                return {
                    "submitted": False,
                    "missing": list(draft.asked_for),
                    "suggested_reply": follow_up,
                    "message": "信息还不全或者没有库存，没有提交。按 suggested_reply 的意思回复达人。",
                }
            asyncio.run(self.handler._submit_for_approval(self.chat_id, self.nickname, draft))
        packet = self._pending_for(draft.product)
        logger.info(f"[达人工具] 达人 {self.chat_id} 提交了寄样申请 {packet.packet_id if packet else '?'}")
        return {
            "submitted": True,
            "packet_id": packet.packet_id if packet else "",
            "product": packet.requested_product if packet else draft.product,
            "recipient": f"{draft.recipient_name}（{_mask_phone(draft.recipient_phone)}）",
            "needs_xhs_account": not draft.xhs_account,
            "message": "已提交审核，等运营批准。" + _NO_PROMISE
            + ("还缺达人的小红书主页链接，请向达人要一下。" if not draft.xhs_account else ""),
        }

    def get_request_status(self) -> Dict[str, Any]:
        """这位达人最近的申请：状态、运单号、推广链接。不给收件信息和驳回原因。"""
        packets = [p for p in self.store.list_approvals(None) if p.influencer_id == self.chat_id][:5]
        items = []
        for p in packets:
            item = {
                "packet_id": p.packet_id,
                "product": p.requested_product,
                "status": STATUS_LABELS[p.status],
            }
            if p.status == GateStatus.FULFILLED:
                item["tracking_number"] = p.tracking_number or "运单号还没出来"
                if p.product_link:
                    item["product_link"] = p.product_link
            items.append(item)
        return {
            "requests": items,
            "note": (
                "物流轨迹暂时查不到，只有运单号。状态是已驳回的，不要自己解释原因，交给运营；"
                "执行中、执行失败的，告诉达人在处理，并交给运营。"
            ),
        }

    def hand_over_to_operator(self, summary: str) -> Dict[str, Any]:
        summary = _text(summary)
        if not summary:
            raise ToolError("写一句要运营处理什么")
        self.store.add_inbox_item(self.chat_id, INBOX_MESSAGE, summary)
        return {"handed_over": True, "message": "已交给运营。可以告诉达人我们核对一下再回复，不要做任何承诺。"}

    # ---- 工具清单 ----

    def specs(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "check_inventory",
                "description": "查一款商品现在有没有货（聚水潭）。达人问有没有货、想要样品时先查。",
                "inputSchema": _schema({"product": "商品名，例如 VC精华液"}, ["product"]),
            },
            {
                "name": "submit_sample_request",
                "description": (
                    "达人确认要样品、收件信息都给了以后，提交寄样申请。只生成审批单，"
                    "要等运营在后台批准后才会发货；提交后不能向达人承诺发货时间、快递和佣金。"
                ),
                "inputSchema": _schema({
                    "product": "商品名",
                    "recipient_name": "收件人姓名",
                    "recipient_phone": "收件人手机号",
                    "shipping_address": "收货地址",
                    "xhs_account": "达人的小红书主页链接或小红书号，没有就留空",
                    "evidence": "达人说要样品的原话，给运营核对",
                }, ["product", "recipient_name", "recipient_phone", "shipping_address"]),
            },
            {
                "name": "get_request_status",
                "description": "查这位达人的寄样申请到哪一步了，发货后有运单号。达人问审核结果、快递到哪了时用。",
                "inputSchema": _schema({}, []),
            },
            {
                "name": "hand_over_to_operator",
                "description": "拿不准、超出你能回答范围的事（价格、合同、投诉、佣金以外的商务条件等），交给运营人工处理。",
                "inputSchema": _schema({"summary": "一句话说明要运营处理什么"}, ["summary"]),
            },
        ]

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tools: Dict[str, Callable[..., Dict[str, Any]]] = {
            "check_inventory": self.check_inventory,
            "submit_sample_request": self.submit_sample_request,
            "get_request_status": self.get_request_status,
            "hand_over_to_operator": self.hand_over_to_operator,
        }
        if name not in tools:
            raise ToolError(f"没有这个工具：{name}")
        try:
            return tools[name](**(arguments or {}))
        except TypeError as exc:
            raise ToolError(f"参数不对：{exc}") from exc

    # ---- 内部 ----

    def _require_influencer(self) -> None:
        if not self.store.is_influencer(self.chat_id):
            raise ToolError("这位联系人不在达人名单里，不能提交寄样申请")

    def _pending_for(self, product: str):
        for packet in self.store.list_approvals([GateStatus.PENDING]):
            if packet.influencer_id == self.chat_id and (not product or product in packet.requested_product):
                return packet
        return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _schema(properties: Dict[str, str], required: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "string", "description": desc} for name, desc in properties.items()},
        "required": required,
        "additionalProperties": False,
    }


# ---- MCP（JSON-RPC 2.0，一行一条消息） ----

def handle_message(tools: InfluencerTools, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理一条消息，返回要写回的响应；通知（没有 id）不回。"""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        result: Dict[str, Any] = {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools.specs()}
    elif method == "tools/call":
        try:
            payload, is_error = tools.call(params.get("name", ""), params.get("arguments") or {}), False
        except ToolError as exc:
            payload, is_error = {"error": str(exc)}, True
        except Exception as exc:  # 聚水潭、小红书出错：告诉模型转人工，不要瞎答
            logger.exception("[达人工具] 工具出错")
            payload, is_error = {"error": f"系统出错了，交给运营处理：{exc}"}, True
        result = {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        }
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"不支持的方法：{method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(tools: InfluencerTools, stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是 JSON"}}
        else:
            response = handle_message(tools, message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


ALLOWED_TOOLS = tuple(f"mcp__{SERVER_NAME}__{name}" for name in (
    "check_inventory", "submit_sample_request", "get_request_status", "hand_over_to_operator",
))


def mcp_config(chat_id: str, db_path: str, *, python: str = sys.executable, nickname: str = "") -> Dict[str, Any]:
    """给 `claude -p --mcp-config` 用的配置。配合 --strict-mcp-config 和 --allowedTools ALLOWED_TOOLS，
    再关掉自带工具（--tools ""），模型就只能用这几样工具。"""
    args = ["-m", "src.influencer.mcp_server", "--chat-id", chat_id, "--db", db_path]
    if nickname:
        args += ["--nickname", nickname]
    # claude -p 不一定在项目目录里运行，用 PYTHONPATH 让 python -m 找得到 src
    env = {"PYTHONPATH": str(PROJECT_ROOT)}
    return {"mcpServers": {SERVER_NAME: {"type": "stdio", "command": python, "args": args, "env": env}}}


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="达人工具的 MCP 服务（给 claude -p 用）")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--nickname", default="")
    args = parser.parse_args(argv)
    # stdout 只能写协议消息，日志一律写到 stderr
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    handler = InfluencerBusinessHandler(InfluencerStore(args.db))
    serve(InfluencerTools(handler, args.chat_id, args.nickname))


if __name__ == "__main__":
    main()
