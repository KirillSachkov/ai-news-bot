"""X/Twitter adapter: the free cascade (option A).

Nothing here needs an API key, an account, a cookie or a session token. It is
built as two independent stages, because the reliable free routes only give
content by post ID while only the unstable routes give a timeline:

  Stage 1, discovery (which posts are new?):
      twiiit.com -> rss.xcancel.com -> other Nitter mirrors
      Each is an RSS feed of a public profile. All of them are third-party
      mirrors and can go dark without notice, so we try them in order and treat
      "all routes silent" as a health problem, not as "no news".

  Stage 2, content (what does post <id> actually say?):
      cdn.syndication.twimg.com -> api.fxtwitter.com -> api.vxtwitter.com
      These are stable: the first is X's own embed backend (it must keep
      working or every embedded tweet on the web breaks), the others are
      independent mirrors.

Keep the retry/backoff modest: these are free third-party resources.
"""

from __future__ import annotations

import json
import re
import time

from .http import fetch, fetch_json

FEED_READER_UA = "Miniflux/2.2.5 (+https://miniflux.app)"

# Checked live on 2026-09-11, after X's August cease-and-desist and the
# project's 6 September restart. Only xcancel answers with an actual feed:
#   twiiit      -> redirects to a random Nitter, which serves an HTML error page
#   nitter-prd  -> TLS protocol error
#   nitter-tk   -> HTML instead of a feed
#   nitter.net  -> TLS handshake timeout
# The dead four cost ~31s per handle per cycle and returned nothing, so they are
# gone rather than kept "just in case". Re-add one only after it is seen to work.
#
# xcancel requires a one-time whitelist of the reader, keyed to this exact
# User-Agent: do not change FEED_READER_UA without re-requesting the whitelist.
DISCOVERY_ROUTES = (
    # (template, headers, note)
    ("https://rss.xcancel.com/%s/rss", {"User-Agent": FEED_READER_UA}, "xcancel"),
)

_STATUS_RE = re.compile(r"/status(?:es)?/(\d{6,25})")

CONTENT_ROUTES = ("syndication", "fxtwitter", "vxtwitter")

# Optional route-health tracker (a Store instance), injected by the pipeline.
# Mirrors block by IP after a few requests, so a route that answers 403/429 is
# put on cooldown instead of being hammered on every cycle.
_ROUTE_HEALTH = None
_STORE = None


def set_store(store):
    """Inject the state store: route health plus the X link miner need it."""
    global _ROUTE_HEALTH, _STORE
    _ROUTE_HEALTH = store
    _STORE = store


def set_route_health(store):
    set_store(store)


def _tweet_id_from_url(url):
    if not url:
        return None
    match = _STATUS_RE.search(url)
    return match.group(1) if match else None


def parse_status_feed(payload, handle):
    """Extract (tweet_id, url, published, text) tuples from a Nitter RSS feed."""
    from .feeds import parse_feed  # local import keeps module deps one-way

    items = parse_feed(payload if isinstance(payload, bytes)
                       else payload.encode("utf-8"))
    out = []
    for entry in items:
        tweet_id = _tweet_id_from_url(entry.get("url")) or _tweet_id_from_url(
            entry.get("uid"))
        if not tweet_id:
            continue
        out.append({
            "tweet_id": tweet_id,
            "url": "https://x.com/%s/status/%s" % (handle, tweet_id),
            "published": entry.get("published"),
            "text": entry.get("summary") or entry.get("title") or "",
        })
    return out


def discover(handle, limit=15, preferred=None):
    """Return (posts, route_name, error).

    `preferred` is an optional list of route names to try first, so a source can
    be pinned to the mirror that actually works for you.
    """
    errors = []
    routes = list(DISCOVERY_ROUTES)
    if preferred:
        order = {name: index for index, name in enumerate(preferred)}
        routes.sort(key=lambda entry: order.get(entry[2], 99))
    for template, headers, name in routes:
        if _ROUTE_HEALTH is not None and _ROUTE_HEALTH.route_blocked(name):
            errors.append("%s: cooling down after a block" % name)
            continue
        url = template % handle
        result = fetch(url, extra_headers=headers, retries=1, timeout=20)
        if not result.ok:
            errors.append("%s: %s" % (name, result.error or result.status))
            if _ROUTE_HEALTH is not None:
                _ROUTE_HEALTH.route_note(name, False, result.status)
            continue
        text = result.text()
        if "not yet whitelist" in text.lower():
            errors.append("%s: this reader is not whitelisted yet "
                          "(email rss@xcancel.com the ID from the feed to get "
                          "whitelisted, once)" % name)
            continue
        try:
            posts = parse_status_feed(result.body, handle)
        except ValueError as exc:
            errors.append("%s: %s" % (name, exc))
            if _ROUTE_HEALTH is not None:
                _ROUTE_HEALTH.route_note(name, False, "parse")
            continue
        except Exception as exc:
            errors.append("%s: parse %s" % (name, exc))
            continue
        if posts:
            if _ROUTE_HEALTH is not None:
                _ROUTE_HEALTH.route_note(name, True)
            return posts[:limit], name, None
        errors.append("%s: empty feed" % name)
    return [], None, "; ".join(errors) or "no discovery route answered"


def content(tweet_id, handle=None, screen_name=None):
    """Fetch one post's content. Returns (dict, route, error)."""
    handle = screen_name or handle
    routes = []
    if handle:
        routes.append(("syndication",
                       "https://cdn.syndication.twimg.com/tweet-result?id=%s&token=0"
                       % tweet_id))
        routes.append(("fxtwitter",
                       "https://api.fxtwitter.com/%s/status/%s" % (handle, tweet_id)))
        routes.append(("vxtwitter",
                       "https://api.vxtwitter.com/%s/status/%s" % (handle, tweet_id)))
    else:
        routes.append(("syndication",
                       "https://cdn.syndication.twimg.com/tweet-result?id=%s&token=0"
                       % tweet_id))

    errors = []
    for name, url in routes:
        try:
            data = fetch_json(url, extra_headers={"User-Agent": FEED_READER_UA},
                              retries=1, timeout=20)[0]
        except Exception as exc:
            errors.append("%s: %s" % (name, exc))
            continue
        if not data:
            errors.append("%s: empty" % name)
            continue
        parsed = _parse_content(name, data)
        if parsed:
            parsed["route"] = name
            return parsed, name, None
        errors.append("%s: unparsed payload" % name)
    return None, None, "; ".join(errors) or "no content route answered"


def _parse_content(route, data):
    if route == "syndication":
        if not data.get("id_str"):
            return None
        user = data.get("user") or {}
        return {
            "text": data.get("text") or "",
            "author": user.get("screen_name") or "",
            "name": user.get("name") or "",
            "created_at": data.get("created_at"),
            "likes": data.get("favorite_count") or 0,
            "replies": data.get("conversation_count") or 0,
        }
    if route == "fxtwitter":
        tweet = data.get("tweet") or {}
        if not tweet.get("id"):
            return None
        author = tweet.get("author") or {}
        return {
            "text": tweet.get("text") or "",
            "author": author.get("screen_name") or "",
            "name": author.get("name") or "",
            "created_at": tweet.get("created_at"),
            "likes": tweet.get("likes") or 0,
            "replies": tweet.get("replies") or 0,
        }
    if route == "vxtwitter":
        if not data.get("conversationID"):
            return None
        return {
            "text": data.get("text") or "",
            "author": data.get("user_screen_name") or "",
            "name": data.get("user_name") or "",
            "created_at": data.get("date"),
            "likes": data.get("likes") or 0,
            "replies": data.get("replies") or 0,
        }
    return None


def x_user(source, limit=15):
    """Adapter entry point: a monitored X account as normalized items."""
    handle = (source.get("handle") or "").lstrip("@")
    if not handle:
        return [], "source has no handle"

    posts, route, error = discover(handle, limit=limit,
                                   preferred=source.get("discovery"))
    if error:
        return [], error

    items = []
    fetched = 0
    for post in posts:
        parsed, content_route, content_error = content(post["tweet_id"], handle)
        if not parsed:
            fetched += 1
            continue
        text = parsed["text"].strip()
        if not text:
            continue
        title = text.split("\n", 1)[0].strip()
        if len(title) > 140:
            title = title[:137].rstrip() + "..."
        summary = " ".join(text.split())
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        items.append({
            "uid": "x:%s" % post["tweet_id"],
            "title": title,
            "url": post["url"],
            "summary": summary,
            "published": parsed["created_at"] or post.get("published"),
            "extra": {
                "route": content_route,
                "discovery": route,
                "likes": parsed.get("likes") or 0,
                "replies": parsed.get("replies") or 0,
                "author": parsed.get("author") or handle,
                "points": parsed.get("likes") or 0,
            },
        })
        # keep the per-cycle content fetch bounded on free third-party routes
        fetched += 1
        if fetched >= limit:
            break
    return items, None


# --------------------------------------------------------------------------- #
# discovery route: mine x.com links out of content we have already downloaded
# --------------------------------------------------------------------------- #

_X_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/"
    r"([A-Za-z0-9_]{1,20})/status/(\d{6,25})"
)


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_link_table(store):
    store.db.execute(
        """CREATE TABLE IF NOT EXISTS x_links (
               tweet_id TEXT PRIMARY KEY,
               url      TEXT,
               handle   TEXT,
               via      TEXT,
               first_seen TEXT,
               state    TEXT)""")
    store.db.commit()


def mine_links(limit=6, scan=500):
    """Turn x.com links found in already-fetched pages into real X items.

    Telegram channels quote primary sources, so part of X discovery costs zero
    extra requests: the links are sitting in pages we downloaded anyway. The
    post itself is then hydrated through the content cascade, and every seen
    tweet id is recorded so nothing is fetched twice.
    """
    store = _STORE
    if store is None:
        return [], "miner has no store injected"
    _ensure_link_table(store)

    rows = store.db.execute(
        """SELECT extra, source FROM items
           WHERE (extra LIKE '%x.com/%' OR extra LIKE '%twitter.com/%')
           ORDER BY nid DESC LIMIT ?""", (scan,)).fetchall()

    candidates = {}
    for row in rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except ValueError:
            continue
        if not isinstance(extra, dict):
            continue
        for link in extra.get("links") or []:
            match = _X_LINK_RE.search(link or "")
            if match:
                candidates.setdefault(match.group(2), (match.group(1), row["source"]))

    fresh = []
    for tweet_id, (handle, via) in candidates.items():
        known = store.db.execute(
            "SELECT 1 FROM x_links WHERE tweet_id=?", (tweet_id,)).fetchone()
        if not known:
            fresh.append((tweet_id, handle, via))
    fresh.sort(key=lambda entry: int(entry[0]), reverse=True)  # newest first

    items = []
    for tweet_id, handle, via in fresh[:limit]:
        parsed, route, _error = content(tweet_id, handle)
        store.db.execute(
            "INSERT OR IGNORE INTO x_links (tweet_id, url, handle, via, first_seen, state) "
            "VALUES (?,?,?,?,?,?)",
            (tweet_id, "https://x.com/%s/status/%s" % (handle, tweet_id), handle,
             via, _now_iso(), "ok" if parsed else "failed"))
        store.db.commit()
        if not parsed:
            continue
        text = (parsed.get("text") or "").strip()
        if not text:
            continue
        title = text.split("\n", 1)[0].strip()
        if len(title) > 140:
            title = title[:137].rstrip() + "..."
        summary = " ".join(text.split())
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        items.append({
            "uid": "x:%s" % tweet_id,
            "title": title,
            "url": "https://x.com/%s/status/%s" % (handle, tweet_id),
            "summary": summary,
            "published": parsed.get("created_at"),
            "extra": {
                "route": "miner/%s" % (route or "?"),
                "quoted_by": via,
                "likes": parsed.get("likes") or 0,
                "replies": parsed.get("replies") or 0,
                "author": parsed.get("author") or handle,
                "points": parsed.get("likes") or 0,
            },
        })
        time.sleep(0.4)
    return items, None


# --------------------------------------------------------------------------- #
# Bluesky (AT Protocol) - a legal, key-free replacement for X accounts that
# cross-post. Not a workaround: these are the authors' own posts on another
# public network.
# --------------------------------------------------------------------------- #

def bsky_user(source, limit=20):
    actor = (source.get("handle") or "").lstrip("@")
    if not actor:
        return [], "source has no handle"
    url = ("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
           "?actor=%s&limit=%d&filter=posts_no_replies" % (actor, limit))
    data, error = fetch_json(url, retries=1, timeout=20)
    if error:
        return [], error
    feed = data.get("feed") if isinstance(data, dict) else None
    if feed is None:
        return [], "unexpected payload"

    items = []
    for entry in feed:
        if entry.get("reason"):          # skip reposts, keep original posts
            continue
        post = entry.get("post") or {}
        record = post.get("record") or {}
        text = (record.get("text") or "").strip()
        if not text:
            continue
        uri = post.get("uri") or ""
        rkey = uri.rsplit("/", 1)[-1] if uri else ""
        handle = (post.get("author") or {}).get("handle") or actor
        if not rkey:
            continue
        title = text.split("\n", 1)[0].strip()
        if len(title) > 140:
            title = title[:137].rstrip() + "..."
        summary = " ".join(text.split())
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        items.append({
            "uid": "bsky:%s" % uri,
            "title": title,
            "url": "https://bsky.app/profile/%s/post/%s" % (handle, rkey),
            "summary": summary,
            "published": record.get("createdAt"),
            "extra": {
                "route": "bluesky",
                "likes": post.get("likeCount") or 0,
                "replies": post.get("replyCount") or 0,
                "reposts": post.get("repostCount") or 0,
                "author": handle,
                "points": post.get("likeCount") or 0,
            },
        })
    return items, None
