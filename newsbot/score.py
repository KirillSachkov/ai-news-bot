"""Scoring: which of the hundreds of daily items deserve the operator's time.

Two jobs:
  1. Estimate how "big" an item is for an AI/tech feed (release, breakthrough,
     new model, major company event) versus routine noise.
  2. Learn from the 👍/👎 feedback and shift future scores accordingly.

The score is deliberately explainable: every point added carries a reason, and
`explain()` can print them, so a wrong ranking can be debugged instead of
guessed at.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

# --- what counts as a big, must-not-miss event ------------------------------- #
RELEASE_TERMS = {
    "released": 1.2, "release": 1.0, "launch": 1.0, "launches": 1.0,
    "announces": 1.1, "announced": 1.0, "introduces": 0.9, "unveils": 0.9,
    "open-source": 1.3, "open source": 1.3, "open-weights": 1.4, "weights": 0.9,
    "now available": 0.9, "general availability": 1.0, "ships": 0.9,
    "benchmark": 0.6, "sota": 1.0, "state of the art": 1.0,
    "breakthrough": 1.5, "first ever": 1.2, "record": 0.7,
    "acquires": 1.2, "acquisition": 1.1, "raises": 0.8, "funding": 0.7,
    "valuation": 0.8, "ipo": 0.9, "partnership": 0.6,
}

ENTITY_TERMS = {
    "openai": 1.0, "anthropic": 1.0, "deepmind": 1.0, "google": 0.7,
    "meta": 0.7, "microsoft": 0.7, "nvidia": 0.9, "apple": 0.6, "amazon": 0.6,
    "xai": 0.9, "grok": 0.9, "gemini": 0.9, "claude": 1.0, "gpt": 1.0,
    "llama": 0.9, "qwen": 0.9, "deepseek": 1.1, "mistral": 0.8, "cohere": 0.5,
    "stability ai": 0.6, "midjourney": 0.6, "runway": 0.5, "sora": 0.8,
    "hugging face": 0.8, "huggingface": 0.8, "ollama": 0.7, "vllm": 0.7,
    "transformer": 0.4, "diffusion": 0.6, "rag": 0.4, "agent": 0.5,
    "agi": 0.9, "superintelligence": 0.9,
}

TOPIC_TERMS = {
    "ai": 0.35, "artificial intelligence": 0.6, "machine learning": 0.5,
    "neural": 0.4, "llm": 0.6, "large language model": 0.7, "model": 0.25,
    "inference": 0.4, "training": 0.3, "fine-tune": 0.4, "quantization": 0.4,
    "gpu": 0.4, "cuda": 0.4, "dataset": 0.3, "context window": 0.4,
    "multimodal": 0.5, "reasoning": 0.5, "robotics": 0.4, "chip": 0.4,
    "vulnerability": 0.5, "security": 0.25, "regulation": 0.4, "eu ai act": 0.6,
    "нейросет": 0.5, "искусственный интеллект": 0.6, "модель": 0.3,
    "релиз": 0.6, "обучени": 0.3, "инференс": 0.4,
}

# --- what to suppress -------------------------------------------------------- #
NOISE_PATTERNS = (
    (re.compile(r"\berid\b|реклама|рекламн|advertis|sponsored|партн[её]рский материал", re.I), 4.0),
    (re.compile(r"\bваканси|hiring|we're hiring|job opening|должност", re.I), 3.0),
    (re.compile(r"\bвебинар|webinar|подкаст|podcast episode|вебинар", re.I), 1.2),
    (re.compile(r"^(мем|шутк|юмор)|😂|😭|🗿", re.I), 1.5),
    (re.compile(r"\bскидк|промокод|распродаж|discount|coupon", re.I), 3.0),
)

_HAS_LATIN = re.compile(r"[A-Za-z]")
_HAS_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _age_hours(published):
    if not published:
        return 6.0
    try:
        stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return 6.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
    if delta < 0:
        return 0.0
    return delta


def _keyword_hits(text):
    hits = {}
    for table in (RELEASE_TERMS, ENTITY_TERMS, TOPIC_TERMS):
        for term, weight in table.items():
            if term in text:
                hits[term] = weight
    return hits


def score_item(item, source_cfg, weights, learning=True):
    """Return (score, reasons, keyword_list)."""
    title = (item.get("title") or "")
    summary = (item.get("summary") or "")
    text = ("%s %s" % (title, summary)).lower()
    reasons = []
    keywords = []
    score = 0.0

    base_weight = float(source_cfg.get("weight", 1.0))
    score += base_weight
    reasons.append("source weight +%.2f" % base_weight)

    # freshness: a 24h half-life-ish decay, capped
    age = _age_hours(item.get("published"))
    freshness = max(0.0, 2.0 - (age / 12.0))
    if freshness:
        score += freshness
        reasons.append("freshness(%.1fh) +%.2f" % (age, freshness))
    elif age > 72:
        score -= 2.0
        reasons.append("stale(%.0fh) -2.00" % age)

    # signal terms, with the title weighted double
    title_hits = _keyword_hits(title.lower())
    body_hits = _keyword_hits(text)
    for term, weight in body_hits.items():
        contribution = weight + (weight if term in title_hits else 0.0)
        score += contribution
        keywords.append(term)
        reasons.append("term '%s' +%.2f" % (term, contribution))

    # engagement from community sources
    extra = item.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}
    points = 0
    for key in ("points", "upvotes", "likes", "score"):
        try:
            points = max(points, int(extra.get(key) or 0))
        except (TypeError, ValueError):
            pass
    if points:
        bump = min(2.5, math.log10(points + 1) * 1.6)
        score += bump
        reasons.append("engagement(%s) +%.2f" % (points, bump))

    # Russian-language sources are legitimate but should not dominate an
    # English-first AI feed unless they are the only carrier of the news.
    if _HAS_CYRILLIC.search(title) and not _HAS_LATIN.search(title):
        score -= 0.4
        reasons.append("ru-only title -0.40")

    # noise penalties
    for pattern, penalty in NOISE_PATTERNS:
        if pattern.search(title) or pattern.search(summary):
            score -= penalty
            reasons.append("noise '%s' -%.2f" % (pattern.pattern[:24], penalty))

    # A research-paper dump scores high on topic words alone; without a named
    # lab, model or release it is usually not a headline for this feed.
    group = (item.get("source_group") or source_cfg.get("group") or "")
    if group == "research":
        has_entity = any(term in text for term in ENTITY_TERMS)
        has_event = any(term in text for term in RELEASE_TERMS)
        if not has_entity and not has_event:
            score -= 0.8
            reasons.append("research without entity/event -0.80")

    if extra.get("is_ad"):
        score -= 4.0
        reasons.append("telegram ad marker -4.00")

    # learned adjustments
    if learning and weights:
        src_values = weights.get("source", {})
        if item.get("source") in src_values:
            delta = src_values[item["source"]][0]
            score += delta
            reasons.append("learned source %+.2f" % delta)
        kw_values = weights.get("kw", {})
        for term in keywords:
            if term in kw_values:
                delta = kw_values[term][0]
                score += delta
                reasons.append("learned term '%s' %+.2f" % (term, delta))

    return score, reasons, keywords


def is_breaking(score, item, cfg, weights):
    """Big-event override: worth interrupting the hourly budget for.

    Requires both a high score and an actual event word in the title, plus at
    least one AI/tech entity or topic term - otherwise a "Hitachi launches a
    heat pump" headline from a general feed would steal a breaking slot.
    """
    threshold = float(cfg.get("breaking_score", 7.0))
    if score < threshold:
        return False
    title = (item.get("title") or "").lower()
    has_event = False
    for term in ("released", "release", "launch", "announces", "announced",
                 "open-source", "open source", "unveils", "breakthrough",
                 "acquires", "ships", "now available", "general availability",
                 "выпустил", "представил", "анонсировал"):
        if term in title:
            has_event = True
            break
    if not has_event:
        return False
    combined = "%s %s" % (title, (item.get("summary") or "").lower())
    for term in ENTITY_TERMS:
        if term in combined:
            return True
    for term in ("ai", "llm", "model", "neural", "gpt", "нейросет"):
        if term in combined:
            return True
    return False


def explain(item, source_cfg, weights):
    score, reasons, keywords = score_item(item, source_cfg, weights)
    lines = ["%s" % item.get("title", "")[:100]]
    for reason in reasons:
        lines.append("    %s" % reason)
    lines.append("    = %.2f" % score)
    return "\n".join(lines)
