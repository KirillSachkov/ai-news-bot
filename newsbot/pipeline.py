"""Pipeline: fetch -> normalize -> dedupe -> score -> judge -> deliver.

This is the part that decides what actually reaches the operator. It encodes the
"no spam, but never miss a big one" rule:

  * a hard hourly budget (max_per_hour) plus a minimum gap between messages;
  * a breaking-news override with its own, smaller budget, used only when an
    item both scores high and contains an actual event word ("released",
    "announces", "open-source", ...);
  * a two-stage ranking: the free heuristic ranks everything, only the shortlist
    is sent to the LLM, and the LLM's verdict decides the final call.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from . import feeds, render, score as scoring, x_sources
from .store import canonical_url, title_key, utc_now


# --------------------------------------------------------------------------- #
# verdict storage (LLM results live next to the items)
# --------------------------------------------------------------------------- #

def ensure_verdict_table(store):
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS verdicts (nid INTEGER PRIMARY KEY, verdict TEXT, ts TEXT)")
    store.db.commit()


def get_verdict(store, nid):
    row = store.db.execute("SELECT verdict FROM verdicts WHERE nid=?", (nid,)).fetchone()
    if not row or not row["verdict"]:
        return None
    try:
        return json.loads(row["verdict"])
    except ValueError:
        return None


def set_verdict(store, nid, verdict):
    store.db.execute(
        "INSERT INTO verdicts (nid, verdict, ts) VALUES (?,?,?) "
        "ON CONFLICT(nid) DO UPDATE SET verdict=excluded.verdict, ts=excluded.ts",
        (nid, json.dumps(verdict, ensure_ascii=False), utc_now()))
    store.db.commit()


# --------------------------------------------------------------------------- #
# stage 1: fetch
# --------------------------------------------------------------------------- #

def source_index(sources):
    return {s["name"]: s for s in sources}


# --------------------------------------------------------------------------- #
# cross-source duplicate detection
# --------------------------------------------------------------------------- #

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "with", "is",
    "are", "new", "how", "why", "what", "its", "it", "this", "that", "at", "as",
    "by", "from", "be", "will", "has", "have", "not", "but", "you", "your",
    "и", "в", "во", "на", "с", "со", "для", "от", "по", "из", "что", "как",
    "это", "не", "а", "но", "уже", "все", "весь", "или",
}


def title_signature(title):
    words = re.findall(r"[a-z0-9а-яё]+", (title or "").lower())
    return " ".join(w for w in words if w not in _STOPWORDS and len(w) > 2)


def similar_to_any(title, titles, threshold=0.82):
    """True when `title` reads as the same story as one already selected."""
    import difflib

    signature = title_signature(title)
    if not signature:
        return False
    for other in titles:
        candidate = title_signature(other)
        if not candidate:
            continue
        if signature == candidate:
            return True
        if difflib.SequenceMatcher(None, signature, candidate).ratio() >= threshold:
            return True
    return False


def is_fresh_enough(item, cfg):
    hours = float(cfg.get("lookback_hours", 48))
    published = item.get("published")
    if not published:
        return True
    try:
        stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
    return age <= hours


def fetch_source(source):
    """Fetch one source. Returns (items, error, validators)."""
    kind = source.get("type")
    if kind == "x_user":
        items, error = x_sources.x_user(source, limit=int(source.get("limit", 15)))
        return items, error, {}
    if kind == "x_miner":
        items, error = x_sources.mine_links(limit=int(source.get("limit", 6)))
        return items, error, {}
    if kind == "bsky_user":
        items, error = x_sources.bsky_user(source, limit=int(source.get("limit", 20)))
        return items, error, {}
    items, error, validators = feeds.collect_with_validators(source)
    return items, error, validators


def collect(store, sources, cfg, verbose=False):
    """Fetch every enabled source and store new items. Returns stats."""
    stats = {"sources": 0, "ok": 0, "failed": 0, "fetched": 0, "new": 0,
             "errors": {}, "skipped_unchanged": 0}
    x_sources.set_store(store)
    for source in sources:
        if not source.get("enabled", True):
            continue
        name = source["name"]
        stats["sources"] += 1
        state = store.source_state(name) or {}
        if source.get("type") == "rss" and state.get("etag"):
            # cheap conditional request: reuse stored validators
            pass
        items, error, validators = fetch_source(source)
        if error:
            stats["failed"] += 1
            stats["errors"][name] = error
            store.touch_source_error(name, error, source.get("group"))
            if verbose:
                print("  [!] %-22s %s" % (name, error))
            continue
        stats["ok"] += 1
        store.touch_source_ok(
            name, source.get("group"),
            etag=validators.get("etag"),
            last_modified=validators.get("last_modified"))
        stats["fetched"] += len(items)
        fresh = 0
        for item in items:
            if not is_fresh_enough(item, cfg):
                continue
            item["source"] = name
            item["source_group"] = source.get("group")
            if store.exists(item["uid"]) or store.exists_url(item.get("url")):
                store.backfill_links(item["uid"], (item.get("extra") or {}).get("links"))
                continue
            if store.add_item(item):
                stats["new"] += 1
                fresh += 1
        if verbose:
            print("  [ok] %-21s %3d items (%d new)" % (name, len(items), fresh))
        delay = float(cfg.get("politeness_delay_seconds", 0.4))
        if source.get("type") in ("x_user", "x_miner", "bsky_user"):
            # The free X mirrors block by IP, and they are shared resources:
            # space the calls out instead of firing them in a burst.
            delay = float(cfg.get("x_politeness_delay_seconds", 5.0))
        time.sleep(delay)
    return stats


# --------------------------------------------------------------------------- #
# stage 2: score + judge
# --------------------------------------------------------------------------- #

def rerank_pending(store, sources, cfg, llm=None, verbose=False):
    """Score every pending item; send the shortlist to the LLM when enabled."""
    ensure_verdict_table(store)
    index = source_index(sources)
    weights = store.weights() if cfg.get("learning", True) else {}
    pending = store.pending(limit=int(cfg.get("pending_scan_limit", 300)))
    max_llm = int(cfg.get("llm_max_per_cycle", 15))
    llm_floor = float(cfg.get("llm_min_heuristic", 4.0))
    llm_used = 0
    judged = 0

    scored = []
    for row in pending:
        source_cfg = index.get(row["source"], {"weight": 1.0})
        value, reasons, keywords = scoring.score_item(row, source_cfg, weights)
        store.set_score(row["nid"], value)
        row["score"] = value
        row["_reasons"] = reasons
        row["_keywords"] = keywords
        scored.append(row)
    scored.sort(key=lambda r: r["score"], reverse=True)

    if llm is not None and llm.enabled:
        for row in scored:
            if llm_used >= max_llm:
                break
            if row["score"] < llm_floor:
                continue
            verdict = get_verdict(store, row["nid"])
            if verdict is None:
                try:
                    verdict = llm.judge(row, group=row.get("source_group"))
                except Exception as exc:
                    if verbose:
                        print("  [!] llm failed for nid=%s: %s" % (row["nid"], exc))
                    break
                if verdict is not None:
                    set_verdict(store, row["nid"], verdict)
                    llm_used += 1
            if verdict:
                judged += 1
            time.sleep(float(cfg.get("llm_delay_seconds", 0.3)))
    elif verbose:
        print("  (LLM disabled: no DEEPSEEK_API_KEY -> heuristic only)")

    return {"scanned": len(scored), "judged": judged, "llm_calls": llm_used}


def effective_score(row, store, cfg):
    verdict = get_verdict(store, row["nid"])
    if verdict and verdict.get("score") is not None:
        return float(verdict["score"])
    return float(row.get("score") or 0)


# --------------------------------------------------------------------------- #
# stage 3: budgeted selection
# --------------------------------------------------------------------------- #

def _sends_last_hour(store):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    return store.sends_since(cutoff)


def _seconds_since_last(store):
    last = store.last_send_at()
    if not last:
        return 10 ** 6
    try:
        stamp = datetime.fromisoformat(last)
    except ValueError:
        return 10 ** 6
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def select_due(store, sources, cfg, force=False, dry=False):
    """Pick the items to deliver right now under the anti-spam budget.

    With dry=True nothing is marked in the database, so the selection can be
    inspected without disturbing the queue.
    """
    index = source_index(sources)
    weights = store.weights() if cfg.get("learning", True) else {}
    min_score = float(cfg.get("min_score", 3.0))
    max_per_hour = int(cfg.get("max_per_hour", 2))
    max_breaking = int(cfg.get("breaking_max_per_hour", 2))
    min_gap = float(cfg.get("min_gap_minutes", 20)) * 60.0

    hour_sends = _sends_last_hour(store)
    normal_used = len([s for s in hour_sends if s.get("kind") == "news"])
    breaking_used = len([s for s in hour_sends if s.get("kind") == "breaking"])
    gap = _seconds_since_last(store)

    normal_budget = max(0, max_per_hour - normal_used)
    breaking_budget = max(0, max_breaking - breaking_used)

    pending = store.pending(limit=int(cfg.get("pending_scan_limit", 300)))
    candidates = []
    for row in pending:
        value = effective_score(row, store, cfg)
        verdict = get_verdict(store, row["nid"])
        if verdict and (verdict.get("verdict") == "skip"):
            if not dry:
                store.mark(row["nid"], "rejected")
            continue
        if value < min_score:
            continue
        source_cfg = index.get(row["source"], {"weight": 1.0})
        breaking = scoring.is_breaking(value, row, cfg, weights)
        candidates.append((value, breaking, row))
    candidates.sort(key=lambda entry: (entry[0], entry[2].get("discovered") or ""),
                    reverse=True)

    chosen = []
    chosen_titles = []
    for value, breaking, row in candidates:
        # The same story arrives from several outlets; sending it twice is the
        # most visible form of spam in a news feed.
        if similar_to_any(row.get("title") or "", chosen_titles):
            if not dry:
                store.mark(row["nid"], "duplicate")
            continue
        if breaking and breaking_budget > 0:
            chosen.append((value, breaking, row))
            chosen_titles.append(row.get("title") or "")
            breaking_budget -= 1
            continue
        if not breaking and normal_budget > 0:
            if gap < min_gap and not force and chosen:
                continue
            chosen.append((value, breaking, row))
            chosen_titles.append(row.get("title") or "")
            normal_budget -= 1
    return chosen


# --------------------------------------------------------------------------- #
# stage 4: deliver
# --------------------------------------------------------------------------- #

def deliver(tg, store, chat_id, chosen, cfg, verbose=False):
    from .http import final_url

    sent = 0
    for value, breaking, row in chosen:
        verdict = get_verdict(store, row["nid"])
        url = row.get("url") or ""
        # Aggregator feeds hand out redirect blobs; resolve once at send time so
        # the reader gets the publisher's real article link.
        if "news.google.com" in url:
            resolved = final_url(url)
            if resolved and "news.google.com" not in resolved:
                row = dict(row)
                row["url"] = resolved
        text = render.item_card(row, verdict=verdict, breaking=breaking)
        keyboard = render.feedback_keyboard(row["nid"], row.get("url"))
        try:
            tg.send_message(chat_id, text, keyboard=keyboard)
        except Exception as exc:
            if verbose:
                print("  [!] send failed nid=%s: %s" % (row["nid"], exc))
            continue
        store.mark(row["nid"], "sent", sent=True)
        if breaking:
            store.db.execute("UPDATE sends SET kind='breaking' WHERE id=(SELECT MAX(id) FROM sends)")
            store.db.commit()
        sent += 1
        if verbose:
            print("  [->] %s  (%.2f)" % ((row.get("title") or "")[:70], value))
        time.sleep(float(cfg.get("send_delay_seconds", 0.7)))
    return sent


def bootstrap(store, tg, chat_id, sources, stats, cfg, verbose=False):
    """First ever run: send a short starter selection, drop the rest.

    Without this the operator presses Start and then waits for the next fetch
    before seeing anything, which reads as "the bot is broken". A handful of the
    best current items is the honest demo; everything else is dropped so the
    backlog never arrives as a flood.
    """
    if store.kv_get("bootstrap_done"):
        return False
    limit = int(cfg.get("bootstrap_items", 3))
    try:
        chosen = select_due(store, sources, cfg, force=True)[:limit]
    except Exception:
        chosen = []
    store.mark_all_pending_dropped("bootstrap")
    store.kv_set("bootstrap_done", utc_now())
    text = (
        "<b>Бот подключён</b>\n\n"
        "источников: %d (активных: %d)\n"
        "за первый проход собрано записей: %d\n"
        "старое не отправляю — жду только новое.\n\n"
        "Дальше буду присылать 1–%s новостей в час. Оценивай кнопками 👍 / 👎 — "
        "я подстраиваю отбор под тебя." % (
            stats.get("sources", 0), stats.get("ok", 0),
            stats.get("fetched", 0), cfg.get("max_per_hour", 2))
    )
    try:
        tg.send_message(chat_id, text)
    except Exception:
        pass
    if chosen:
        try:
            tg.send_message(chat_id, "<i>Что уже есть по твоим источникам сейчас:</i>")
        except Exception:
            pass
        deliver(tg, store, chat_id, chosen, cfg, verbose=verbose)
    if verbose:
        print("  [i] bootstrap: starter items sent=%d, backlog dropped" % len(chosen))
    return True
