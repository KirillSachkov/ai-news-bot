"""Read public X profile data. No JavaScript execution, cookies, or login.

The profile preview is a small, unordered tail and can include pinned posts.
It is not a complete timeline. Structural changes fail visibly at the source.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone

from . import http
from .feeds import to_iso

_KEY = re.compile(r"[A-Za-z_$][\w$]*")
_REF = re.compile(r"\$R\[(\d+)\]")
_ASSIGN = re.compile(r"\$R\[\d+\]=")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_ENTRY = re.compile(r'content:(\$R\[\d+\]=\{__isTimelineTimelineItemContent:"TimelineTweet")')


class Reference:
    def __init__(self, number):
        self.number = number


class DataReader:
    """Restricted serialized-data grammar. Functions and expressions are errors."""
    def __init__(self, text):
        self.text, self.offset, self.refs, self.nodes = text, 0, {}, 0
        self.resolved_nodes = 0
        self.decoder = json.JSONDecoder()

    def whitespace(self):
        while self.offset < len(self.text) and self.text[self.offset].isspace():
            self.offset += 1

    def value(self, depth=0):
        self.nodes += 1
        if depth > 60 or self.nodes > 200000:
            raise ValueError("X data exceeds parsing bounds")
        self.whitespace()
        text, offset = self.text, self.offset
        if offset >= len(text):
            raise ValueError("incomplete X data")
        char = text[offset]
        if char == '"':
            value, self.offset = self.decoder.raw_decode(text, offset)
            return value
        if char in "{[":
            self.offset += 1
            output = {} if char == "{" else []
            closing = "}" if char == "{" else "]"
            self.whitespace()
            while self.offset < len(text) and text[self.offset] != closing:
                if char == "{":
                    self.whitespace()
                    if text[self.offset] == '"':
                        key = self.value(depth+1)
                    else:
                        match = _KEY.match(text, self.offset)
                        if not match:
                            raise ValueError("unsupported X object key")
                        key, self.offset = match.group(), match.end()
                    self.whitespace()
                    if text[self.offset:self.offset+1] != ":":
                        raise ValueError("missing X object colon")
                    self.offset += 1
                    output[key] = self.value(depth+1)
                else:
                    output.append(self.value(depth+1))
                self.whitespace()
                if text[self.offset:self.offset+1] == ",":
                    self.offset += 1
                    self.whitespace()
                elif text[self.offset:self.offset+1] != closing:
                    raise ValueError("incomplete X object")
            if self.offset >= len(text):
                raise ValueError("incomplete X object")
            self.offset += 1
            return output
        match = _REF.match(text, offset)
        if match:
            number, self.offset = int(match.group(1)), match.end()
            self.whitespace()
            if text[self.offset:self.offset+1] == "=":
                self.offset += 1
                self.refs[number] = self.value(depth+1)
            return Reference(number)
        for token, value in (("!0", True), ("!1", False), ("true", True),
                             ("false", False), ("null", None), ("void 0", None),
                             ("undefined", None)):
            if text.startswith(token, offset):
                self.offset += len(token)
                return value
        match = _NUMBER.match(text, offset)
        if match:
            self.offset = match.end()
            return json.loads(match.group())
        raise ValueError("unsupported X data value")

    def resolve(self, value, seen=frozenset(), depth=0):
        self.resolved_nodes += 1
        if depth > 60 or self.resolved_nodes > 200000:
            raise ValueError("X references exceed expansion bound")
        if isinstance(value, Reference):
            if value.number in seen or value.number not in self.refs:
                return None
            return self.resolve(self.refs[value.number], seen | {value.number}, depth+1)
        if isinstance(value, dict):
            return {k: self.resolve(v, seen, depth+1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, seen, depth+1) for v in value]
        return value


def normalize_entry(entry, handle):
    results = entry.get("tweet_results") or {}
    tweet = results.get("result") or {}
    if tweet.get("__typename") == "TweetWithVisibilityResults":
        tweet = tweet.get("tweet") or {}
    legacy = tweet.get("legacy") or {}
    if legacy.get("retweeted_status_result") or tweet.get("retweeted_status_result"):
        return None
    user = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    author = (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get("screen_name")
    if not author or author.lower() != handle.lower():
        return None
    tweet_id = str(tweet.get("rest_id") or results.get("rest_id") or "")
    if not re.fullmatch(r"\d{6,25}", tweet_id):
        return None
    details = tweet.get("details") or legacy
    note = ((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    text = html.unescape(note.get("text") or details.get("full_text") or legacy.get("full_text") or "").strip()
    published = to_iso(details.get("created_at") or legacy.get("created_at"))
    if details.get("created_at_ms") is not None:
        try:
            published = datetime.fromtimestamp(float(details["created_at_ms"])/1000, timezone.utc).isoformat(timespec="seconds")
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    if not text or not published:
        return None
    return {"uid": "x:%s" % tweet_id, "title": text.split("\n", 1)[0][:140],
            "summary": " ".join(text.split())[:1200],
            "url": "https://x.com/%s/status/%s" % (author, tweet_id),
            "published": published,
            "extra": {"author": author, "route": "x_html", "preview_partial": True,
                      "has_quote": bool(tweet.get("quoted_tweet_results")),
                      "summary_truncated": len(" ".join(text.split())) > 1200,
                      "text_chars": len(text), "points": legacy.get("favorite_count") or 0}}


def parse_profile(text, handle, limit=10):
    if len(text) > 3*1024*1024:
        raise ValueError("X profile exceeds size bound")
    matches = list(_ENTRY.finditer(text))[:100]
    if not matches:
        raise ValueError("no serialized X timeline entries (login wall or changed markup)")
    reader = DataReader(text)
    for count, match in enumerate(_ASSIGN.finditer(text)):
        if count >= 6000:
            raise ValueError("X profile exceeds reference bound")
        reader.offset = match.start()
        try:
            reader.value()
        except (ValueError, IndexError):
            if reader.nodes > 200000:
                raise ValueError("X profile exceeds parsing bounds")
    items = {}
    for match in matches:
        reader.offset = match.start()+len("content:")
        try:
            entry = reader.resolve(reader.value())
            item = normalize_entry(entry, handle)
        except (ValueError, IndexError, TypeError, AttributeError):
            continue
        if item:
            items[item["uid"]] = item
    if not items:
        raise ValueError("X timeline has no dated posts by the requested author")
    return sorted(items.values(), key=lambda i: i["published"], reverse=True)[:limit]


def collect(source):
    handle = source["handle"]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return [], "invalid X handle"
    result = http.fetch("https://x.com/%s" % handle, timeout=15, retries=0)
    if not result.ok:
        return [], result.error or "HTTP %s" % result.status
    try:
        return parse_profile(result.text(), handle, int(source.get("limit", 10))), None
    except ValueError as exc:
        return [], str(exc)
