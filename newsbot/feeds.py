"""Source adapters: feeds, official free APIs, and Telegram web previews.

Every adapter returns a list of normalized item dicts:

    {"uid", "title", "url", "summary", "published", "extra"}

`uid` must be stable across runs (it is the deduplication key), `published` is an
ISO-8601 UTC string or None, and `extra` carries adapter-specific ranking hints
(points, likes, comments) that the scorer may use.

Notes learned the hard way while testing live sources:
  * some feeds (xcancel) return valid XML preceded by whitespace, which makes a
    strict parser fail with "XML declaration not at start of entity" - so the
    payload is left-stripped (and BOM-stripped) before parsing;
  * mirrors and CDNs sometimes answer with an HTML error page and HTTP 200, so a
    non-feed body is reported as such instead of as a mysterious parse error;
  * Telegram's public preview page is HTML and is parsed by regex, because the
    widget markup is stable while its surrounding layout is not.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .http import fetch, fetch_json


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")


def strip_html(value):
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def to_iso(value):
    """Best-effort parse of RFC-822 or ISO-8601 timestamps to ISO UTC."""
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        pass
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# RSS / Atom (plain feeds, GitHub release feeds, Google News, YouTube channels,
# vendor blogs, media categories)
# --------------------------------------------------------------------------- #

def parse_feed(payload):
    """Parse RSS 2.0 or Atom into normalized items.

    Raises ValueError with a readable message when the body is not a feed.
    """
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    raw = raw.lstrip()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:].lstrip()
    head = raw[:400].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise ValueError("body is an HTML page, not a feed "
                         "(mirror or CDN returned an error page)")
    if not raw.startswith(b"<"):
        raise ValueError("body does not start with XML/HTML markup: %r"
                         % raw[:60])
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("invalid feed XML: %s" % exc)

    items = []
    for element in root.iter():
        if local_name(element.tag) not in ("item", "entry"):
            continue

        children = {}
        links = []
        for child in list(element):
            name = local_name(child.tag).lower()
            if name == "link":
                href = child.get("href")
                rel = child.get("rel") or "alternate"
                if href:
                    if rel == "alternate":
                        links.insert(0, href)
                    else:
                        links.append(href)
                elif (child.text or "").strip():
                    links.append(child.text.strip())
                continue
            if name == "category":
                continue
            if name not in children and (child.text or "").strip():
                children[name] = child.text.strip()

        title = strip_html(children.get("title", ""))
        if not title:
            continue

        url = ""
        for candidate in links:
            if candidate.startswith("http"):
                url = candidate
                break
        if not url:
            guid = children.get("guid") or children.get("id") or ""
            if guid.startswith("http"):
                url = guid

        summary = strip_html(
            children.get("description")
            or children.get("summary")
            or children.get("content")
            or children.get("encoded")
            or ""
        )
        if len(summary) > 600:
            summary = summary[:597].rstrip() + "..."

        published = to_iso(
            children.get("pubdate")
            or children.get("published")
            or children.get("updated")
            or children.get("date")
            or ""
        )

        uid = children.get("guid") or children.get("id") or url or title
        items.append({
            "uid": uid.strip(),
            "title": title,
            "url": url,
            "summary": summary,
            "published": published,
            "extra": {},
        })
    return items


def rss(url, limit=None):
    result = fetch(url)
    if result.not_modified:
        return [], None, {"not_modified": True}
    if not result.ok:
        return [], result.error or ("HTTP %s" % result.status), {}
    try:
        items = parse_feed(result.body)
    except ValueError as exc:
        return [], str(exc), {}
    if limit:
        try:
            items = items[:int(limit)]
        except (TypeError, ValueError):
            pass
    validators = {
        "etag": result.header("ETag"),
        "last_modified": result.header("Last-Modified"),
    }
    return items, None, validators


# --------------------------------------------------------------------------- #
# Hacker News (Algolia API, free, no key)
# --------------------------------------------------------------------------- #

def _hn(tags, limit, by_date=False):
    endpoint = "search_by_date" if by_date else "search"
    url = ("https://hn.algolia.com/api/v1/%s?tags=%s&hitsPerPage=%d"
           % (endpoint, tags, limit))
    data, error = fetch_json(url)
    if error:
        return [], error
    items = []
    for hit in data.get("hits") or []:
        title = hit.get("title") or hit.get("story_title") or ""
        if not title:
            continue
        object_id = hit.get("objectID") or ""
        link = hit.get("url") or "https://news.ycombinator.com/item?id=%s" % object_id
        items.append({
            "uid": "hn:%s" % object_id,
            "title": strip_html(title),
            "url": link,
            "summary": "",
            "published": to_iso(hit.get("created_at")),
            "extra": {
                "points": hit.get("points") or 0,
                "comments": hit.get("num_comments") or 0,
            },
        })
    return items, None


def hn_front_page(limit=25):
    return _hn("front_page", limit)


def hn_show(limit=20):
    # by_date, because relevance sorting returns months-old Show HN posts
    return _hn("show_hn", limit, by_date=True)


# --------------------------------------------------------------------------- #
# Hugging Face (public API, no key)
# --------------------------------------------------------------------------- #

def hf_daily_papers(limit=20):
    data, error = fetch_json("https://huggingface.co/api/daily_papers?limit=%d" % limit)
    if error:
        return [], error
    if not isinstance(data, list):
        return [], "unexpected payload"
    items = []
    for entry in data:
        paper = entry.get("paper") or {}
        paper_id = paper.get("id") or entry.get("id") or ""
        title = paper.get("title") or ""
        if not paper_id or not title:
            continue
        summary = strip_html(paper.get("summary") or "")
        if len(summary) > 400:
            summary = summary[:397].rstrip() + "..."
        items.append({
            "uid": "hf-paper:%s" % paper_id,
            "title": title.strip(),
            "url": "https://huggingface.co/papers/%s" % paper_id,
            "summary": summary,
            "published": to_iso(paper.get("publishedAt") or entry.get("publishedAt")),
            "extra": {"points": paper.get("upvotes") or 0},
        })
    return items, None


def hf_trending_models(limit=15):
    url = ("https://huggingface.co/api/models?sort=trendingScore&direction=-1"
           "&limit=%d" % limit)
    data, error = fetch_json(url)
    if error:
        return [], error
    if not isinstance(data, list):
        return [], "unexpected payload"
    items = []
    for model in data:
        model_id = model.get("id") or model.get("modelId") or ""
        if not model_id:
            continue
        items.append({
            "uid": "hf-model:%s" % model_id,
            "title": "New/trending model: %s" % model_id,
            "url": "https://huggingface.co/%s" % model_id,
            "summary": ("pipeline: %s; likes: %s; downloads: %s"
                        % (model.get("pipeline_tag") or "n/a",
                           model.get("likes") or 0, model.get("downloads") or 0)),
            "published": to_iso(model.get("createdAt")),
            "extra": {"points": model.get("likes") or 0,
                      "downloads": model.get("downloads") or 0},
        })
    return items, None


# --------------------------------------------------------------------------- #
# Telegram public web previews: https://t.me/s/<channel>
# --------------------------------------------------------------------------- #

_MESSAGE_RE = re.compile(
    r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"(.*?)'
    r'(?=<div class="tgme_widget_message |</section>)',
    re.S,
)
_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*'
    r'(?:<div class="tgme_widget_message_(?:footer|reply_markup)|\Z)',
    re.S,
)
_HREF_RE = re.compile(r'href="([^"]+)"')
_ERID_RE = re.compile(r"\berid\b|Реклама", re.I)


def telegram_web(channel, limit=20):
    result = fetch("https://t.me/s/%s" % channel.lstrip("@"))
    if not result.ok:
        return [], result.error or ("HTTP %s" % result.status)
    page = result.text()
    if "tgme_widget_message" not in page:
        return [], "no message markup on the preview page (channel private or renamed?)"
    items = []
    for match in _MESSAGE_RE.finditer(page):
        post_path = match.group(1)
        block = match.group(2)
        try:
            post_id = int(post_path.split("/")[-1])
        except (ValueError, IndexError):
            continue

        text_match = _TEXT_RE.search(block)
        raw_text = text_match.group(1) if text_match else ""
        text = strip_html(raw_text)
        if not text:
            continue

        time_match = _TIME_RE.search(block)
        published = to_iso(time_match.group(1)) if time_match else None

        title = text.split("\n", 1)[0].strip()
        if len(title) > 140:
            title = title[:137].rstrip() + "..."

        links = [href for href in _HREF_RE.findall(raw_text)
                 if href.startswith("http") and "t.me/" not in href]
        summary = " ".join(text.split())
        if len(summary) > 400:
            summary = summary[:397].rstrip() + "..."
        items.append({
            "uid": "tg:%s/%d" % (channel, post_id),
            "title": title,
            "url": "https://t.me/%s/%d" % (channel, post_id),
            "summary": summary,
            "published": published,
            "extra": {"is_ad": bool(_ERID_RE.search(text)),
                      "outbound": len(set(links)),
                      # Persisted on purpose: these channels quote primary
                      # sources, and their x.com links are the cheapest X
                      # discovery channel available (see x_sources.mine_links).
                      "links": list(dict.fromkeys(links))[:12]},
        })
        if len(items) >= limit:
            break
    return items, None


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

ADAPTERS = {
    "rss": lambda source: rss(source["url"], source.get("limit")),
    "hn_front_page": lambda source: hn_front_page(source.get("limit", 25)),
    "hn_show": lambda source: hn_show(source.get("limit", 20)),
    "hf_daily_papers": lambda source: hf_daily_papers(source.get("limit", 20)),
    "hf_trending_models": lambda source: hf_trending_models(source.get("limit", 15)),
    "telegram_web": lambda source: telegram_web(source["channel"], source.get("limit", 20)),
}


def collect(source):
    """Run one source. Returns (items, error)."""
    kind = source.get("type")
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        return [], "unknown source type: %s" % kind
    return adapter(source)


def collect_with_validators(source):
    """Like collect(), but also returns HTTP cache validators when available."""
    if source.get("type") == "rss":
        return rss(source["url"], source.get("limit"))
    items, error = collect(source)
    return items, error, {}
