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


def item_age_hours(item):
    """Age of an item in hours, or None when it carries no usable date."""
    published = item.get("published")
    if not published:
        return None
    try:
        stamp = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0


def is_fresh_enough(item, cfg):
    """Collection gate. Undated items are admitted but never treated as fresh."""
    hours = float(cfg.get("lookback_hours", 8))
    age = item_age_hours(item)
    if age is None:
        return True
    return age <= hours


def too_old_to_send(item, cfg):
    """Delivery gate.

    Separate from the collection gate on purpose: an item can sit in the queue
    behind the hourly budget long enough to go stale after it was collected, and
    nothing downstream used to notice.
    """
    age = item_age_hours(item)
    if age is None:
        return False
    return age > float(cfg.get("max_age_hours", 6))


def fetch_source(source, etag=None, last_modified=None):
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
    items, error, validators = feeds.collect_with_validators(
        source, etag=etag, last_modified=last_modified)
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
        items, error, validators = fetch_source(
            source, etag=state.get("etag"), last_modified=state.get("last_modified"))
        if validators.get("not_modified"):
            # The server says nothing changed; stop here instead of re-parsing.
            stats["ok"] += 1
            stats["skipped_unchanged"] += 1
            store.touch_source_ok(name, source.get("group"))
            if verbose:
                print("  [=] %-21s unchanged (304)" % name)
            time.sleep(float(cfg.get("politeness_delay_seconds", 0.4)))
            continue
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
            nid = store.add_item(item)
            if nid:
                stats["new"] += 1
                fresh += 1
                # Group it with the other carriers of the same event straight
                # away: the count of independent carriers is both the duplicate
                # guard and the "this is big" signal.
                try:
                    store.assign_story(
                        nid, item.get("title"), item.get("summary"),
                        name, source.get("group"),
                        window_minutes=int(cfg.get("corroboration_window_minutes", 90)))
                except Exception as exc:
                    if verbose:
                        print("  [!] story grouping failed nid=%s: %s" % (nid, exc))
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

def rerank_pending(store, sources, cfg, llm=None, verbose=False, fast=False):
    """Score every pending item; send the shortlist to the LLM when enabled.

    `fast=True` is the 90-second lane. It judges far less: that cadence runs
    ~40x an hour, and at the full per-cycle allowance a backlog of unjudged
    items would be re-offered to the model on every pass until it cleared -
    turning a cheap poll into a steady spend.
    """
    ensure_verdict_table(store)
    index = source_index(sources)
    weights = store.weights() if cfg.get("learning", True) else {}
    # Ordered by discovery, not by score: an unscored row carries score 0 and
    # would otherwise sit at the bottom of a score-ordered page forever.
    pending = store.pending(limit=int(cfg.get("pending_scan_limit", 300)),
                            order="fresh")
    if fast:
        max_llm = int(cfg.get("llm_fast_max_per_cycle", 4))
        llm_floor = float(cfg.get("llm_fast_floor", 6.0))
    else:
        max_llm = int(cfg.get("llm_max_per_cycle", 15))
        llm_floor = float(cfg.get("llm_min_heuristic", 4.0))
    llm_used = 0
    judged = 0
    llm_errors = 0

    scored = []
    for row in pending:
        source_cfg = index.get(row["source"], {"weight": 1.0})
        value, reasons, keywords = scoring.score_item(row, source_cfg, weights, cfg=cfg)
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
                    # One bad item used to abort the whole judging pass and take
                    # every other candidate down with it.
                    llm_errors += 1
                    if verbose:
                        print("  [!] llm failed for nid=%s: %s" % (row["nid"], exc))
                    if llm_errors >= 3:
                        if verbose:
                            print("  [!] llm failing repeatedly, skipping the rest")
                        break
                    continue
                if verdict is not None:
                    set_verdict(store, row["nid"], verdict)
                    llm_used += 1
            if verdict:
                judged += 1
            time.sleep(float(cfg.get("llm_delay_seconds", 0.3)))
    elif verbose:
        print("  (LLM disabled: no DEEPSEEK_API_KEY -> heuristic only)")

    return {"scanned": len(scored), "judged": judged, "llm_calls": llm_used,
            "llm_errors": llm_errors}


def base_score(row, store, cfg, verdict=None):
    """Ranking value before corroboration: the model's verdict, else heuristic."""
    if verdict is None:
        verdict = get_verdict(store, row["nid"])
    if verdict and verdict.get("score") is not None:
        value = float(verdict["score"])
    else:
        value = float(row.get("score") or 0)
    # An item with no date cannot be shown to be fresh, so it must not be able
    # to outrank things that can.
    if not row.get("published"):
        value = min(value, float(cfg.get("undated_score_cap", 5.0)))
    return value


def effective_score(row, store, cfg, stats=None, verdict=None):
    """Final ranking value: base score plus corroboration.

    Corroboration is the honest measure of "popular": how many independent
    groups of sources carry the same event. It is free - the data is collected
    anyway - and it is what separates a real story from one outlet's angle.
    """
    value = base_score(row, store, cfg, verdict=verdict)
    if stats is None:
        stats = store.story_stats(row.get("story_id"))
    if stats["groups"] >= int(cfg.get("corroboration_min_groups", 2)):
        value += float(cfg.get("corroboration_bonus", 1.5))
    return value


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


def _sends_last_day(store, kind=None):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = store.sends_since(cutoff)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return len(rows)


def _seconds_since_kind(store, kind):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")
    stamps = [r["ts"] for r in store.sends_since(cutoff) if r.get("kind") == kind]
    if not stamps:
        return 10 ** 6
    try:
        stamp = datetime.fromisoformat(max(stamps))
    except ValueError:
        return 10 ** 6
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def is_breaking_now(value, base_value, row, cfg, weights, stats, age, verdict):
    """Does this deserve to interrupt the hourly budget right now?

    Two independent routes in, and they must stay independent:

      * corroboration - several unrelated groups carry the same event, which is
        what a person means by "everyone is talking about it";
      * weight - the model rates it very highly on its own.

    The weight route is judged on the score *before* the corroboration bonus,
    or a well-covered story would come in through both doors at once and the
    threshold would effectively drop by the size of the bonus. It also requires
    an actual model verdict: heuristic scores live on a different scale, and
    without this a wordy headline no model ever looked at could interrupt.

    Stale and undated items are never breaking, whatever they score.
    """
    if age is None or age > float(cfg.get("max_age_hours", 6)):
        return False
    min_groups = int(cfg.get("breaking_min_groups", 3))
    if stats["groups"] >= min_groups and value >= float(cfg.get("min_score", 3.0)):
        return True
    if verdict is None:
        return False
    return scoring.is_breaking(base_value, row, cfg, weights)


def select_due(store, sources, cfg, force=False, dry=False):
    """Pick the items to deliver right now under the anti-spam budget.

    With dry=True nothing is marked in the database, so the selection can be
    inspected without disturbing the queue.
    """
    weights = store.weights() if cfg.get("learning", True) else {}
    min_score = float(cfg.get("min_score", 3.0))
    max_per_hour = int(cfg.get("max_per_hour", 2))
    max_breaking = int(cfg.get("breaking_max_per_hour", 3))
    max_breaking_day = int(cfg.get("breaking_max_per_day", 12))
    min_gap = float(cfg.get("min_gap_minutes", 20)) * 60.0
    breaking_gap = float(cfg.get("breaking_min_gap_minutes", 3)) * 60.0

    hour_sends = _sends_last_hour(store)
    normal_used = len([s for s in hour_sends if s.get("kind") == "news"])
    breaking_used = len([s for s in hour_sends if s.get("kind") == "breaking"])

    normal_budget = max(0, max_per_hour - normal_used)
    breaking_budget = max(0, max_breaking - breaking_used)
    if _sends_last_day(store, "breaking") >= max_breaking_day:
        breaking_budget = 0

    # Both gaps are carried through the loop and reset on every pick, so a batch
    # cannot fire several messages back to back the way it used to.
    gap = _seconds_since_last(store)
    gap_breaking = _seconds_since_kind(store, "breaking")

    pending = store.pending(limit=int(cfg.get("pending_scan_limit", 300)))
    candidates = []
    for row in pending:
        verdict = get_verdict(store, row["nid"])
        if verdict and (verdict.get("verdict") == "skip"):
            if not dry:
                store.mark(row["nid"], "rejected")
            continue
        # Freshness is enforced here, not only at collection: an item can go
        # stale while it waits behind the hourly budget.
        if too_old_to_send(row, cfg):
            if not dry:
                store.mark(row["nid"], "stale")
            continue
        stats = store.story_stats(row.get("story_id"))
        # Someone already told this story; a second carrier is a repeat.
        if stats["sent_nid"] and stats["sent_nid"] != row["nid"]:
            if not dry:
                store.mark(row["nid"], "duplicate")
            continue
        base = base_score(row, store, cfg, verdict=verdict)
        value = effective_score(row, store, cfg, stats=stats, verdict=verdict)
        if value < min_score:
            continue
        age = item_age_hours(row)
        breaking = is_breaking_now(value, base, row, cfg, weights, stats, age, verdict)
        candidates.append((value, breaking, row, stats))
    candidates.sort(key=lambda entry: (entry[0], entry[2].get("discovered") or ""),
                    reverse=True)

    chosen = []
    chosen_titles = []
    chosen_stories = set()
    for value, breaking, row, stats in candidates:
        story_id = row.get("story_id")
        if story_id and story_id in chosen_stories:
            continue
        # Belt and braces: clustering handles the cross-language case, this
        # catches near-identical headlines that never shared a proper name.
        if similar_to_any(row.get("title") or "", chosen_titles):
            if not dry:
                store.mark(row["nid"], "duplicate")
            continue
        if breaking and breaking_budget > 0:
            if gap_breaking < breaking_gap and not force:
                continue
            chosen.append((value, breaking, row))
            chosen_titles.append(row.get("title") or "")
            if story_id:
                chosen_stories.add(story_id)
            breaking_budget -= 1
            gap_breaking = 0.0
            gap = 0.0
            continue
        if not breaking and normal_budget > 0:
            if gap < min_gap and not force:
                continue
            chosen.append((value, breaking, row))
            chosen_titles.append(row.get("title") or "")
            if story_id:
                chosen_stories.add(story_id)
            normal_budget -= 1
            gap = 0.0
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
        store.mark(row["nid"], "sent", sent=True,
                   kind="breaking" if breaking else "news")
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
