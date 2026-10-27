"""涨佣（D37，2026-10-26）：达人要求提高佣金时，系统不改佣金，只查他近 30 天在我们店的合作数据，
告诉运营是否满 40 单或成交额（退款前）满 1 万元，由运营决定。数据来自聚水潭命令行（jst mine），这里用替身。"""

import pytest

from src.influencer import jst_erp
from src.influencer.jst_erp import ERPNotReadyError, coop_summary


def cli_returns(monkeypatch, rows=None, error=None):
    calls = []

    def fake(*args):
        calls.append(args)
        if error:
            raise ERPNotReadyError(error)
        return {"rows": rows or []}

    monkeypatch.setattr(jst_erp, "run_jst_cli", fake)
    return calls


def row(no, orders, amount, net=0.0):
    return {"platformNo": no, "coop30d": {"orders": orders, "amount": amount, "netAmount": net, "commission": 0}}


def test_forty_orders_meets_the_bar(monkeypatch):
    calls = cli_returns(monkeypatch, [row("dy1", 40, 3000.0, 1000.0)])
    result = coop_summary("DY", "dy1")
    assert calls == [("mine", "--platform", "DY", "--no", "dy1")]
    assert result["ok"] and result["meets"] and result["orders"] == 40
    assert "满足" in result["text"] and "由运营决定" in result["text"]


def test_ten_thousand_before_refunds_meets_the_bar_even_if_refunded(monkeypatch):
    # 成交额先按退款前算（用户 10-26 定）；退款后的净额只列出来给运营参考
    cli_returns(monkeypatch, [row("dy1", 12, 10000.0, 0.0)])
    result = coop_summary("DY", "dy1")
    assert result["meets"] and "退款后 0 元" in result["text"]


def test_below_the_bar(monkeypatch):
    cli_returns(monkeypatch, [row("dy1", 39, 9999.0)])
    result = coop_summary("DY", "dy1")
    assert result["ok"] and not result["meets"] and "不满足" in result["text"]


@pytest.mark.parametrize("number,rows,error,expected", [
    ("", None, None, "没有这位达人的抖音号"),
    ("dy1", [], None, "我的达人」里没有 dy1"),
    ("dy1", None, "聚水潭登录失效", "查合作数据失败"),
])
def test_what_cannot_be_looked_up_is_said_plainly(monkeypatch, number, rows, error, expected):
    cli_returns(monkeypatch, rows, error)
    result = coop_summary("DY", number)
    assert result["ok"] is False and expected in result["text"]


def test_a_missing_cli_is_reported_not_faked(monkeypatch, tmp_path):
    monkeypatch.setenv("JST_CLI_PATH", str(tmp_path / "nowhere"))
    with pytest.raises(ERPNotReadyError, match="找不到聚水潭命令行"):
        jst_erp.run_jst_cli("mine")
