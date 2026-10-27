"""Which network the Mac connects from (src/relay/link.py): the addresses the console offers,
the relay's state and the logs, when the Mac and Windows share a LAN or are joined by Tailscale
(cross-network.md)."""

import json
import logging
import sys
import urllib.request
from types import SimpleNamespace

import pytest

from test_edge_protocol import RealSide

from src.relay import link
from src.relay.link import PeerLog, address_kind, this_pc_addresses
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

TOKEN = "k" * 32
ME = "wxid_me"


@pytest.mark.parametrize("address, kind", [
    ("127.0.0.1", "loopback"), ("::1", "loopback"),
    ("192.168.9.155", "lan"), ("10.0.0.8", "lan"), ("172.20.1.2", "lan"), ("169.254.3.4", "lan"),
    ("100.101.102.103", "tailscale"), ("100.64.0.1", "tailscale"), ("fd7a:115c:a1e0::1", "tailscale"),
    ("::ffff:192.168.1.2", "lan"), ("::ffff:100.100.1.1", "tailscale"),
    ("8.8.8.8", "public"), ("100.128.0.1", "public"),
    ("", "unknown"), ("win-pc", "unknown"),
])
def test_the_network_of_an_address(address, kind):
    assert address_kind(address) == kind


def adapters(**named):
    return SimpleNamespace(net_if_addrs=lambda: {
        name: [SimpleNamespace(family=link.socket.AF_INET6 if ":" in a else link.socket.AF_INET, address=a)
               for a in addrs] for name, addrs in named.items()})


def test_this_pcs_addresses_are_the_lans_then_tailscales(monkeypatch):
    monkeypatch.setattr(link, "_local_addresses", lambda: ["192.168.9.155", "127.0.0.1", "169.254.7.7", "172.17.0.1"])
    monkeypatch.setitem(sys.modules, "psutil", adapters(
        Ethernet=["192.168.9.155"], Tailscale=["100.101.102.103", "fd7a:115c:a1e0::5"], vEthernet=["172.30.0.1"]))
    assert this_pc_addresses() == [{"ip": "192.168.9.155", "kind": "lan"}, {"ip": "172.17.0.1", "kind": "lan"},
                                   {"ip": "100.101.102.103", "kind": "tailscale"}]
    monkeypatch.setitem(sys.modules, "psutil", None)                       # no psutil: the LAN ones
    assert [a["ip"] for a in this_pc_addresses()] == ["192.168.9.155", "172.17.0.1"]


def test_where_the_mac_connects_from_is_logged_once_per_address(caplog):
    peer = PeerLog("CLAUDE_RELAY: the Mac worker")
    with caplog.at_level(logging.INFO, logger="src.relay.link"):
        for address in ("192.168.9.20", "192.168.9.20", "", "100.101.102.103", "100.101.102.103"):
            peer.note(address)
    assert [r.getMessage() for r in caplog.records] == [
        "CLAUDE_RELAY: the Mac worker connects from 192.168.9.20 (lan)",
        "CLAUDE_RELAY: the Mac worker now connects from 100.101.102.103 (tailscale), before from 192.168.9.20"]
    assert peer.address == "100.101.102.103" and peer.kind == "tailscale"


@pytest.fixture
def relay():
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid=ME)
    r = ClaudeRelay(cfg, store=RelayStore(":memory:"), send=lambda chat, text: True)
    r.start()
    yield r
    r.stop()


def test_the_relay_knows_where_the_worker_asks_from(relay):
    host, port = relay.address
    request = urllib.request.Request(f"http://{host}:{port}/relay/next?wait=0",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as answer:
        assert answer.status == 204
    state = relay.snapshot()
    assert state["mac_address"] == "127.0.0.1" and state["mac_link"] == "loopback"
    assert relay.status_text(ME).splitlines()[0] == "Mac：在线"               # this PC itself: no network
    relay.note_worker(None, address="100.101.102.103")                    # the worker moved to Tailscale
    assert relay.snapshot()["mac_link"] == "tailscale"
    assert relay.status_text(ME).splitlines()[0] == "Mac：在线（经 Tailscale）"
    relay.note_worker(None, address="192.168.9.20")
    assert relay.status_text(ME).splitlines()[0] == "Mac：在线（经局域网）"


def test_the_edge_knows_where_the_mac_asks_from(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="src.relay.link"):
        side = RealSide(tmp_path)
        try:
            side.client.health()
            side.client.health()
        finally:
            side.close()
    assert side.edge._peer.address == "127.0.0.1"
    assert [r.getMessage() for r in caplog.records if "EDGE: the Mac" in r.getMessage()] == [
        "EDGE: the Mac connects from 127.0.0.1 (loopback)"]
