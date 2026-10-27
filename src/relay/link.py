"""Which network an address is on: the Mac and this PC on one LAN, over Tailscale, or not.

The Mac always connects to this PC (the relay's port, ``/relay/*`` and ``/edge/*``); when the
two are not on one LAN they are joined by Tailscale (cross-network.md), whose addresses are in
100.64.0.0/10 (IPv6 fd7a:115c:a1e0::/48).  The console labels addresses with it and the logs
say where the Mac connects from.
"""

from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

LOOPBACK, LAN, TAILSCALE, PUBLIC, UNKNOWN = "loopback", "lan", "tailscale", "public", "unknown"
LABELS = {LOOPBACK: "本机", LAN: "局域网", TAILSCALE: "Tailscale", PUBLIC: "公网", UNKNOWN: "未知"}
VIA = {LAN: "经局域网", TAILSCALE: "经 Tailscale", PUBLIC: "经公网"}     # how the Mac is reached


def address_kind(address: str) -> str:
    """``loopback`` / ``lan`` / ``tailscale`` / ``public`` / ``unknown`` (not an IP address)."""
    try:
        ip = ipaddress.ip_address(str(address).split("%")[0].strip("[]"))
    except ValueError:
        return UNKNOWN
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return LOOPBACK
    if ip in (TAILSCALE_V4 if ip.version == 4 else TAILSCALE_V6):
        return TAILSCALE
    if ip.is_private or ip.is_link_local:
        return LAN
    return PUBLIC


def this_pc_addresses() -> list[dict]:
    """This PC's IPv4 addresses for the Mac's relay_url, ``[{"ip", "kind"}]``: the LAN ones (the
    outgoing interface first), then Tailscale's (its adapter, when Tailscale runs here)."""
    found = _local_addresses()
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and address_kind(a.address) == TAILSCALE and a.address not in found:
                    found.append(a.address)
    except Exception:
        logger.debug("psutil could not list the network adapters", exc_info=True)
    order = {LAN: 0, TAILSCALE: 1, PUBLIC: 2}
    usable = [{"ip": ip, "kind": address_kind(ip)} for ip in found
              if address_kind(ip) in order and not ipaddress.ip_address(ip).is_link_local]
    return sorted(usable, key=lambda a: order[a["kind"]])


def _local_addresses() -> list[str]:
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))          # no packet is sent: this only picks the outgoing interface
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    return found


class PeerLog:
    """Where the Mac connects from, logged once per address: the first time, and when it moves
    (from the LAN to Tailscale, say)."""

    def __init__(self, what: str):
        self._what = what
        self.address: str | None = None

    def note(self, address: str) -> None:
        address = str(address or "")
        if not address or address == self.address:
            return
        before, self.address = self.address, address
        if before is None:
            logger.info("%s connects from %s (%s)", self._what, address, address_kind(address))
        else:
            logger.info("%s now connects from %s (%s), before from %s", self._what, address,
                        address_kind(address), before)

    @property
    def kind(self) -> str | None:
        return address_kind(self.address) if self.address else None
