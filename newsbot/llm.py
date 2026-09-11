"""LLM layer: DeepSeek V4 (flash) as the final judge and summarizer.

Architecture note: the model is deliberately NOT applied to every fetched item.
The free heuristic scorer (score.py) first ranks everything and cuts it down to
a small shortlist; only that shortlist reaches the model. On our volumes that
keeps the API usage inside the free grant on a new account (5M tokens) and at
cents per month afterwards.

The wire format is OpenAI-compatible, so the request is plain JSON over urllib
and the tool keeps its zero-dependency property. Everything degrades gracefully:
with no API key the bot still works, using the heuristic score alone.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

from .http import fetch

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

SYSTEM_PROMPT = """Ты — редактор новостной ленты про технологии и искусственный интеллект.
Тебе дают одну публикацию (заголовок, источник, текст-анонс, ссылку).
Оцени её как новость для занятого читателя, который следит за ИИ и разработкой.

Критерии значимости:
- новые модели, релизы, открытые веса, бенчмарки, крупные апдейты продуктов;
- прорывы, важные исследования, крупные деньги (раунды, покупки), регуляторика;
- то, что меняет инструменты или подходы разработчика.
Снижай оценку за: рекламу, вакансии, вебинары, мемы, пересказ старого,
маркетинговый шум без фактов, узколокальные новости без значения.

Верни СТРОГО json без пояснений вокруг:
{"score": <0-10>, "verdict": "send"|"skip", "category": "<model|research|product|business|policy|tool|other>",
 "reason": "<одна короткая фраза почему>", "title_ru": "<заголовок по-русски, до 90 символов>",
 "summary_ru": "<2-3 предложения по-русски, только факты из текста, без выдумок>"}
Если данных не хватает для утверждения — не додумывай, пиши то, что есть.
"""

USER_TEMPLATE = """Источник: {source} ({group})
Заголовок: {title}
Дата: {published}
Текст: {summary}
Ссылка: {url}
"""


class LLMError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc)


class DeepSeek:
    def __init__(self, api_key=None, base_url=None, model=None, timeout=45,
                 max_tokens=700, thinking=False, store=None):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.store = store
        self._resolved = None
        self._ensure_cache()

    # ------------------------------------------------------------------ model
    def resolve_model(self):
        """Pick the model id this key actually exposes.

        Model ids move (the flash tier has been renamed more than once), so the
        id is discovered from the API instead of hardcoded: the configured id
        wins if present, otherwise the first id containing "flash", otherwise
        the first available. The choice is cached in the state database.
        """
        if self._resolved:
            return self._resolved
        if not self.enabled:
            self._resolved = self.model
            return self._resolved
        cached = self.store.kv_get("llm_model") if self.store else None
        if cached:
            self._resolved = cached
            return self._resolved
        try:
            available = self.models()
        except LLMError:
            self._resolved = self.model
            return self._resolved
        if self.model in available:
            chosen = self.model
        else:
            flash = [m for m in available if "flash" in m.lower()]
            chosen = flash[0] if flash else (available[0] if available else self.model)
        self._resolved = chosen
        if self.store:
            self.store.kv_set("llm_model", chosen)
        return self._resolved

    # ------------------------------------------------------------------ cache
    def _ensure_cache(self):
        if self.store is None:
            return
        self.store.db.execute(
            """CREATE TABLE IF NOT EXISTS llm_cache (
                   uid TEXT PRIMARY KEY, verdict TEXT, ts TEXT,
                   tokens_in INTEGER, tokens_out INTEGER)""")
        self.store.db.execute(
            """CREATE TABLE IF NOT EXISTS llm_usage (
                   day TEXT PRIMARY KEY, calls INTEGER, tokens_in INTEGER,
                   tokens_out INTEGER)""")
        self.store.db.commit()

    @property
    def enabled(self):
        return bool(self.api_key)

    def cached(self, uid):
        if self.store is None:
            return None
        row = self.store.db.execute(
            "SELECT verdict FROM llm_cache WHERE uid=?", (uid,)).fetchone()
        if not row or not row["verdict"]:
            return None
        try:
            return json.loads(row["verdict"])
        except ValueError:
            return None

    def remember(self, uid, verdict, tokens_in=0, tokens_out=0):
        if self.store is None:
            return
        self.store.db.execute(
            "INSERT INTO llm_cache (uid, verdict, ts, tokens_in, tokens_out) "
            "VALUES (?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET verdict=excluded.verdict, "
            "ts=excluded.ts",
            (uid, json.dumps(verdict, ensure_ascii=False), _now().isoformat(),
             tokens_in, tokens_out))
        day = _now().strftime("%Y-%m-%d")
        self.store.db.execute(
            """INSERT INTO llm_usage (day, calls, tokens_in, tokens_out)
               VALUES (?,1,?,?) ON CONFLICT(day) DO UPDATE SET
               calls=calls+1, tokens_in=tokens_in+excluded.tokens_in,
               tokens_out=tokens_out+excluded.tokens_out""",
            (day, tokens_in, tokens_out))
        self.store.db.commit()

    def usage_today(self):
        if self.store is None:
            return {}
        row = self.store.db.execute(
            "SELECT * FROM llm_usage WHERE day=?",
            (_now().strftime("%Y-%m-%d"),)).fetchone()
        return dict(row) if row else {"calls": 0, "tokens_in": 0, "tokens_out": 0}

    # ------------------------------------------------------------------- http
    def models(self):
        """List model ids available to this key (never guess an id)."""
        result = fetch(self.base_url + "/models", accept="application/json",
                       timeout=self.timeout,
                       extra_headers={"Authorization": "Bearer %s" % self.api_key})
        if not result.ok:
            if result.status == 401:
                raise LLMError("401 unauthorized: DEEPSEEK_API_KEY is missing or wrong")
            raise LLMError("models request failed: %s" % (result.error or result.status))
        try:
            data = json.loads(result.text())
        except ValueError as exc:
            raise LLMError("invalid JSON from /models: %s" % exc)
        return [m.get("id") for m in data.get("data", []) if m.get("id")]

    # ------------------------------------------------------------------- chat
    def _chat(self, messages, json_mode=True):
        import urllib.error
        import urllib.request

        payload = {
            "model": self.resolve_model(),
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if not self.thinking:
            payload["thinking"] = {"type": "disabled"}

        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
                "Accept": "application/json",
            })
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8", "replace"))
                usage = body.get("usage") or {}
                content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return content, int(usage.get("prompt_tokens") or 0), \
                    int(usage.get("completion_tokens") or 0)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                last_error = "HTTP %s %s" % (exc.code, detail)
                if exc.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise LLMError(last_error)
            except Exception as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
        raise LLMError(last_error or "unknown error")

    def judge(self, item, group=None, use_cache=True):
        """Return the model verdict dict, or None if the LLM is unavailable."""
        if not self.enabled:
            return None
        self.resolve_model()
        uid = item.get("uid") or item.get("url") or item.get("title")
        if use_cache:
            hit = self.cached(uid)
            if hit is not None:
                hit["cached"] = True
                return hit

        prompt = USER_TEMPLATE.format(
            source=item.get("source", ""), group=group or item.get("source_group", ""),
            title=item.get("title", ""), published=item.get("published") or "n/a",
            summary=(item.get("summary") or "")[:1200],
            url=item.get("url") or "",
        )
        content, tokens_in, tokens_out = self._chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        verdict = _loads_loose(content)
        if verdict is None:
            raise LLMError("model returned unparseable JSON: %r" % content[:160])
        verdict["score"] = _clamp(verdict.get("score"), 0, 10)
        verdict["cached"] = False
        self.remember(uid, verdict, tokens_in, tokens_out)
        return verdict


def _clamp(value, low, high):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _loads_loose(text):
    """Parse JSON even if the model wrapped it in prose or fences."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None
    return None
