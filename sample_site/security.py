"""Rate limiting, log-safe paths, tokens and signed form nonces."""

import collections
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time

TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def new_token():
    return secrets.token_urlsafe(24)


def token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def token_ok(token):
    return bool(token) and bool(TOKEN_SHAPE.match(token))


class RateLimiter(object):
    """Sliding-window counters per (bucket, key), in memory, thread-safe.

    Each key remembers its own window, so a 24-hour counter is not swept after
    an hour. When the table is full the least recently used keys are evicted
    (never everything at once).
    """

    MAX_KEYS = 50000

    def __init__(self, clock=time.monotonic, max_keys=None):
        self._clock = clock
        self._lock = threading.Lock()
        self._hits = collections.OrderedDict()  # (bucket, key) -> (window, deque of times)
        self._last_sweep = clock()
        self.max_keys = max_keys or self.MAX_KEYS

    def _entry(self, bucket, key, window, now, create):
        item = self._hits.get((bucket, key))
        if item is None:
            if not create:
                return None
            item = (window, collections.deque())
            self._hits[(bucket, key)] = item
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end((bucket, key))
        times = item[1]
        cutoff = now - window
        while times and times[0] <= cutoff:
            times.popleft()
        return times

    def hit(self, bucket, key, limit, window):
        """Count one event; False (and not counted) when the limit is reached."""
        now = self._clock()
        with self._lock:
            self._sweep(now)
            times = self._entry(bucket, key, window, now, True)
            if len(times) >= limit:
                return False
            times.append(now)
            return True

    def over(self, bucket, key, limit, window):
        """True when the key already reached the limit (does not count)."""
        now = self._clock()
        with self._lock:
            times = self._entry(bucket, key, window, now, False)
            return (len(times) if times is not None else 0) >= limit

    def __len__(self):
        return len(self._hits)

    def _sweep(self, now):
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        dead = [k for k, (window, times) in self._hits.items() if not times or times[-1] <= now - window]
        for k in dead:
            del self._hits[k]


def client_key(ip):
    """The rate-limit key for an address: IPv6 by its /64, IPv4 as is."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return (ip or "-")[:64]
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network("%s/64" % addr, strict=False))
    return str(addr)


def is_internal_peer(ip):
    """True for loopback / private addresses (where a reverse proxy sits)."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_private


def is_loopback_host(host):
    """True when a Host header names this machine (plain-http local use)."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Signer(object):
    """HMAC helpers keyed by the site's own random secret (kept in the DB)."""

    def __init__(self, secret):
        self._key = secret.encode("ascii")

    def _mac(self, text, n=22):
        return hmac.new(self._key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:n]

    def nonce(self, link_id):
        """A form nonce bound to one link."""
        r = secrets.token_hex(12)
        return "%s.%s" % (r, self._mac("nonce:%d:%s" % (link_id, r)))

    def nonce_ok(self, link_id, nonce):
        if not nonce or "." not in nonce or len(nonce) > 80:
            return False
        r, sig = nonce.split(".", 1)
        return bool(re.match(r"^[0-9a-f]{24}$", r)) and hmac.compare_digest(
            sig, self._mac("nonce:%d:%s" % (link_id, r)))

    def ip_tag(self, ip):
        """A short, salted tag for an IP (rate-limit log lines only)."""
        return self._mac("ip:" + (ip or ""), 8)


_KNOWN = (
    (re.compile(r"^/s/[^/]*$"), "/s/…"),
    (re.compile(r"^/g/[^/]*$"), "/g/…"),
    (re.compile(r"^/img/[0-9a-f]{32}$"), None),
    (re.compile(r"^/static/[A-Za-z0-9_.-]+$"), None),
    (re.compile(r"^/(|healthz|robots\.txt|favicon\.ico)$"), None),
    (re.compile(r"^/admin/api/(status|images|catalog|links|submissions/pull|submissions/ack)$"), None),
    (re.compile(r"^/admin/api/images/[0-9a-f]{32}$"), None),
    (re.compile(r"^/admin/api/links/\d{1,12}/revoke$"), None),
)


def safe_path(path):
    """The request path as it may appear in a log line (no tokens, no query)."""
    path = (path or "").split("?", 1)[0]
    for pattern, replacement in _KNOWN:
        if pattern.match(path):
            return replacement or path
    return "/<other>"


_LINE_PATH = re.compile(r"(/[sg]/)[^\s?\"']+")


def scrub(text):
    """Remove anything token-like from a free log line."""
    return _LINE_PATH.sub(r"\1…", text or "")
