"""SQLite storage: one file, WAL, one connection guarded by a lock.

Every write runs in ``BEGIN IMMEDIATE`` so a submit and a pull never
interleave, also across processes (gunicorn workers).
"""

import contextlib
import json
import os
import secrets
import sqlite3
import threading

from .forms import mask_receiver

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    key TEXT PRIMARY KEY,
    mime TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    data BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    ref TEXT NOT NULL,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL,
    max_items INTEGER NOT NULL,
    ask_account INTEGER NOT NULL,
    max_uses INTEGER,
    uses INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    origin_nonce TEXT
);
CREATE INDEX IF NOT EXISTS links_owner ON links(owner);
CREATE INDEX IF NOT EXISTS links_parent ON links(parent_id);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id INTEGER NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    rev INTEGER NOT NULL,
    state TEXT NOT NULL,
    items TEXT NOT NULL,
    receiver TEXT NOT NULL,
    account TEXT,
    nonce TEXT,
    submitted_at REAL NOT NULL,
    taken_at REAL,
    acked_at REAL,
    purged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS subs_owner_state ON submissions(owner, state);
"""

LINK_COLUMNS = ("id", "token_hash", "ref", "owner", "kind", "max_items", "ask_account",
                "max_uses", "uses", "parent_id", "created_at", "expires_at", "revoked_at")
SUB_COLUMNS = ("id", "link_id", "owner", "rev", "state", "items", "receiver", "account",
               "nonce", "submitted_at", "taken_at", "acked_at", "purged")


def _open_private(path):
    """Create the database file (and its folder) readable by the owner only."""
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o700, exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _row(columns, row):
    return dict(zip(columns, row)) if row is not None else None


class Store(object):
    def __init__(self, path, clock):
        self.path = path
        self.clock = clock
        _open_private(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False,
                                     isolation_level=None)
        c = self._conn
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        # Freed and overwritten content is zeroed, and the WAL file is cut back
        # to nothing after a checkpoint, so page images holding a receiver do
        # not linger after ack / purge (stock SQLite keeps the WAL at full size).
        c.execute("PRAGMA secure_delete=ON")
        c.execute("PRAGMA journal_size_limit=0")
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                c.execute(stmt)
        columns = [r[1] for r in c.execute("PRAGMA table_info(links)")]
        if "origin_nonce" not in columns:  # databases made before 2026-11-13
            c.execute("ALTER TABLE links ADD COLUMN origin_nonce TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS links_origin ON links(parent_id, origin_nonce)")
        with self.tx() as cur:
            if self._meta(cur, "secret") is None:
                self._set_meta(cur, "secret", secrets.token_hex(32))
            if self._meta(cur, "catalog_version") is None:
                self._set_meta(cur, "catalog_version", "0")
                self._set_meta(cur, "catalog_json", "[]")
                self._set_meta(cur, "catalog_updated_at", "")
            if self._meta(cur, "schema") is None:
                self._set_meta(cur, "schema", "1")

    # -- plumbing ----------------------------------------------------------

    @contextlib.contextmanager
    def tx(self):
        """A write transaction (BEGIN IMMEDIATE) under the process lock.

        Whatever fails (the body or the COMMIT itself), the shared connection
        is left outside any transaction, so later writes keep working.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
                cur.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def _rollback(self):
        try:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def checkpoint(self):
        """Copy the WAL into the database and truncate it to zero bytes."""
        with self._lock:
            try:
                return self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error:
                return None

    @contextlib.contextmanager
    def read(self):
        with self._lock:
            yield self._conn.cursor()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @staticmethod
    def _meta(cur, key):
        row = cur.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set_meta(cur, key, value):
        cur.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, value))

    def meta(self, key):
        with self.read() as cur:
            return self._meta(cur, key)

    def set_meta(self, key, value):
        with self.tx() as cur:
            self._set_meta(cur, key, value)

    # -- catalogue ---------------------------------------------------------

    def catalog_version(self):
        return int(self.meta("catalog_version") or 0)

    def catalog_state(self):
        with self.read() as cur:
            version = int(self._meta(cur, "catalog_version") or 0)
            styles = json.loads(self._meta(cur, "catalog_json") or "[]")
            updated = self._meta(cur, "catalog_updated_at") or ""
        return version, (float(updated) if updated else None), styles

    def replace_catalog(self, styles, keys):
        """Replace the catalogue atomically; return ``(version, missing_keys)``.

        When an image key is missing nothing is written.
        """
        now = self.clock()
        with self.tx() as cur:
            have = set(r[0] for r in cur.execute("SELECT key FROM images"))
            missing = sorted(set(keys) - have)
            if missing:
                return None, missing
            version = int(self._meta(cur, "catalog_version") or 0) + 1
            self._set_meta(cur, "catalog_version", str(version))
            self._set_meta(cur, "catalog_json", json.dumps(styles, ensure_ascii=False,
                                                           separators=(",", ":")))
            self._set_meta(cur, "catalog_updated_at", repr(now))
            keep = set(keys)
            for (items,) in cur.execute("SELECT items FROM submissions").fetchall():
                for item in json.loads(items):
                    if item.get("image"):
                        keep.add(item["image"])
            for key in have - keep:
                cur.execute("DELETE FROM images WHERE key=?", (key,))
        return version, []

    # -- images ------------------------------------------------------------

    def put_image(self, key, mime, width, height, data):
        with self.tx() as cur:
            if cur.execute("SELECT 1 FROM images WHERE key=?", (key,)).fetchone():
                return False
            cur.execute("INSERT INTO images(key, mime, width, height, data, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (key, mime, width, height, sqlite3.Binary(data), self.clock()))
            return True

    def image_keys(self):
        with self.read() as cur:
            return [r[0] for r in cur.execute("SELECT key FROM images ORDER BY key")]

    def get_image(self, key):
        with self.read() as cur:
            row = cur.execute("SELECT mime, data FROM images WHERE key=?", (key,)).fetchone()
        return (row[0], bytes(row[1])) if row else None

    def image_sizes(self, keys):
        """``{key: (width, height)}`` for the given keys."""
        keys = list(set(keys))
        out = {}
        with self.read() as cur:
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                q = "SELECT key, width, height FROM images WHERE key IN (%s)" % ",".join("?" * len(part))
                for key, w, h in cur.execute(q, part):
                    out[key] = (w, h)
        return out

    # -- links -------------------------------------------------------------

    def create_link(self, token_hash, ref, owner, kind, max_items, ask_account, max_uses,
                    expires_at, parent_id=None, cur=None):
        sql = ("INSERT INTO links(token_hash, ref, owner, kind, max_items, ask_account, max_uses, "
               "uses, parent_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)")
        args = (token_hash, ref, owner, kind, max_items, 1 if ask_account else 0, max_uses,
                parent_id, self.clock(), expires_at)
        if cur is not None:
            cur.execute(sql, args)
            return cur.lastrowid
        with self.tx() as c:
            c.execute(sql, args)
            return c.lastrowid

    def link_by_hash(self, token_hash):
        """The link with this token hash; ``parent_revoked_at`` says whether the
        general link it came from was revoked."""
        cols = ",".join("l." + c for c in LINK_COLUMNS)
        with self.read() as cur:
            row = cur.execute(
                "SELECT %s, p.revoked_at FROM links l LEFT JOIN links p ON p.id=l.parent_id "
                "WHERE l.token_hash=?" % cols, (token_hash,)).fetchone()
        if row is None:
            return None
        link = _row(LINK_COLUMNS, row[:len(LINK_COLUMNS)])
        link["parent_revoked_at"] = row[len(LINK_COLUMNS)]
        return link

    def link_by_id(self, link_id):
        with self.read() as cur:
            row = cur.execute("SELECT %s FROM links WHERE id=?" % ",".join(LINK_COLUMNS),
                              (link_id,)).fetchone()
        return _row(LINK_COLUMNS, row)

    def revoke_link(self, link_id):
        with self.tx() as cur:
            row = cur.execute("SELECT revoked_at FROM links WHERE id=?", (link_id,)).fetchone()
            if row is None:
                return False
            if row[0] is None:
                cur.execute("UPDATE links SET revoked_at=? WHERE id=?", (self.clock(), link_id))
            return True

    def list_links(self, owner):
        cols = ",".join("l." + c for c in LINK_COLUMNS)
        with self.read() as cur:
            rows = cur.execute(
                "SELECT %s, s.state, s.rev FROM links l LEFT JOIN submissions s ON s.link_id=l.id "
                "WHERE l.owner=? ORDER BY l.id" % cols, (owner,)).fetchall()
        out = []
        for row in rows:
            link = _row(LINK_COLUMNS, row[:len(LINK_COLUMNS)])
            link["sub_state"] = row[len(LINK_COLUMNS)]
            link["sub_rev"] = row[len(LINK_COLUMNS) + 1]
            out.append(link)
        return out

    # -- submissions -------------------------------------------------------

    def submission_for_link(self, link_id):
        with self.read() as cur:
            row = cur.execute("SELECT %s FROM submissions WHERE link_id=?" % ",".join(SUB_COLUMNS),
                              (link_id,)).fetchone()
        return _decode_sub(_row(SUB_COLUMNS, row))

    def save_submission(self, link, clean, nonce):
        """Store a personal link's submission; return ``"saved"``, ``"duplicate"``
        (same nonce already stored) or ``"locked"`` (taken / acked)."""
        now = self.clock()
        with self.tx() as cur:
            row = cur.execute("SELECT id, state, nonce, rev FROM submissions WHERE link_id=?",
                              (link["id"],)).fetchone()
            if row is not None:
                sub_id, state, old_nonce, rev = row
                if state != "new":
                    return "locked"
                if nonce and old_nonce == nonce:
                    return "duplicate"
                cur.execute(
                    "UPDATE submissions SET rev=?, items=?, receiver=?, account=?, nonce=?, "
                    "submitted_at=? WHERE id=?",
                    (rev + 1, _dumps(clean["items"]), _dumps(clean["receiver"]), clean["account"],
                     nonce, now, sub_id))
            else:
                self._insert_sub(cur, link, clean, nonce, now)
                cur.execute("UPDATE links SET uses=uses+1 WHERE id=?", (link["id"],))
            return "saved"

    def _insert_sub(self, cur, link, clean, nonce, now):
        cur.execute(
            "INSERT INTO submissions(link_id, owner, rev, state, items, receiver, account, nonce, "
            "submitted_at) VALUES(?, ?, 1, 'new', ?, ?, ?, ?, ?)",
            (link["id"], link["owner"], _dumps(clean["items"]), _dumps(clean["receiver"]),
             clean["account"], nonce, now))

    def submit_general(self, general, child_hash, clean, nonce, expires_at):
        """Create the personal link (random token, only its hash stored) for a
        general link's submission.

        Returns ``"saved"``, ``"duplicate"`` (this form nonce was submitted
        already: the first submit wins) or ``"full"`` (``max_uses`` reached).
        """
        now = self.clock()
        with self.tx() as cur:
            if cur.execute("SELECT 1 FROM links WHERE parent_id=? AND origin_nonce=?",
                           (general["id"], nonce)).fetchone():
                return "duplicate"
            row = cur.execute("SELECT uses, max_uses FROM links WHERE id=?", (general["id"],)).fetchone()
            if row is None:
                return "full"
            uses, max_uses = row
            if max_uses is not None and uses >= max_uses:
                return "full"
            child_id = self.create_link(child_hash, general["ref"], general["owner"], "personal",
                                        general["max_items"], True, None, expires_at,
                                        parent_id=general["id"], cur=cur)
            cur.execute("UPDATE links SET origin_nonce=? WHERE id=?", (nonce, child_id))
            child = {"id": child_id, "owner": general["owner"]}
            self._insert_sub(cur, child, clean, nonce, now)
            cur.execute("UPDATE links SET uses=uses+1 WHERE id=?", (child_id,))
            cur.execute("UPDATE links SET uses=uses+1 WHERE id=?", (general["id"],))
            return "saved"

    def pull(self, owner):
        """Mark ``new`` as ``taken`` and return every un-acked submission."""
        now = self.clock()
        cols = ",".join("s." + c for c in SUB_COLUMNS)
        with self.tx() as cur:
            cur.execute("UPDATE submissions SET state='taken', taken_at=? WHERE owner=? AND state='new'",
                        (now, owner))
            rows = cur.execute(
                "SELECT %s, l.ref, l.parent_id FROM submissions s JOIN links l ON l.id=s.link_id "
                "WHERE s.owner=? AND s.state='taken' ORDER BY s.id" % cols, (owner,)).fetchall()
        out = []
        for row in rows:
            sub = _decode_sub(_row(SUB_COLUMNS, row[:len(SUB_COLUMNS)]))
            sub["ref"] = row[len(SUB_COLUMNS)]
            sub["parent_id"] = row[len(SUB_COLUMNS) + 1]
            out.append(sub)
        return out

    def ack(self, owner, ids):
        now = self.clock()
        acked, unknown = [], []
        with self.tx() as cur:
            for sub_id in ids:
                row = cur.execute("SELECT state, receiver FROM submissions WHERE id=? AND owner=?",
                                  (sub_id, owner)).fetchone()
                if row is None or row[0] == "new":
                    unknown.append(sub_id)
                    continue
                if row[0] == "taken":
                    masked = mask_receiver(json.loads(row[1]))
                    cur.execute("UPDATE submissions SET state='acked', acked_at=?, receiver=?, "
                                "account=NULL WHERE id=?", (now, _dumps(masked), sub_id))
                acked.append(sub_id)
        self.checkpoint()
        return acked, unknown

    def status(self):
        now = self.clock()
        with self.read() as cur:
            active = cur.execute("SELECT COUNT(*) FROM links WHERE revoked_at IS NULL AND expires_at>?",
                                 (now,)).fetchone()[0]
            counts = dict(cur.execute("SELECT state, COUNT(*) FROM submissions GROUP BY state").fetchall())
        return active, counts

    # -- purge -------------------------------------------------------------

    def purge(self, retention_days, link_grace_days=30):
        """Strip personal fields after the retention time; drop dead links."""
        now = self.clock()
        cutoff = now - retention_days * 86400.0
        link_cutoff = now - link_grace_days * 86400.0
        with self.tx() as cur:
            rows = cur.execute("SELECT id, receiver FROM submissions WHERE purged=0 AND submitted_at<?",
                               (cutoff,)).fetchall()
            for sub_id, receiver in rows:
                cur.execute("UPDATE submissions SET receiver=?, account=NULL, purged=1, nonce=NULL "
                            "WHERE id=?", (_dumps(mask_receiver(json.loads(receiver))), sub_id))
            dead = cur.execute(
                "SELECT l.id FROM links l LEFT JOIN submissions s ON s.link_id=l.id "
                "WHERE s.id IS NULL AND l.uses=0 AND "
                "((l.revoked_at IS NOT NULL AND l.revoked_at<?) OR l.expires_at<?)",
                (link_cutoff, link_cutoff)).fetchall()
            for (link_id,) in dead:
                cur.execute("DELETE FROM links WHERE id=?", (link_id,))
        self.checkpoint()
        return len(rows), len(dead)

    def try_claim_purge(self, interval):
        """True when this process should run the purge now (at most once per interval,
        coordinated through the DB so several processes do not all run it)."""
        now = self.clock()
        with self.tx() as cur:
            last = self._meta(cur, "last_purge")
            if last and now - float(last) < interval:
                return False
            self._set_meta(cur, "last_purge", repr(now))
            return True


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_sub(sub):
    if sub is None:
        return None
    sub["items"] = json.loads(sub["items"])
    sub["receiver"] = json.loads(sub["receiver"])
    return sub
