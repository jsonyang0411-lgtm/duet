"""聚水潭（JuShuiTan）的只读查询：达人模块按名单上的号码查档位、主播类型、平台 UID（tiers.py）。

聚水潭没有开放平台密钥（D32），走我们自己的命令行 ~/jushuitan-engineering
（``node bin/jst.mjs``，直接发 HTTP，令牌从调试版 Chrome 现取）。建寄样单不在这里：
走 PRM 的平台动作 jst.sample_order（Mac 上的 worker 调 sample-create）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

CLI_TIMEOUT_SECONDS = 60


class ERPNotReadyError(RuntimeError):
    """聚水潭用不了：命令行找不到、登录失效、限流。"""


def jst_cli_dir() -> Path:
    """聚水潭命令行的目录：环境变量 JST_CLI_PATH，默认 ~/jushuitan-engineering。"""
    return Path(os.getenv("JST_CLI_PATH") or "~/jushuitan-engineering").expanduser()


def run_jst_cli(*args: str) -> Dict[str, Any]:
    """跑一条只读的聚水潭命令，返回它输出的 JSON。命令失败（登录失效、限流、找不到命令行）抛 ERPNotReadyError。"""
    entry = jst_cli_dir() / "bin" / "jst.mjs"
    if not entry.exists():
        raise ERPNotReadyError(f"找不到聚水潭命令行 {entry}（设 JST_CLI_PATH）")
    node = os.getenv("JST_NODE_PATH") or "node"
    try:
        done = subprocess.run(
            [node, str(entry), *args], capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ERPNotReadyError(f"聚水潭命令行没跑起来：{exc}") from exc
    if done.returncode != 0:
        why = (done.stderr or "").strip().splitlines()[-1:] or [f"退出码 {done.returncode}"]
        raise ERPNotReadyError(f"聚水潭命令行出错：{why[0][:200]}")
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise ERPNotReadyError("聚水潭命令行的输出不是 JSON") from exc
