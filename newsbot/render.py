"""Message rendering: item card, inline feedback keyboard, status pages."""

from __future__ import annotations

import html
from datetime import datetime, timezone

CATEGORY_ICON = {
    "model": "\U0001F9E0",       # brain
    "research": "\U0001F52C",    # microscope
    "product": "\U0001F680",     # rocket
    "business": "\U0001F4B0",    # money bag
    "policy": "\U0001F4DC",      # scroll
    "tool": "\U0001F6E0",        # hammer and wrench
    "other": "\U0001F4CC",       # pushpin
}

VERDICT_LABEL = {"good": "\u2705 Полезно", "bad": "\u274C Не то"}


def esc(value):
    return html.escape(str(value or ""), quote=False)


def human_age(published):
    if not published:
        return ""
    try:
        stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    minutes = (datetime.now(timezone.utc) - stamp).total_seconds() / 60.0
    if minutes < 0:
        return ""
    if minutes < 60:
        return "%dм назад" % minutes
    if minutes < 60 * 24:
        return "%dч назад" % (minutes / 60)
    return "%dд назад" % (minutes / 1440)


def item_card(item, verdict=None, breaking=False, reason=None):
    """Render one news card as Telegram HTML text."""
    icon = CATEGORY_ICON.get((verdict or {}).get("category") or "other", "\U0001F4CC")
    prefix = "\U0001F525 " if breaking else ""
    title = esc((verdict or {}).get("title_ru") or item.get("title") or "Без заголовка")

    body = (verdict or {}).get("summary_ru") or item.get("summary") or ""
    body = esc(body.strip())

    lines = ["%s%s<b>%s</b>" % (prefix, icon, title)]
    if body:
        lines.append("")
        lines.append(body)

    meta = [esc(item.get("source") or "")]
    age = human_age(item.get("published"))
    if age:
        meta.append(age)
    extra = item.get("extra") or {}
    if extra.get("points"):
        meta.append("\u2B06 %s" % extra["points"])
    lines.append("")
    lines.append("<i>%s</i>" % " · ".join(m for m in meta if m))

    # Google News hands out opaque redirect blobs that cannot be resolved with a
    # plain request any more. Printing one is 300 characters of noise, so the
    # link lives on the button instead, where its text is never shown.
    url = item.get("url") or ""
    if url and "news.google.com" not in url:
        lines.append(esc(url))

    # An interruption should say why it was worth interrupting for.
    if breaking and (verdict or {}).get("reason"):
        lines.append("")
        lines.append("<i>почему: %s</i>" % esc(verdict["reason"]))

    text = "\n".join(lines)
    return text[:4000]


def feedback_keyboard(nid, url=None, verdict=None):
    if verdict:
        return None
    row1 = [
        {"text": "\U0001F44D Полезно", "callback_data": "fb|g|%s" % nid},
        {"text": "\U0001F44E Не то", "callback_data": "fb|b|%s" % nid},
    ]
    row2 = []
    if url:
        row2.append({"text": "\U0001F517 Источник", "url": url})
    rows = [row1]
    if row2:
        rows.append(row2)
    return {"inline_keyboard": rows}


def rated_text(original_text, verdict_label):
    return "%s\n\n<b>%s</b>" % (original_text, esc(verdict_label))


def status_text(store, cfg, chat_id=None, fast=None):
    counts = store.counts()
    feedback = counts.get("feedback") or {}
    health = store.source_health()
    broken = [row for row in health if row["fail_count"] and row["fail_count"] >= 3]
    lines = [
        "<b>Состояние</b>",
        "источников: %d, с ошибками: %d" % (len(health), len(broken)),
        "в очереди: %s | отправлено: %s | пропущено: %s" % (
            counts.get("pending", 0), counts.get("sent", 0),
            counts.get("expired", 0) + counts.get("dropped", 0)
            + counts.get("stale", 0) + counts.get("duplicate", 0)),
        "устарело: %s | дублей: %s" % (
            counts.get("stale", 0), counts.get("duplicate", 0)),
        "оценок: 👍 %s / 👎 %s" % (feedback.get("good", 0), feedback.get("bad", 0)),
        "",
        "лента: %s/час, пауза %s мин, порог %s" % (
            cfg.get("max_per_hour"), cfg.get("min_gap_minutes"), cfg.get("min_score")),
        "важное: до %s/час, пауза %s мин, от %s баллов или %s источников" % (
            cfg.get("breaking_max_per_hour"), cfg.get("breaking_min_gap_minutes"),
            cfg.get("breaking_score"), cfg.get("breaking_min_groups")),
        "свежесть: не старше %s ч" % cfg.get("max_age_hours"),
    ]
    if fast is not None:
        lines.append("опрос: %s источников каждые %s с, остальные каждые %s с" % (
            fast, cfg.get("fast_interval_seconds"), cfg.get("fetch_interval_seconds")))
    if chat_id:
        lines.append("chat_id: <code>%s</code>" % chat_id)
    if broken:
        lines.append("")
        lines.append("<b>Молчат:</b>")
        for row in broken[:10]:
            lines.append("· %s — %s" % (esc(row["name"]), esc(row["last_error"] or "")[:60]))
    return "\n".join(lines)


def sources_text(store, sources):
    by_group = {}
    for source in sources:
        by_group.setdefault(source.get("group", "other"), []).append(source)
    lines = ["<b>Источники (%d)</b>" % len(sources)]
    for group in sorted(by_group):
        lines.append("")
        lines.append("<b>%s</b>" % esc(group))
        for source in by_group[group]:
            state = store.source_state(source["name"]) or {}
            mark = "\u2705" if state.get("last_ok") else "\u23F3"
            if state.get("fail_count", 0) >= 3:
                mark = "\u26A0\uFE0F"
            line = "%s %s" % (mark, esc(source["name"]))
            # A warning without a reason is useless, so show the short cause.
            if state.get("fail_count", 0) and state.get("last_error"):
                line += " — <i>%s</i>" % esc(short_reason(state["last_error"]))
            lines.append(line)
    return "\n".join(lines)


def short_reason(error):
    """Compress a long multi-route error into one readable phrase."""
    text = (error or "").lower()
    if "not whitelisted" in text:
        return "нужна вайтлиста xcancel (письмо rss@xcancel.com)"
    if "cooling down" in text and "403" not in text and "429" not in text:
        return "маршруты остывают после блокировки"
    if "403" in text:
        return "зеркало блокирует по IP (403)"
    if "429" in text:
        return "лимит запросов (429)"
    if "410" in text:
        return "фид отключён (410)"
    if "html page" in text:
        return "зеркало отдало HTML вместо фида"
    if "ssl" in text:
        return "TLS-сбой на зеркале"
    if "timed out" in text or "timeout" in text:
        return "таймаут"
    return error[:70]
