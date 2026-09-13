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
import threading
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

-- A "story" is one real-world event, carried by several outlets. Grouping the
-- carriers is what stops the same news arriving twice, and the number of
-- independent carriers is the cheapest honest measure of how big it is.
CREATE TABLE IF NOT EXISTS stories (
    story_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    signature  TEXT,
    entities   TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    sent_nid   INTEGER,
    sent_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_stories_last_seen ON stories(last_seen);

CREATE TABLE IF NOT EXISTS story_items (
    story_id INTEGER NOT NULL,
    nid      INTEGER NOT NULL,
    source   TEXT,
    grp      TEXT,
    ts       TEXT NOT NULL,
    PRIMARY KEY (story_id, nid)
);
CREATE INDEX IF NOT EXISTS idx_story_items_nid ON story_items(nid);
"""

# Columns added after the first release; SQLite has no "ADD COLUMN IF NOT
# EXISTS", so they are applied one by one and duplicates are ignored.
MIGRATIONS = (
    "ALTER TABLE items ADD COLUMN curl TEXT",
    "ALTER TABLE items ADD COLUMN story_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_items_curl ON items(curl)",
    "CREATE INDEX IF NOT EXISTS idx_items_story ON items(story_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_published ON items(published)",
)

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


# Words that survive translation: product names, labs, model ids, versions.
# A Russian headline about the same event keeps "Anthropic", "DeepSeek" and
# "Claude" verbatim, which is what lets a RU and an EN carrier of one story be
# recognised as the same story. Generic tech vocabulary is excluded, or every
# AI headline would match every other one.
_GENERIC_TOKENS = {
    "the", "and", "for", "with", "from", "that", "this", "his", "her", "its",
    "new", "now", "how", "why", "what", "who", "will", "can", "has", "have",
    "are", "was", "were", "been", "but", "not", "you", "your", "our", "all",
    "ai", "llm", "llms", "model", "models", "tech", "app", "apps", "data",
    "news", "says", "said", "report", "reports", "update", "updates", "via",
    "web", "api", "apis", "using", "used", "use", "more", "than", "into",
    "about", "after", "before", "over", "under", "out", "off", "get", "gets",
    "million", "billion", "company", "companies", "users", "user", "week",
    "day", "days", "year", "years", "time", "first", "best", "top", "how-to",
    # Action verbs and framing nouns. These appear in a large share of AI
    # headlines, so two items sharing only these share nothing at all - and
    # letting them count is what collapsed every story into one blob.
    "release", "releases", "released", "launch", "launches", "launched",
    "announce", "announces", "announced", "introduce", "introduces", "unveils",
    "adds", "brings", "builds", "makes", "made", "wants", "plans", "sets",
    "research", "researchers", "framework", "platform", "tool", "tools",
    "system", "systems", "team", "teams", "startup", "source", "open",
    "based", "free", "million", "版", "run", "runs", "show", "hits", "scores",
    "across", "their", "them", "они", "with", "without", "under", "above",
    "version", "support", "supports", "adding", "available", "general",
    "benchmark", "benchmarks", "training", "inference", "agent", "agents",
}

# Names so common in this feed that sharing them proves nothing: half the AI
# headlines of any given day mention OpenAI or Claude. Two items must share
# something rarer than these before they count as the same event - otherwise an
# encyclopedia entry on Anthropic merges with the day's Anthropic news.
_COMMON_ENTITIES = {
    "openai", "anthropic", "claude", "chatgpt", "gpt", "google", "deepmind",
    "microsoft", "meta", "nvidia", "apple", "amazon", "gemini", "copilot",
    "llama", "mistral", "huggingface", "hugging", "face", "grok", "xai",
}

_TOKEN_RE = re.compile(r"[a-z][a-z0-9.\-]{2,}")
_VERSION_RE = re.compile(r"\b[a-z]{2,}[- ]?v?\d+(?:\.\d+)*\b")


def entity_tokens(*texts):
    """Latin-script names and version strings that identify a story."""
    blob = " ".join(t or "" for t in texts).lower()
    tokens = set()
    for match in _TOKEN_RE.findall(blob):
        token = match.strip(".-")
        if len(token) < 3 or token in _GENERIC_TOKENS:
            continue
        if token.isdigit():
            continue
        tokens.add(token)
    for match in _VERSION_RE.findall(blob):
        tokens.add(match.replace(" ", "-"))
    return tokens


class Store:
    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # check_same_thread=False: the fetch worker and the Telegram loop are
        # different threads. Writes are serialised by _lock; WAL keeps a reader
        # from blocking the writer, and the busy timeout absorbs the rest.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error:
            pass
        self.db.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                self.db.execute(statement)
            except sqlite3.OperationalError:
                pass  # column or index already present
        self.db.commit()

    # ------------------------------------------------------------------ items
    def add_item(self, item, score_row=None):
        """Insert a fetched item. Returns nid if newly inserted, else None."""
        try:
            with self._lock:
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO items
                       (uid, source, source_group, title, url, curl, summary,
                        published, discovered, extra)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["uid"], item["source"], item.get("source_group"),
                        item["title"], item.get("url"),
                        canonical_url(item.get("url")),
                        item.get("summary"), item.get("published"), utc_now(),
                        json.dumps(item.get("extra") or {}, ensure_ascii=False),
                    ),
                )
                self.db.commit()
                return cursor.lastrowid if cursor.rowcount else None
        except sqlite3.Error:
            return None

    def set_score(self, nid, score):
        with self._lock:
            self.db.execute("UPDATE items SET score=? WHERE nid=?",
                            (round(score, 3), nid))
            self.db.commit()

    def set_url(self, nid, url):
        """Store a link that was resolved after the row was written.

        Aggregator feeds hand out redirect blobs that are resolved at send time;
        without persisting the result the buttons rebuilt later (after a rating)
        would point back at the opaque redirect.
        """
        with self._lock:
            self.db.execute("UPDATE items SET url=?, curl=? WHERE nid=?",
                            (url, canonical_url(url), nid))
            self.db.commit()

    def exists(self, uid):
        row = self.db.execute("SELECT 1 FROM items WHERE uid=?", (uid,)).fetchone()
        return row is not None

    def exists_url(self, url):
        """True when this article is already stored, ignoring tracking junk."""
        canonical = canonical_url(url)
        if not canonical:
            return False
        row = self.db.execute(
            "SELECT 1 FROM items WHERE curl=? OR url=? LIMIT 1",
            (canonical, url),
        ).fetchone()
        return row is not None

    def pending(self, limit=50, order="score"):
        """Pending items.

        `order="fresh"` is what the scorer must use: new rows carry score 0, so
        ranking by score buries them behind everything already scored and, on a
        long queue, they would never be looked at at all.
        """
        if order == "fresh":
            sql = ("SELECT * FROM items WHERE status='pending' "
                   "ORDER BY discovered DESC LIMIT ?")
        else:
            sql = ("SELECT * FROM items WHERE status='pending' "
                   "ORDER BY score DESC, discovered DESC LIMIT ?")
        rows = self.db.execute(sql, (limit,)).fetchall()
        return [decode_extra(dict(r)) for r in rows]

    def mark(self, nid, status, sent=False, kind="news"):
        with self._lock:
            if sent:
                self.db.execute(
                    "UPDATE items SET status=?, sent_at=? WHERE nid=?",
                    (status, utc_now(), nid),
                )
                self.db.execute(
                    "INSERT INTO sends (ts, nid, kind) VALUES (?,?,?)",
                    (utc_now(), nid, kind),
                )
                # Remember that this story has been told, so later carriers of
                # the same event are recognised as repeats rather than news.
                self.db.execute(
                    "UPDATE stories SET sent_nid=?, sent_at=? WHERE story_id="
                    "(SELECT story_id FROM items WHERE nid=?) AND sent_nid IS NULL",
                    (nid, utc_now(), nid),
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

    # ----------------------------------------------------------------- stories
    def assign_story(self, nid, title, summary, source, group,
                     window_minutes=90, min_shared=2, fuzzy=0.45):
        """Attach an item to the story it belongs to, creating one if needed.

        Matching runs on two signals, because neither alone is enough:
        shared proper names (which survive translation, so a Russian and an
        English carrier of one event still meet) and fuzzy title similarity
        (which catches rewrites inside one language that share no rare name).
        """
        # Title plus the opening of the summary: a full abstract adds dozens
        # of generic technical words that drown the identifying names.
        entities = entity_tokens(title, (summary or "")[:300])
        signature = " ".join(sorted(
            w for w in re.findall(r"[a-zа-яё0-9]{3,}", (title or "").lower())))
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")

        with self._lock:
            rows = self.db.execute(
                "SELECT story_id, signature, entities FROM stories "
                "WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT 400",
                (cutoff,)).fetchall()

            best_id, best_rank = None, 0.0
            for row in rows:
                try:
                    core = set(json.loads(row["entities"] or "[]"))
                except ValueError:
                    core = set()
                rank = 0.0
                shared = entities & core
                if len(shared) >= min_shared and core and entities:
                    # Containment, not raw count: two headlines that share two
                    # names out of three are the same event, two that share two
                    # names out of twenty are both merely about big AI labs.
                    overlap = len(shared) / float(min(len(entities), len(core)))
                    rare = shared - _COMMON_ENTITIES
                    # Containment is required in every case. Raw counts do not
                    # survive long text: two unrelated arXiv abstracts share
                    # three technical words without effort, and trusting the
                    # count alone merged 71 papers into one "story".
                    # Beyond that, two shared names are evidence only when one
                    # of them is not a household name.
                    if overlap >= 0.5 and (len(shared) >= 3 or rare):
                        rank = 1.0 + overlap + 0.5 * len(rare)
                if rank == 0.0 and signature and row["signature"]:
                    # Word-set overlap, not string similarity. Comparing two
                    # alphabetically sorted sentences with SequenceMatcher
                    # measures how similar their letters are, which is why an
                    # Apple interview and a Russian 5G item once landed in the
                    # same story: unrelated sentences in one language share a
                    # lot of letters in sorted order.
                    mine = set(signature.split())
                    theirs = set(row["signature"].split())
                    common = mine & theirs
                    if len(common) >= 3:
                        union = mine | theirs
                        jaccard = len(common) / float(len(union) or 1)
                        if jaccard >= fuzzy:
                            rank = jaccard
                if rank > best_rank:
                    best_id, best_rank = row["story_id"], rank

            now = utc_now()
            if best_id is None:
                cursor = self.db.execute(
                    "INSERT INTO stories (signature, entities, first_seen, last_seen) "
                    "VALUES (?,?,?,?)",
                    (signature, json.dumps(sorted(entities)), now, now))
                best_id = cursor.lastrowid
            else:
                # The seed item's names stay the story's identity. Merging every
                # match's names in made the set grow without bound, and a story
                # that knows a hundred names matches everything: one run ended
                # with 123 unrelated items in a single "story".
                self.db.execute(
                    "UPDATE stories SET last_seen=? WHERE story_id=?", (now, best_id))

            self.db.execute(
                "INSERT OR IGNORE INTO story_items (story_id, nid, source, grp, ts) "
                "VALUES (?,?,?,?,?)", (best_id, nid, source, group, now))
            self.db.execute("UPDATE items SET story_id=? WHERE nid=?", (best_id, nid))
            self.db.commit()
        return best_id

    def story_stats(self, story_id):
        """How many independent carriers this story has, and whether it was sent."""
        if not story_id:
            return {"sources": 0, "groups": 0, "sent_nid": None, "first_seen": None}
        row = self.db.execute(
            "SELECT COUNT(DISTINCT source) s, COUNT(DISTINCT grp) g "
            "FROM story_items WHERE story_id=?", (story_id,)).fetchone()
        head = self.db.execute(
            "SELECT sent_nid, first_seen FROM stories WHERE story_id=?",
            (story_id,)).fetchone()
        return {
            "sources": (row["s"] if row else 0) or 0,
            "groups": (row["g"] if row else 0) or 0,
            "sent_nid": head["sent_nid"] if head else None,
            "first_seen": head["first_seen"] if head else None,
        }

    def story_carriers(self, story_id):
        """(source, group) pairs carrying a story."""
        if not story_id:
            return []
        rows = self.db.execute(
            "SELECT DISTINCT source, grp FROM story_items WHERE story_id=?",
            (story_id,)).fetchall()
        return [(r["source"], r["grp"]) for r in rows]

    def sent_since(self, hours, limit=60):
        """Items delivered in the last `hours`, newest first."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT nid, title, summary, source, story_id, sent_at FROM items "
            "WHERE status='sent' AND sent_at >= ? ORDER BY sent_at DESC LIMIT ?",
            (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_send_id(self):
        row = self.db.execute("SELECT MAX(id) m FROM sends").fetchone()
        return (row["m"] if row else 0) or 0

    def prune_stories(self, keep_hours=72):
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=keep_hours)).isoformat(timespec="seconds")
        with self._lock:
            self.db.execute(
                "DELETE FROM story_items WHERE story_id IN "
                "(SELECT story_id FROM stories WHERE last_seen < ?)", (cutoff,))
            self.db.execute("DELETE FROM stories WHERE last_seen < ?", (cutoff,))
            self.db.commit()

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass
