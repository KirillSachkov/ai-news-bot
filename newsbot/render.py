"""Message rendering: item card, inline feedback keyboard, status pages."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

# Editorial rubrics (see editorial.md): icon and the label shown on the card.
RUBRIC = {
    "release": ("\U0001F9E0", "Релиз"),              # brain
    "tool": ("\U0001F6E0", "Полезное"),              # hammer and wrench
    "breakthrough": ("\U0001F52C", "Прорыв"),        # microscope
    "viral": ("\U0001F92F", "Вирусное"),             # exploding head
    "industry": ("\U0001F4B0", "Индустрия"),         # money bag
    "safety": ("\U0001F6A8", "ИИ и безопасность"),   # rotating light
    "outage": ("⚡", "Сбои и блокировки"),       # high voltage
    "hardware": ("\U0001F916", "Железо и роботы"),   # robot
    "career": ("\U0001F4BC", "Карьера"),             # briefcase
    "other": ("\U0001F4CC", "Другое"),               # pushpin
}

# Verdicts written before the editorial profile carried a coarser category.
LEGACY_CATEGORY = {"model": "release", "research": "breakthrough", "product": "release",
                   "business": "industry", "policy": "other", "tool": "tool"}

CHANNEL_LABEL = {"codecamp": "в духе @codecamp", "data_secrets": "в духе @data_secrets",
                 "both": "для обоих каналов"}

VERDICT_LABEL = {"good": "✅ Полезно", "bad": "❌ Не то"}


def esc(value):
    return html.escape(str(value or ""), quote=False)


def esc_clip(value, limit):
    """Clip escaped text without breaking an HTML entity or a surrounding tag."""
    output, size = [], 0
    for char in str(value or ""):
        escaped = esc(char)
        if size+len(escaped) > limit:
            break
        output.append(escaped)
        size += len(escaped)
    return "".join(output)


def message_parts(text, limit=3900):
    """Status/source lines have self-contained tags; split only between lines."""
    parts, lines, size = [], [], 0
    for line in text.splitlines():
        if lines and size+len(line)+1 > limit:
            parts.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line)+1
    if lines:
        parts.append("\n".join(lines))
    return parts


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


def rubric_of(verdict):
    verdict = verdict or {}
    rubric = verdict.get("rubric") or LEGACY_CATEGORY.get(verdict.get("category")) or "other"
    return rubric if rubric in RUBRIC else "other"


def _score_text(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return ("%d" % number) if number == int(number) else ("%.1f" % number)


def item_card(item, verdict=None, breaking=False, reason=None, story=None):
    """Render one news card as Telegram HTML text.

    The card answers the operator's question before he opens the link: is it on
    format (fit, rubric, which channel it resembles, why), is it already being
    talked about (other outlets, reference channels), and what the post could
    look like (the editor's draft).
    """
    verdict = verdict or {}
    story = story or {}
    icon, label = RUBRIC[rubric_of(verdict)]
    prefix = "\U0001F525 " if breaking else ""
    title = esc_clip(verdict.get("title_ru") or item.get("title") or "Без заголовка", 300)

    body = verdict.get("summary_ru") or item.get("summary") or ""
    body = esc_clip(body.strip(), 1800)

    lines = ["%s%s <b>%s</b>" % (prefix, icon, title)]
    if body:
        lines.append("")
        lines.append(body)

    lines.append("")
    note = []
    score = _score_text(verdict.get("score"))
    if score:
        note.append("\U0001F3AF %s/10" % score)
    note.append(label)
    channel = CHANNEL_LABEL.get(verdict.get("channel"))
    if channel:
        note.append(channel)
    lines.append(" · ".join(note))
    why = verdict.get("reason") or reason
    if why:
        lines.append("<i>почему: %s</i>" % esc_clip(why, 450))

    signals = []
    if story.get("reference"):
        signals.append("\U0001F4E3 уже написали: %s"
                       % ", ".join("@%s" % esc(name) for name in story["reference"]))
    if story.get("sources", 0) > 1:
        signals.append("\U0001F501 источников: %d" % story["sources"])
    if signals:
        lines.append(" · ".join(signals))

    meta = [esc_clip(item.get("source") or "", 120)]
    age = human_age(item.get("published"))
    if age:
        meta.append(age)
    extra = item.get("extra") or {}
    if extra.get("summary_truncated"):
        meta.append("описание сокращено")
    if extra.get("freshness_basis") == "discovery":
        meta.append("найдено: %s" % human_age(extra.get("freshness_at")))
    if extra.get("growth_delta") and extra.get("growth_hours"):
        meta.append("рост: +%s за %s ч" % (extra["growth_delta"], extra["growth_hours"]))
    if extra.get("points"):
        meta.append("⬆ %s" % extra["points"])
    lines.append("")
    lines.append("<i>%s</i>" % " · ".join(m for m in meta if m))

    # Google News hands out opaque redirect blobs that cannot be resolved with a
    # plain request any more. Printing one is 300 characters of noise, so the
    # link lives on the button instead, where its text is never shown.
    url = item.get("url") or ""
    if url and "news.google.com" not in url:
        if len(esc(url)) <= 700:
            lines.append(esc(url))

    text = "\n".join(lines)
    return text


def feedback_keyboard(nid, url=None, verdict=None):
    link_row = []
    # Telegram rejects the whole message when a url button carries anything but
    # a real http(s) link, so a broken url costs the card, not just the button.
    if url and str(url).startswith(("http://", "https://")):
        link_row.append({"text": "\U0001F517 Источник", "url": url})
    # After a rating the vote buttons have done their job, but the source link
    # is exactly what the card is kept for — it stays on the message.
    if verdict:
        return {"inline_keyboard": [link_row]} if link_row else None
    row1 = [
        {"text": "\U0001F44D Полезно", "callback_data": "fb|g|%s" % nid},
        {"text": "\U0001F44E Не то", "callback_data": "fb|b|%s" % nid},
    ]
    rows = [row1]
    if link_row:
        rows.append(link_row)
    return {"inline_keyboard": rows}


def rated_text(original_text, verdict_label):
    # Telegram hands the old message back as plain text: the card's tags are
    # already gone, so a bare "<" or "&" in a headline would break HTML parsing
    # and the edit — and with it the source button — would fail silently.
    return "%s\n\n<b>%s</b>" % (esc(original_text), esc(verdict_label))


def status_text(store, cfg, chat_id=None, fast=None, sources=None):
    counts = store.counts()
    feedback = counts.get("feedback") or {}
    health = store.source_health()
    if sources is not None:
        names = {s["name"] for s in sources if s.get("enabled", True)}
        health = [s for s in health if s["name"] in names]
    broken = [row for row in health if row["fail_count"] and row["fail_count"] >= 3]
    lines = [
        "<b>Состояние</b>",
        "источников: %d, с ошибками: %d" % (len(health), len(broken)),
        "в очереди: %s | отправлено: %s | пропущено: %s" % (
            counts.get("pending", 0), counts.get("sent", 0),
            counts.get("expired", 0) + counts.get("dropped", 0)
            + counts.get("stale", 0) + counts.get("duplicate", 0)
            + counts.get("rejected", 0)),
        "не формат: %s | устарело: %s | повторов: %s" % (
            counts.get("rejected", 0), counts.get("stale", 0), counts.get("duplicate", 0)),
        "оценок: 👍 %s / 👎 %s" % (feedback.get("good", 0), feedback.get("bad", 0)),
        "",
        "лента: до %s/день и %s/час, пауза %s мин, порог fit %s" % (
            cfg.get("max_per_day"), cfg.get("max_per_hour"), cfg.get("min_gap_minutes"),
            cfg.get("min_score")),
        "срочное: до %s/день, от %s баллов (или от %s с подтверждением)" % (
            cfg.get("breaking_max_per_day"), cfg.get("breaking_score"),
            cfg.get("breaking_corroborated_score")),
        "свежесть: не старше %s ч, повторы ловлю за %s ч" % (
            cfg.get("max_age_hours"), cfg.get("dedup_window_hours")),
    ]
    if fast is not None:
        lines.append("опрос: %s источников каждые %s с, остальные каждые %s с" % (
            fast, cfg.get("fast_interval_seconds"), cfg.get("fetch_interval_seconds")))
    if chat_id:
        lines.append("chat_id: <code>%s</code>" % chat_id)
    last = store.last_collection()
    if last:
        stats = json.loads(last["stats"])
        lines.append("последний сбор: %s · %s · %s с" % (
            esc(last["label"]), esc(last["finished"]), stats.get("seconds", "?")))
        lines.append("сбор: новых %s, старых %s, уже известных %s, ошибок %s, cooldown %s" % (
            stats.get("new", 0), stats.get("old", 0), stats.get("seen", 0),
            stats.get("failed", 0), stats.get("cooldown", 0)))
        if stats.get("overdue_fast"):
            lines.append("полный сбор превысил интервал быстрой полосы")
    selection = json.loads(store.kv_get("selection_status", "{}"))
    if selection:
        lines.append("отбор: ждут редактора %s, ниже порога %s, ждут проверки повторов %s" % (
            selection.get("waiting_editor", 0), selection.get("below_threshold", 0),
            selection.get("waiting_repeat", 0)))
        lines.append("слотов сейчас с учётом часа и суток: обычных %s, срочных %s" % (
            selection.get("normal_budget", 0), selection.get("breaking_budget", 0)))
    if store.kv_get("paused") == "1":
        lines.append("доставка на паузе")
    if store.kv_get("muted_until"):
        lines.append("тишина до: %s" % esc(store.kv_get("muted_until")))
    if store.kv_get("delivery_error"):
        lines.append("ошибка доставки: %s" % esc(store.kv_get("delivery_error")))
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
            mark = "✅" if state.get("last_ok") else "⏳"
            if state.get("fail_count", 0) >= 3:
                mark = "⚠️"
            line = "%s %s" % (mark, esc(source["name"]))
            if source.get("role") == "reference":
                line += " — <i>ориентир, только сигнал</i>"
            # A warning without a reason is useless, so show the short cause.
            if state.get("fail_count", 0) and state.get("last_error"):
                line += " — <i>%s</i>" % esc(short_reason(state["last_error"]))
            if state.get("newest"):
                line += " · свежайшее: %s" % esc(state["newest"][:16])
            if state.get("last_check"):
                line += " · %s с" % state.get("seconds", 0)
            lines.append(line)
    return "\n".join(lines)


def short_reason(error):
    """Compress a long multi-route error into one readable phrase."""
    text = (error or "").lower()
    if "github rate limit" in text:
        return "лимит GitHub для IP сервера — нужен GITHUB_TOKEN в .env"
    if "not whitelisted" in text:
        return "нужна вайтлиста xcancel (письмо rss@xcancel.com)"
    if "cooling down" in text and "403" not in text and "429" not in text:
        return "маршруты остывают после блокировки"
    if "403" in text:
        return "зеркало блокирует по IP (403)"
    if "429" in text:
        return "лимит запросов (429)"
    if "451" in text:
        return "доступ к xcancel ограничен (451)"
    if "410" in text:
        return "фид отключён (410)"
    if "html page" in text:
        return "зеркало отдало HTML вместо фида"
    if "ssl" in text:
        return "TLS-сбой на зеркале"
    if "timed out" in text or "timeout" in text:
        return "таймаут"
    return error[:70]
