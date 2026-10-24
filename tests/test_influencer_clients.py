"""小红书 CLI 适配器的真实调用路径（src/influencer/xhs_cli.py）。

用临时 shell 脚本冒充同事的 CLI，检查参数、退出码和输出格式的处理。
命令格式以 handoff.md 第 7 节的接口约定草案为准。
"""

import asyncio
import sys

import pytest

from src.influencer.xhs_cli import XHSCLIError, XiaohongshuCLIClient

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="用 shell 脚本冒充 CLI")


@pytest.fixture(autouse=True)
def no_mock_env(monkeypatch):
    monkeypatch.delenv("INFLUENCER_MOCK", raising=False)


def fake_cli(tmp_path, body):
    script = tmp_path / "xhs"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return XiaohongshuCLIClient(cli_path=str(script))


def test_commission_call_passes_the_approval_id_as_idempotency_key(tmp_path):
    client = fake_cli(tmp_path, 'printf \'{"success": true, "args": "%s"}\' "$*"')
    result = asyncio.run(client.set_exclusive_commission(
        xhs_uid="xhs_001", sku_id="SKU-1", rate=0.25, idempotency_key="GATE-ABC",
    ))
    assert result["success"] is True
    assert "commission set --uid xhs_001 --sku SKU-1 --rate 0.25" in result["args"]
    assert "--idempotency-key GATE-ABC --json" in result["args"]


def test_a_non_zero_exit_code_is_an_error_not_a_result(tmp_path):
    client = fake_cli(tmp_path, 'echo "账号未授权" >&2; exit 3')
    with pytest.raises(XHSCLIError, match="退出码 3"):
        asyncio.run(client.get_creator_stats("xhs_001"))


def test_output_that_is_not_json_is_an_error(tmp_path):
    client = fake_cli(tmp_path, "echo ok")
    with pytest.raises(XHSCLIError, match="不是 JSON"):
        asyncio.run(client.get_creator_stats("xhs_001"))
