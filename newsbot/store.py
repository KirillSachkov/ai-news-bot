"""SQLite state: seen items, pending queue, delivery log, feedback and weights.

One file database, no server. Everything the bot must remember between runs
lives here: what has already been sent, what the source HTTP validators were,
how the operator rated past items, and the learned scoring adjustments.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    nid         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT UNIQUE NOT NULL,
    source      TEXT NOT NULL,
    source_group TEXT,
    title       TEXT NOT NULL,
    url         TEXT,
    summary     TEXT,
    published   TEXT,
    discovered  TEXT NOT NULL,
    score       REAL DEFAULT 0,
    status      TEXT DEFAULT 'pending',
    sent_at     TEXT,
    feedback    TEXT,
    feedback_at TEXT,
    extra       TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(discovered);
CREATE INDEX IF NOT EXISTS idx_items_feedback ON items(feedback);

CREATE TABLE IF NOT EXISTS sources (
    name            TEXT PRIMARY KEY,
    group_name      TEXT,
    etag            TEXT,
    last_modified   TEXT,
    last_ok         TEXT,
    last_error      TEXT,
    fail_count      INTEGER DEFAULT 0,
    ok_count        INTEGER DEFAULT 0,
    last_item_seen  TEXT
);

CREATE TABLE IF NOT EXISTS sends (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    nid     INTEGER,
    kind    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sends_ts ON sends(ts);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS weights (
    kind  TEXT NOT NULL,
    key   TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    n     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS feedback_log (
    ts       TEXT NOT NULL,
    nid      INTEGER,
    source   TEXT,
    verdict  TEXT,
    keywords TEXT
);

CREATE TABLE IF NOT EXISTS route_state (
    route       TEXT PRIMARY KEY,
    blocked_until TEXT,
    last_ok     TEXT,
    fail_count  INTEGER DEFAULT 0,
    last_status TEXT
);
"""

_TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|yclid|mc_cid|mc_eid|igshid|ref|ref_src|source|"
    r"cmpid|ocid|spm|si|s|t|at_medium|at_campaign)$",
    re.I,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode_extra(row):
    """SQLite stores `extra` as JSON text; callers expect a dict."""
    if isinstance(row, dict) and isinstance(row.get("extra"), str):
        try:
            row["extra"] = json.loads(row["extra"])
        except ValueError:
            row["extra"] = {}
    return row


def canonical_url(url):
    """Normalize a URL so the same article from different routes collapses."""
    if not url:
        return ""
    url = url.strip()
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url)
        scheme = (parts.scheme or "https").lower()
        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not _TRACKING_PARAMS.match(k)]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((scheme, netloc, path, urlencode(query), ""))
    except Exception:
        return url


def title_key(title):
    """A stable key for cross-source duplicate detection."""
    text = (title or "").lower()
    text = re.sub(r"[^a-z0-9а-яё ]+", " ", text)
    words = [w for w in text.split() if len(w) > 2]
    words.sort()
    return hashlib.sha1(" ".join(words[:14]).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ items
    def add_item(self, item, score_row=None):
        """Insert a fetched item. Returns nid if newly inserted, else None."""
        try:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO items
                   (uid, source, source_group, title, url, summary, published,
                    discovered, extra)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    item["uid"], item["source"], item.get("source_group"),
                    item["title"], item.get("url"), item.get("summary"),
                    item.get("published"), utc_now(),
                    json.dumps(item.get("extra") or {}, ensure_ascii=False),
                ),
            )
            self.db.commit()
            return cursor.lastrowid if cursor.rowcount else None
        except sqlite3.Error:
            return None

    def set_score(self, nid, score):
        self.db.execute("UPDATE items SET score=? WHERE nid=?", (round(score, 3), nid))
        self.db.commit()

    def exists(self, uid):
        row = self.db.execute("SELECT 1 FROM items WHERE uid=?", (uid,)).fetchone()
        return row is not None

    def exists_url(self, url):
        canonical = canonical_url(url)
        if not canonical:
            return False
        row = self.db.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone()
        return row is not None

    def pending(self, limit=50):
        rows = self.db.execute(
            """SELECT * FROM items WHERE status='pending'
               ORDER BY score DESC, discovered DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [decode_extra(dict(r)) for r in rows]

    def mark(self, nid, status, sent=False):
        if sent:
            self.db.execute(
                "UPDATE items SET status=?, sent_at=? WHERE nid=?",
                (status, utc_now(), nid),
            )
            self.db.execute(
                "INSERT INTO sends (ts, nid, kind) VALUES (?,?,?)",
                (utc_now(), nid, "news"),
            )
        else:
            self.db.execute("UPDATE items SET status=? WHERE nid=?", (status, nid))
        self.db.commit()

    def mark_all_pending_dropped(self, reason="bootstrap"):
        self.db.execute(
            "UPDATE items SET status='dropped' WHERE status='pending'")
        self.db.commit()

    def expire_pending(self, ttl_hours):
        cutoff = datetime.fromtimestamp(
            time.time() - ttl_hours * 3600, tz=timezone.utc
        ).isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE items SET status='expired' WHERE status='pending' AND discovered < ?",
            (cutoff,),
        )
        self.db.commit()

    def backfill_links(self, uid, links):
        """Attach outbound links to an item stored before links were persisted.

        Telegram channels quote primary sources; their x.com links are the
        cheapest X discovery channel we have, so old rows are upgraded in place
        when the post is seen again.
        """
        if not links:
            return
        row = self.db.execute("SELECT extra FROM items WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        try:
            extra = json.loads(row["extra"] or "{}")
        except ValueError:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        if extra.get("links"):
            return
        extra["links"] = links[:12]
        self.db.execute("UPDATE items SET extra=? WHERE uid=?",
                        (json.dumps(extra, ensure_ascii=False), uid))
        self.db.commit()

    def item(self, nid):
        row = self.db.execute("SELECT * FROM items WHERE nid=?", (nid,)).fetchone()
        return decode_extra(dict(row)) if row else None

    def set_feedback(self, nid, verdict, keywords=None):
        row = self.item(nid)
        self.db.execute(
            "UPDATE items SET feedback=?, feedback_at=? WHERE nid=?",
            (verdict, utc_now(), nid),
        )
        self.db.execute(
            "INSERT INTO feedback_log (ts, nid, source, verdict, keywords) VALUES (?,?,?,?,?)",
            (utc_now(), nid, (row or {}).get("source"), verdict,
             ",".join(keywords or [])),
        )
        self.db.commit()
        return row

    def recent_sent(self, limit=20):
        rows = self.db.execute(
            """SELECT * FROM items WHERE status='sent'
               ORDER BY sent_at DESC LIMIT ?""", (limit,)).fetchall()
        return [decode_extra(dict(r)) for r in rows]

    def counts(self):
        out = {}
        for row in self.db.execute(
                "SELECT status, COUNT(*) c FROM items GROUP BY status"):
            out[row["status"]] = row["c"]
        out["feedback"] = {
            r["feedback"]: r["c"] for r in self.db.execute(
                "SELECT feedback, COUNT(*) c FROM items WHERE feedback IS NOT NULL "
                "GROUP BY feedback")
        }
        return out

    # ---------------------------------------------------------------- sources
    def source_state(self, name):
        row = self.db.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def touch_source_ok(self, name, group=None, etag=None, last_modified=None,
                        not_modified=False):
        state = self.source_state(name)
        if state is None:
            self.db.execute(
                "INSERT INTO sources (name, group_name, ok_count) VALUES (?,?,0)",
                (name, group))
            state = {"etag": None, "last_modified": None, "ok_count": 0}
        self.db.execute(
            """UPDATE sources SET group_name=?, last_ok=?, last_error=NULL,
                   fail_count=0, ok_count=ok_count+1,
                   etag=COALESCE(?, etag),
                   last_modified=COALESCE(?, last_modified)
               WHERE name=?""",
            (group, utc_now(), etag, last_modified, name),
        )
        self.db.commit()

    def touch_source_error(self, name, error, group=None):
        state = self.source_state(name)
        if state is None:
            self.db.execute(
                "INSERT INTO sources (name, group_name, fail_count) VALUES (?,?,0)",
                (name, group))
        self.db.execute(
            "UPDATE sources SET last_error=?, fail_count=fail_count+1, group_name=? "
            "WHERE name=?",
            (str(error)[:300], group, name),
        )
        self.db.commit()

    def source_health(self):
        rows = self.db.execute(
            """SELECT name, group_name, ok_count, fail_count, last_ok, last_error
               FROM sources ORDER BY name""").fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------------- kv
    def kv_get(self, key, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, key, value):
        self.db.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value) if value is not None else None),
        )
        self.db.commit()

    # ---------------------------------------------------------------- weights
    def bump_weight(self, kind, key, delta, cap=3.0):
        if not key:
            return
        row = self.db.execute(
            "SELECT value, n FROM weights WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        if row is None:
            value, n = 0.0, 0
        else:
            value, n = row["value"], row["n"]
        value = max(-cap, min(cap, value + delta))
        self.db.execute(
            "INSERT INTO weights (kind, key, value, n) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, key) DO UPDATE SET value=excluded.value, n=excluded.n",
            (kind, key, value, n + 1),
        )
        self.db.commit()

    def weights(self):
        out = {}
        for row in self.db.execute("SELECT kind, key, value, n FROM weights"):
            out.setdefault(row["kind"], {})[row["key"]] = (row["value"], row["n"])
        return out

    # ---------------------------------------------------------------- delivery
    def sends_since(self, cutoff_iso):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM sends WHERE ts >= ? ORDER BY ts", (cutoff_iso,))]

    def last_send_at(self):
        row = self.db.execute("SELECT ts FROM sends ORDER BY id DESC LIMIT 1").fetchone()
        return row["ts"] if row else None

    # ------------------------------------------------------------ route health
    def route_blocked(self, route):
        """True while a discovery route is cooling down after a block."""
        row = self.db.execute(
            "SELECT blocked_until FROM route_state WHERE route=?", (route,)).fetchone()
        if not row or not row["blocked_until"]:
            return False
        try:
            until = datetime.fromisoformat(row["blocked_until"])
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until

    def route_note(self, route, ok, status=None, cooldown_minutes=30):
        """Record a route outcome; block it for a while after a block/limit."""
        if ok:
            self.db.execute(
                "INSERT INTO route_state (route, last_ok, fail_count, last_status, "
                "blocked_until) VALUES (?,?,0,?,NULL) "
                "ON CONFLICT(route) DO UPDATE SET last_ok=excluded.last_ok, "
                "fail_count=0, last_status=excluded.last_status, blocked_until=NULL",
                (route, utc_now(), str(status or "ok")))
        else:
            blocked_until = None
            if status in (403, 429, 0, 502, 503):
                blocked_until = (
                    datetime.now(timezone.utc)
                    + timedelta(minutes=cooldown_minutes)
                ).isoformat(timespec="seconds")
            self.db.execute(
                "INSERT INTO route_state (route, fail_count, last_status, blocked_until) "
                "VALUES (?,1,?,?) ON CONFLICT(route) DO UPDATE SET "
                "fail_count=fail_count+1, last_status=excluded.last_status, "
                "blocked_until=COALESCE(excluded.blocked_until, route_state.blocked_until)",
                (route, str(status), blocked_until))
        self.db.commit()

    def route_health(self):
        rows = self.db.execute(
            "SELECT route, blocked_until, last_ok, fail_count, last_status "
            "FROM route_state ORDER BY route").fetchall()
        return [dict(r) for r in rows]

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass
