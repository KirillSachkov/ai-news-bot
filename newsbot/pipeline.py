"""Pipeline: fetch -> normalize -> dedupe -> score -> judge -> deliver.

This is the part that decides what actually reaches the operator. It encodes the
"few, fresh, on format" rule:

  * an editorial judge: the free heuristic ranks everything and shortlists, the
    LLM decides whether an item fits the reference channels (editorial.md) and
    writes the draft; with the LLM enabled nothing unjudged is ever delivered;
  * hard budgets per hour and per day plus a minimum gap between messages, and
    a smaller breaking lane for the story of the day;
  * repeat control on three levels: story clusters, a cheap match against what
    was sent in the last few days, and the editor's own repeat check.
    News sagas resurface for days (recaps, reactions), so the window is four
    days by default, not one.

Everything that talks to the model runs in the collection thread (judging and
the repeat check); the delivery side only reads stored answers, because it
shares its thread with Telegram commands and buttons.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from . import feeds, render, score as scoring, x_sources
from .store import _COMMON_ENTITIES, canonical_url, entity_tokens, title_key, utc_now  # noqa: F401

REFERENCE_ROLE = "reference"


# --------------------------------------------------------------------------- #
# verdict storage (LLM results live next to the items)
# --------------------------------------------------------------------------- #

def ensure_verdict_table(store):
    # Once per connection: the delivery loop calls this every few seconds, and
    # a needless write there competes with the collection thread.
    if getattr(store, "_editor_tables", False):
        return
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS verdicts (nid INTEGER PRIMARY KEY, verdict TEXT, ts TEXT)")
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS dedup_checks (nid INTEGER PRIMARY KEY, "
        "last_send_id INTEGER, duplicate_of INTEGER, ts TEXT)")
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS judge_failures (nid INTEGER PRIMARY KEY, "
        "failures INTEGER, last_error TEXT, ts TEXT)")
    store.db.commit()
    store._editor_tables = True


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


def needs_judgement(verdict, llm):
    """True when the editor has not seen the item under the current profile."""
    if not verdict:
        return True
    tag = getattr(llm, "profile_tag", None)
    return bool(tag) and verdict.get("profile_tag") != tag


def is_item_fault(error):
    """Did the judge fail because of this item rather than the API?

    A 4xx other than 429 or an unparseable answer repeats for the same input;
    timeouts, TLS drops and 5xx say nothing about the item.
    """
    text = str(error)
    if text.startswith("model returned"):
        return True
    return text.startswith("HTTP 4") and not text.startswith("HTTP 429")


def judge_failures(store):
    return {row["nid"]: row["failures"] for row in
            store.db.execute("SELECT nid, failures FROM judge_failures")}


def note_judge_failure(store, nid, error):
    store.db.execute(
        "INSERT INTO judge_failures (nid, failures, last_error, ts) VALUES (?,1,?,?) "
        "ON CONFLICT(nid) DO UPDATE SET failures=failures+1, last_error=excluded.last_error, "
        "ts=excluded.ts", (nid, str(error)[:300], utc_now()))
    store.db.commit()


# --------------------------------------------------------------------------- #
# stage 1: fetch
# --------------------------------------------------------------------------- #

def source_index(sources):
    return {s["name"]: s for s in sources}


def reference_channels(sources):
    """Source name -> channel handle for the reference channels.

    Their posts are not news for the operator - he reads those channels - but
    they are the best available signal of what the target audience cares about.
    They join story clusters and lift the stories they confirm; they are never
    judged or delivered on their own.
    """
    return {s["name"]: (s.get("channel") or s["name"]) for s in sources or []
            if s.get("role") == REFERENCE_ROLE}


def story_view(store, story_id, references=None):
    """Carriers of a story, with the reference channels counted apart.

    A reference post is a confirmation, not an independent outlet: counting it
    as one more group as well would pay the same signal twice.
    """
    references = references or {}
    head = store.story_stats(story_id)
    carriers = store.story_carriers(story_id)
    primary = [(source, group) for source, group in carriers if source not in references]
    return {
        "sources": len({source for source, _ in primary}),
        "groups": len({group for _, group in primary if group}),
        "reference": sorted({references[source] for source, _ in carriers
                             if source in references}),
        "sent_nid": head["sent_nid"],
        "first_seen": head["first_seen"],
    }


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

# Words an event key uses for "what happened" rather than "to what". Two keys
# that differ only in these describe the same event.
_EVENT_VERBS = {
    "release", "releases", "released", "launch", "launches", "launched",
    "announce", "announces", "announced", "ship", "ships", "shipped", "unveil",
    "unveils", "introduce", "introduces", "debut", "debuts", "publish",
    "publishes", "open-sources", "rolls", "out", "update", "updates", "model",
    "says", "reports", "confirms", "officially", "drops",
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


def event_tokens(text):
    """Words of the editor's event key ("deepseek releases v4.1 flash").

    Bare numbers stay: "claude opus 5" and "claude opus 6" are different events.
    """
    return {word for word in re.findall(r"[a-z0-9][a-z0-9.\-]*", str(text or "").lower())
            if (len(word) > 1 or word.isdigit()) and word not in _STOPWORDS}


def recent_entry(row, verdict):
    verdict = verdict or {}
    return {
        "nid": row.get("nid"),
        "story_id": row.get("story_id"),
        "title": row.get("title") or "",
        "title_ru": str(verdict.get("title_ru") or ""),
        "event": event_tokens(verdict.get("event")),
        "entities": entity_tokens(row.get("title"), str(verdict.get("title_ru") or "")),
    }


def recent_sent(store, cfg):
    """What the operator got in the repeat window, newest first."""
    hours = float(cfg.get("dedup_window_hours", 96))
    return [recent_entry(row, get_verdict(store, row["nid"]))
            for row in store.sent_since(hours)]


def _names_nothing_new(mine, theirs, minimum=3):
    """One side names nothing the other does not, and they share enough.

    Plain overlap is not enough: "gemini 3 pro" and "gemini 3 flash" share three
    words out of four and are still two releases. Each has a word the other
    lacks - that is what makes them different events.
    """
    if len(mine & theirs) < minimum:
        return False
    return mine <= theirs or theirs <= mine


def matches_recent(row, verdict, recent):
    """The cheap repeat check against what the operator already got.

    Strict on purpose, because it runs without a model and its answer is final:
    it fires only when one event key (verbs aside) or one set of headline names
    adds nothing to the other. Anything fuzzier is left to the editor's check.
    """
    mine = recent_entry(row, verdict)
    mine_event = mine["event"] - _EVENT_VERBS
    for other in recent:
        if other["nid"] == mine["nid"]:
            continue
        if mine["story_id"] and other["story_id"] == mine["story_id"]:
            return other
        if _names_nothing_new(mine_event, other["event"] - _EVENT_VERBS):
            return other
        rare = (mine["entities"] & other["entities"]) - _COMMON_ENTITIES
        if len(rare) >= 2 and _names_nothing_new(mine["entities"], other["entities"]):
            return other
    return None


def repeat_state(store, nid, last_send_id):
    """The editor's stored repeat answer for this item, if it is still current.

    Returns ("clear", None), ("repeat", nid of the sent item) or None when the
    question has not been asked since the last delivery.
    """
    row = store.db.execute(
        "SELECT last_send_id, duplicate_of FROM dedup_checks WHERE nid=?", (nid,)).fetchone()
    if not row or row["last_send_id"] != last_send_id:
        return None
    if row["duplicate_of"]:
        return ("repeat", row["duplicate_of"])
    return ("clear", None)


def save_repeat_state(store, nid, last_send_id, duplicate_of):
    store.db.execute(
        "INSERT INTO dedup_checks (nid, last_send_id, duplicate_of, ts) VALUES (?,?,?,?) "
        "ON CONFLICT(nid) DO UPDATE SET last_send_id=excluded.last_send_id, "
        "duplicate_of=excluded.duplicate_of, ts=excluded.ts",
        (nid, last_send_id, duplicate_of, utc_now()))
    store.db.commit()


def check_repeats(store, sources, cfg, llm, limit=None):
    """Ask the editor about repeats for the items that could go out next.

    Runs in the collection thread: it is a network call that can take minutes
    when the API is slow, and the delivery loop shares its thread with Telegram.
    Answers are kept until the next delivery, so each question is asked once
    per state of the feed. Returns the number of model calls made.
    """
    if llm is None or not getattr(llm, "enabled", False):
        return 0
    ensure_verdict_table(store)
    limit = int(cfg.get("llm_repeat_checks_per_cycle", 6)) if limit is None else limit
    references = reference_channels(sources)
    recent = recent_sent(store, cfg)
    last_id = store.last_send_id()
    min_score = float(cfg.get("min_score", 7.0))

    candidates = []
    for row in store.pending(limit=int(cfg.get("pending_scan_limit", 300))):
        if row["source"] in references or too_old_to_send(row, cfg):
            continue
        verdict = get_verdict(store, row["nid"])
        if needs_judgement(verdict, llm) or repeat_state(store, row["nid"], last_id):
            continue
        stats = story_view(store, row.get("story_id"), references)
        if stats["sent_nid"]:
            continue
        value = effective_score(row, store, cfg, stats=stats, verdict=verdict)
        if value >= min_score and not matches_recent(row, verdict, recent):
            candidates.append((value, row, verdict))
    candidates.sort(key=lambda entry: entry[0], reverse=True)

    calls = 0
    for value, row, verdict in candidates:
        other = None
        if recent:
            if calls >= limit:
                break
            calls += 1
            try:
                index = llm.same_story(row, verdict, recent)
            except Exception:
                # A failed check must not hold the feed hostage; the story
                # clusters and the cheap check have already run.
                index = None
            if index is not None and 0 <= index < len(recent):
                other = recent[index]
        save_repeat_state(store, row["nid"], last_id, other["nid"] if other else None)
    return calls


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
                        window_minutes=int(cfg.get("corroboration_window_minutes", 360)))
                except Exception as exc:
                    if verbose:
                        print("  [!] story grouping failed nid=%s: %s" % (nid, exc))
                if source.get("role") == REFERENCE_ROLE:
                    store.mark(nid, "signal")
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
    references = reference_channels(sources)
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
    max_failures = int(cfg.get("llm_item_max_failures", 2))
    llm_used = 0
    judged = 0
    llm_errors = 0

    scored = []
    for row in pending:
        if row["source"] in references:
            # Collected before the source became a reference channel.
            store.mark(row["nid"], "signal")
            continue
        source_cfg = index.get(row["source"], {"weight": 1.0})
        value, reasons, keywords = scoring.score_item(row, source_cfg, weights, cfg=cfg)
        store.set_score(row["nid"], value)
        row["score"] = value
        row["_reasons"] = reasons
        row["_keywords"] = keywords
        scored.append(row)
    scored.sort(key=lambda r: r["score"], reverse=True)

    if llm is not None and llm.enabled:
        failures = judge_failures(store)
        api_errors_in_a_row = 0
        for row in scored:
            if llm_used >= max_llm:
                break
            if row["score"] < llm_floor:
                continue
            verdict = get_verdict(store, row["nid"])
            if needs_judgement(verdict, llm):
                if failures.get(row["nid"], 0) >= max_failures:
                    # This item keeps breaking the judge; it must not keep the
                    # items below it from being judged on every cycle.
                    continue
                try:
                    verdict = llm.judge(row, group=row.get("source_group"))
                except Exception as exc:
                    llm_errors += 1
                    if verbose:
                        print("  [!] llm failed for nid=%s: %s" % (row["nid"], exc))
                    if is_item_fault(exc):
                        note_judge_failure(store, row["nid"], exc)
                        continue
                    api_errors_in_a_row += 1
                    if api_errors_in_a_row >= 3:
                        if verbose:
                            print("  [!] llm failing repeatedly, skipping the rest")
                        break
                    continue
                api_errors_in_a_row = 0
                if verdict is not None:
                    set_verdict(store, row["nid"], verdict)
                    llm_used += 1
            if verdict:
                judged += 1
            time.sleep(float(cfg.get("llm_delay_seconds", 0.3)))
        repeat_calls = check_repeats(store, sources, cfg, llm)
    else:
        repeat_calls = 0
        if verbose:
            print("  (LLM disabled: no DEEPSEEK_API_KEY -> heuristic only)")

    return {"scanned": len(scored), "judged": judged, "llm_calls": llm_used,
            "llm_errors": llm_errors, "repeat_checks": repeat_calls}


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


def effective_score(row, store, cfg, stats=None, verdict=None, references=None):
    """Final ranking value: editorial fit plus confirmation.

    Two confirmations, paid separately. Independent groups of sources carrying
    the same event is the honest measure of "everyone is talking about it"; a
    reference channel carrying it is the measure of "it is on format".
    """
    value = base_score(row, store, cfg, verdict=verdict)
    if stats is None:
        stats = story_view(store, row.get("story_id"), references)
    if stats.get("groups", 0) >= int(cfg.get("corroboration_min_groups", 2)):
        value += float(cfg.get("corroboration_bonus", 0.5))
    if stats.get("reference"):
        value += float(cfg.get("reference_bonus", 1.0))
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
    """Does this deserve to interrupt the budget right now?

    With the editor on, only its verdict opens this lane: the story of the day
    on its own score, or a slightly lower one that independent carriers or a
    reference channel already confirm. The score is taken before the bonuses,
    or a well-covered story would come in through both doors at once.

    Without a verdict (heuristic-only mode) the corroboration route alone
    remains. Stale and undated items are never breaking, whatever they score.
    """
    if age is None or age > float(cfg.get("max_age_hours", 6)):
        return False
    many_groups = stats.get("groups", 0) >= int(cfg.get("breaking_min_groups", 3))
    if not verdict or verdict.get("score") is None:
        return many_groups and value >= float(cfg.get("min_score", 3.0))
    if base_value >= float(cfg.get("breaking_score", 9.0)):
        return True
    confirmed = many_groups or bool(stats.get("reference"))
    return confirmed and base_value >= float(cfg.get("breaking_corroborated_score", 8.0))


def select_due(store, sources, cfg, force=False, dry=False, llm=None):
    """Pick the items to deliver right now under the anti-spam budget.

    Makes no network calls: it runs in the Telegram thread. With dry=True
    nothing is marked in the database and the stored repeat answers are not
    required, so the selection can be inspected without disturbing the queue.
    """
    ensure_verdict_table(store)
    weights = store.weights() if cfg.get("learning", True) else {}
    references = reference_channels(sources)
    editor_on = llm is not None and getattr(llm, "enabled", False)
    require_verdict = editor_on and cfg.get("require_verdict", True)
    min_score = float(cfg.get("min_score", 7.0))
    reject_below = float(cfg.get("reject_below", 5.0))
    max_per_hour = int(cfg.get("max_per_hour", 1))
    max_per_day = int(cfg.get("max_per_day", 10))
    max_breaking = int(cfg.get("breaking_max_per_hour", 2))
    max_breaking_day = int(cfg.get("breaking_max_per_day", 3))
    min_gap = float(cfg.get("min_gap_minutes", 40)) * 60.0
    breaking_gap = float(cfg.get("breaking_min_gap_minutes", 10)) * 60.0

    hour_sends = _sends_last_hour(store)
    normal_used = len([s for s in hour_sends if s.get("kind") == "news"])
    breaking_used = len([s for s in hour_sends if s.get("kind") == "breaking"])

    normal_budget = max(0, max_per_hour - normal_used)
    if _sends_last_day(store, "news") >= max_per_day:
        normal_budget = 0
    breaking_budget = max(0, max_breaking - breaking_used)
    if _sends_last_day(store, "breaking") >= max_breaking_day:
        breaking_budget = 0

    # Both gaps are carried through the loop and reset on every pick, so a batch
    # cannot fire several messages back to back the way it used to.
    gap = _seconds_since_last(store)
    gap_breaking = _seconds_since_kind(store, "breaking")
    recent = recent_sent(store, cfg)
    last_id = store.last_send_id()

    pending = store.pending(limit=int(cfg.get("pending_scan_limit", 300)))
    candidates = []
    for row in pending:
        if row["source"] in references:
            # Collected before the source became a reference channel.
            if not dry:
                store.mark(row["nid"], "signal")
            continue
        verdict = get_verdict(store, row["nid"])
        unseen = needs_judgement(verdict, llm) if editor_on else not verdict
        if verdict and not unseen and verdict.get("verdict") == "skip":
            # Clear misses leave the queue; near misses stay, because a reference
            # channel or several outlets picking the story up can still lift them.
            if float(verdict.get("score") or 0) < reject_below or not editor_on:
                if not dry:
                    store.mark(row["nid"], "rejected")
                continue
        if require_verdict and unseen:
            # The editor has not seen it yet, or saw it under an older profile:
            # it waits for the judge instead of slipping through on heuristics.
            continue
        # Freshness is enforced here, not only at collection: an item can go
        # stale while it waits behind the hourly budget.
        if too_old_to_send(row, cfg):
            if not dry:
                store.mark(row["nid"], "stale")
            continue
        stats = story_view(store, row.get("story_id"), references)
        # Someone already told this story; a second carrier is a repeat.
        if stats["sent_nid"] and stats["sent_nid"] != row["nid"]:
            if not dry:
                store.mark(row["nid"], "duplicate")
            continue
        base = base_score(row, store, cfg, verdict=verdict)
        value = effective_score(row, store, cfg, stats=stats, verdict=verdict)
        if value < min_score:
            continue
        if matches_recent(row, verdict, recent):
            if not dry:
                store.mark(row["nid"], "duplicate")
            continue
        if editor_on and recent and not dry:
            state = repeat_state(store, row["nid"], last_id)
            if state is None:
                # The collection thread has not asked the editor since the last
                # delivery; the item waits for the next cycle.
                continue
            if state[0] == "repeat":
                store.mark(row["nid"], "duplicate")
                continue
        age = item_age_hours(row)
        breaking = is_breaking_now(value, base, row, cfg, weights, stats, age, verdict)
        candidates.append((value, breaking, row, verdict))
    candidates.sort(key=lambda entry: (entry[0], entry[2].get("discovered") or ""),
                    reverse=True)

    chosen = []
    chosen_titles = []
    chosen_entries = []
    chosen_stories = set()
    for value, breaking, row, verdict in candidates:
        story_id = row.get("story_id")
        if story_id and story_id in chosen_stories:
            continue
        # Belt and braces: clustering handles the cross-language case, this
        # catches near-identical headlines that never shared a proper name. Not
        # marked: the item it repeats is only chosen, not delivered yet.
        if similar_to_any(row.get("title") or "", chosen_titles) or \
                matches_recent(row, verdict, chosen_entries):
            continue
        if breaking and breaking_budget > 0:
            if gap_breaking < breaking_gap and not force:
                continue
            lane = "breaking"
        elif normal_budget > 0:
            # A breaking story that finds its lane spent still competes for an
            # ordinary slot instead of being dropped on the floor.
            if gap < min_gap and not force:
                continue
            lane = "news"
        else:
            continue
        chosen.append((value, lane == "breaking", row))
        chosen_titles.append(row.get("title") or "")
        chosen_entries.append(recent_entry(row, verdict))
        if story_id:
            chosen_stories.add(story_id)
        if lane == "breaking":
            breaking_budget -= 1
            gap_breaking = 0.0
        else:
            normal_budget -= 1
        gap = 0.0
    return chosen


# --------------------------------------------------------------------------- #
# stage 4: deliver
# --------------------------------------------------------------------------- #

def deliver(tg, store, chat_id, chosen, cfg, verbose=False, sources=None):
    from .http import final_url, google_news_url

    references = reference_channels(sources)
    sent = 0
    for value, breaking, row in chosen:
        verdict = get_verdict(store, row["nid"])
        url = row.get("url") or ""
        # Aggregator feeds hand out redirect blobs; resolve once at send time so
        # the reader gets the publisher's real article link — in the card text,
        # where it can be copied, and on the button.
        if "news.google.com" in url:
            resolved = google_news_url(url) or final_url(url)
            if resolved and "news.google.com" not in resolved:
                store.set_url(row["nid"], resolved)
                row = dict(row)
                row["url"] = resolved
            elif verbose:
                print("  [!] google link not resolved nid=%s" % row["nid"])
        story = story_view(store, row.get("story_id"), references)
        text = render.item_card(row, verdict=verdict, breaking=breaking, story=story)
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


def bootstrap(store, tg, chat_id, sources, stats, cfg, verbose=False, llm=None):
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
        chosen = select_due(store, sources, cfg, force=True, llm=llm)[:limit]
    except Exception:
        chosen = []
    store.mark_all_pending_dropped("bootstrap")
    store.kv_set("bootstrap_done", utc_now())
    channels = ", ".join("@%s" % c for c in sorted(set(reference_channels(sources).values())))
    text = (
        "<b>Бот подключён</b>\n\n"
        "источников: %d (активных: %d)\n"
        "за первый проход собрано записей: %d\n"
        "старое не отправляю — жду только новое.\n\n"
        "Дальше пришлю до %s новостей в день и до %s срочных — только то, что по формату "
        "подошло бы %s. Оценивай кнопками 👍 / 👎 — я подстраиваю отбор под тебя." % (
            stats.get("sources", 0), stats.get("ok", 0),
            stats.get("fetched", 0), cfg.get("max_per_day", 10),
            cfg.get("breaking_max_per_day", 3), channels or "каналам-ориентирам")
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
        deliver(tg, store, chat_id, chosen, cfg, verbose=verbose, sources=sources)
    if verbose:
        print("  [i] bootstrap: starter items sent=%d, backlog dropped" % len(chosen))
    return True
