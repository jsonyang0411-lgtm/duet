"""Rate limiting, log-safe paths, tokens and signed form nonces."""

import base64
import hashlib
import hmac
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
    """Sliding-window counters per (bucket, key), in memory, thread-safe."""

    MAX_KEYS = 50000

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._hits = {}
        self._last_sweep = clock()

    def hit(self, bucket, key, limit, window):
        """Count one event; False when the limit for the window is exceeded."""
        now = self._clock()
        with self._lock:
            self._sweep(now)
            entry = self._hits.get((bucket, key))
            if entry is None:
                entry = []
                self._hits[(bucket, key)] = entry
            cutoff = now - window
            while entry and entry[0] <= cutoff:
                entry.pop(0)
            if len(entry) >= limit:
                return False
            entry.append(now)
            return True

    def over(self, bucket, key, limit, window):
        """True when the key already reached the limit (does not count)."""
        now = self._clock()
        with self._lock:
            entry = self._hits.get((bucket, key)) or []
            cutoff = now - window
            return sum(1 for t in entry if t > cutoff) >= limit

    def _sweep(self, now):
        if now - self._last_sweep < 60 and len(self._hits) < self.MAX_KEYS:
            return
        self._last_sweep = now
        horizon = now - 3600
        for k in [k for k, v in self._hits.items() if not v or v[-1] <= horizon]:
            del self._hits[k]
        if len(self._hits) >= self.MAX_KEYS:
            self._hits.clear()


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

    def child_token(self, general_id, nonce):
        """The personal token a general link's submission gets: fixed per nonce,
        so a double submit lands on the same personal link."""
        raw = hmac.new(self._key, ("child:%d:%s" % (general_id, nonce)).encode("utf-8"),
                       hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw[:24]).decode("ascii").rstrip("=")

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
