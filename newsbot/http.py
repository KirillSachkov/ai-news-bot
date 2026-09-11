"""HTTP fetching with retries, conditional requests and transparent gzip.

Standard library only. No third-party dependencies by design: the tool must run
on a bare Python 3.9+ interpreter (macOS system Python), a cron job, or a
GitHub Actions runner without a package install step.
"""

from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import zlib

USER_AGENT = (
    "ai-news-bot/1.0 (personal news digest; stdlib urllib; "
    "https://github.com/)"
)
DEFAULT_TIMEOUT = 25
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchResult:
    __slots__ = ("status", "body", "headers", "error")

    def __init__(self, status, body, headers, error=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.error = error

    @property
    def ok(self):
        return self.status == 200 and bool(self.body)

    @property
    def not_modified(self):
        return self.status == 304

    def header(self, name, default=None):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def text(self):
        try:
            return self.body.decode("utf-8", errors="replace")
        except Exception:
            return ""


def _decompress(raw, encoding):
    encoding = (encoding or "").lower().strip()
    if encoding == "gzip":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if encoding == "deflate":
        try:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
        except zlib.error:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return raw
    return raw


def fetch(url, etag=None, last_modified=None, timeout=DEFAULT_TIMEOUT, retries=2,
          accept=None, extra_headers=None):
    """Fetch a URL.

    Returns a FetchResult. A 304 means "unchanged since the stored validators";
    callers should skip the source in that case.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept or "application/rss+xml, application/atom+xml, "
                            "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en,ru;q=0.8",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    if extra_headers:
        headers.update(extra_headers)

    last_error = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                raw = _decompress(raw, response.headers.get("Content-Encoding"))
                return FetchResult(response.status, raw, dict(response.headers))
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(304, b"", dict(exc.headers or {}))
            last_error = "HTTP %s" % exc.code
            if exc.code in RETRYABLE_STATUS and attempt < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = 2.0
                if retry_after:
                    try:
                        delay = min(float(retry_after), 30.0)
                    except ValueError:
                        delay = 2.0
                time.sleep(delay * (attempt + 1))
                continue
            return FetchResult(exc.code, b"", dict(exc.headers or {}), last_error)
        except Exception as exc:  # URLError, timeout, ssl, connection reset...
            last_error = "%s: %s" % (type(exc).__name__, exc)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
    return FetchResult(0, b"", {}, last_error or "unknown error")


def fetch_json(url, **kwargs):
    """Fetch a URL and parse it as JSON. Returns (data, error)."""
    import json

    result = fetch(url, accept="application/json", **kwargs)
    if not result.ok:
        return None, result.error or ("HTTP %s" % result.status)
    try:
        return json.loads(result.text()), None
    except ValueError as exc:
        return None, "invalid JSON: %s" % exc


def final_url(url, timeout=15):
    """Follow redirects and return the destination URL.

    Needed because aggregator feeds (Google News) hand out redirect blobs that
    are useless to a reader; the real article URL is only revealed by following
    the redirect. Returns the original URL on any failure.
    """
    if not url:
        return url
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.geturl() or url
    except Exception:
        return url
