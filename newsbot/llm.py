"""LLM layer: DeepSeek V4 (flash) as the editor: judge, repeat check and draft.

Architecture note: the model is deliberately NOT applied to every fetched item.
The free heuristic scorer (score.py) first ranks everything and cuts it down to
a small shortlist; only that shortlist reaches the model. On our volumes that
keeps the API usage inside the free grant on a new account (5M tokens) and at
cents per month afterwards.

The model does not rate "importance" in the abstract. It is given the editorial
profile (editorial.md: what the reference channels publish, what they never
do, real posts as examples) and answers one question - would those channels
take this item - plus writes the draft in their voice.

The wire format is OpenAI-compatible, so the request is plain JSON over urllib
and the tool keeps its zero-dependency property. Everything degrades gracefully:
with no API key the bot still works, using the heuristic score alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

from .http import fetch

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

RUBRICS = ("release", "tool", "breakthrough", "viral", "industry", "safety",
           "outage", "hardware", "career", "other")

# Used only when editorial.md is missing, so the judge still has a direction.
DEFAULT_EDITORIAL = """Ориентир — русскоязычные Telegram-каналы про ИИ и разработку @codecamp и
@data_secrets: релизы моделей, полезные бесплатные инструменты, прорывы, вирусные истории
из мира разработки, крупные сделки, инциденты с ИИ-агентами. Не берут рекламу, анонсы
мероприятий, корпоративные туториалы, патч-релизы, политику и войну."""

SYSTEM_PROMPT = """Ты — шеф-редактор русскоязычного Telegram-канала про ИИ и разработку.
Тебе приносят одну публикацию из источника. Реши, взяли бы её в ленту каналы-ориентиры,
и если да — подготовь черновик поста в их стиле.

{editorial}

## Шкала score — редакционный fit, 0–10
9–10 — сюжет дня: об этом напишут оба канала (фронтир-релиз, сделка на миллиарды, громкий прорыв или инцидент).
7–8 — уверенно в ленту: полезная находка с понятной выгодой, заметный релиз, история, которую будут пересказывать.
5–6 — проходной: взяли бы только в пустой день (рядовой апдейт, нишевый инструмент, исследование без вау).
3–4 — не формат (см. «Что каналы не берут»).
0–2 — мимо: реклама, политика и война, старьё, мусор.

## Правила
- Оценивай, будут ли об этом говорить айтишники, а не формальную важность события.
- Имя крупной компании в заголовке ещё не новость: важно, что конкретно произошло.
- Мало текста (голый заголовок, обрывок поста) — не повод занижать score: оцени само событие, если оно понятно из заголовка. Занижай, только если из текста нельзя понять, что произошло.
- Не выдумывай факты, цифры, бенчмарки и цены: только то, что есть в тексте. Мало текста — пиши черновик коротко.
- verdict = "send" только при score ≥ 7, иначе "skip".

Верни СТРОГО JSON без пояснений вокруг:
{"score": <0-10>, "verdict": "send"|"skip",
 "rubric": "release|tool|breakthrough|viral|industry|safety|outage|hardware|career|other",
 "channel": "codecamp|data_secrets|both|none",
 "reason": "<одна фраза: почему зайдёт или почему не формат>",
 "event": "<ключ события по-английски, 3–8 слов: кто + что + объект, например deepseek releases v4.1 flash>",
 "title_ru": "<заголовок-хук в стиле канала, до 100 символов>",
 "summary_ru": "<черновик поста в стиле канала: 2–4 предложения или «Главное:» с пунктами, без ссылок>"}
"""

USER_TEMPLATE = """Источник: {source} ({group})
Заголовок: {title}
Дата: {published}
Текст: {summary}
Ссылка: {url}
"""

SAME_STORY_PROMPT = """Ты проверяешь ленту новостей на повторы.
Дана новость-кандидат и пронумерованный список новостей, уже отправленных читателю за последние дни.
Повтор — это то же событие: тот же релиз, та же сделка, тот же инцидент, тот же проект — даже если заголовок другой, на другом языке или из другого издания. Реакции, мнения, мелкие подробности и пересказы того же события — тоже повтор.
Не повтор: другое событие той же компании; продолжение с существенно новым фактом (слух стал официальным релизом, анонс стал доступным продуктом, расследование принесло новые крупные данные).
Верни СТРОГО JSON: {"duplicate_of": <номер из списка или null>, "reason": "<коротко>"}"""

SAME_STORY_TEMPLATE = """Кандидат:
Заголовок: {title}
Заголовок редактора: {title_ru}
Событие: {event}
Текст: {summary}

Уже отправлено:
{sent}
"""


class LLMError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc)


class DeepSeek:
    def __init__(self, api_key=None, base_url=None, model=None, timeout=45,
                 max_tokens=900, thinking=False, store=None, profile=None):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.store = store
        self.profile = (profile or "").strip()
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

    @property
    def profile_tag(self):
        """Identity of the question the judge answers.

        The prompt and the editorial profile together are the question, so both
        are hashed: a verdict formed under an older profile carries a different
        tag and the pipeline asks again instead of trusting it.
        """
        blob = ("%s\n%s" % (SYSTEM_PROMPT, self.profile)).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()[:8]

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
        self.store.db.commit()
        self.record_usage(tokens_in, tokens_out)

    def record_usage(self, tokens_in=0, tokens_out=0):
        if self.store is None:
            return
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

    def system_prompt(self):
        """The editor's brief: scale and output format around the profile.

        The heuristic filter can only match words; the model is what tells a
        release the audience will discuss apart from a corporate tutorial that
        merely names the same lab. The profile lives in editorial.md so it can
        be tuned without touching code.
        """
        return SYSTEM_PROMPT.replace("{editorial}", self.profile or DEFAULT_EDITORIAL)

    def judge(self, item, group=None, use_cache=True):
        """Return the editorial verdict dict, or None if the LLM is unavailable."""
        if not self.enabled:
            return None
        self.resolve_model()
        tag = self.profile_tag
        uid = "%s|%s" % (tag, item.get("uid") or item.get("url") or item.get("title"))
        if use_cache:
            hit = self.cached(uid)
            if isinstance(hit, dict):
                hit = normalize_verdict(hit)
                hit["cached"] = True
                return hit

        prompt = USER_TEMPLATE.format(
            source=item.get("source", ""), group=group or item.get("source_group", ""),
            title=item.get("title", ""), published=item.get("published") or "n/a",
            summary=(item.get("summary") or "")[:1200],
            url=item.get("url") or "",
        )
        content, tokens_in, tokens_out = self._chat([
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": prompt},
        ])
        verdict = _loads_loose(content)
        if not isinstance(verdict, dict):
            raise LLMError("model returned unparseable JSON: %r" % content[:160])
        verdict = normalize_verdict(verdict)
        verdict["profile_tag"] = tag
        verdict["cached"] = False
        self.remember(uid, verdict, tokens_in, tokens_out)
        return verdict

    def same_story(self, item, verdict, recent):
        """Index into `recent` of the sent item this one retells, or None.

        `recent` is newest first; up to 60 entries are shown to the model, the
        whole repeat window of a budgeted feed (13 a day for four days is 52).
        """
        if not self.enabled or not recent:
            return None
        shown = recent[:60]
        lines = []
        for position, other in enumerate(shown, 1):
            label = other.get("title_ru") or other.get("title") or ""
            if other.get("title_ru") and other.get("title") and other["title"] != other["title_ru"]:
                label = "%s (%s)" % (other["title_ru"], other["title"])
            lines.append("%d. %s" % (position, label[:220]))
        verdict = verdict or {}
        prompt = SAME_STORY_TEMPLATE.format(
            title=item.get("title") or "", title_ru=verdict.get("title_ru") or "",
            event=verdict.get("event") or "", summary=(item.get("summary") or "")[:500],
            sent="\n".join(lines))
        content, tokens_in, tokens_out = self._chat([
            {"role": "system", "content": SAME_STORY_PROMPT},
            {"role": "user", "content": prompt},
        ])
        self.record_usage(tokens_in, tokens_out)
        data = _loads_loose(content) or {}
        try:
            position = int(data.get("duplicate_of"))
        except (TypeError, ValueError):
            return None
        if 1 <= position <= len(shown):
            return position - 1
        return None


def normalize_verdict(verdict):
    """Coerce a model answer into the shape the pipeline and the card rely on.

    The prompt asks for strings, but a model may answer a bulleted draft with a
    list or an event key with an object, and one such verdict must not be able
    to crash selection for every item behind it.
    """
    verdict = dict(verdict)
    verdict["score"] = _clamp(verdict.get("score"), 0, 10)
    if not isinstance(verdict.get("rubric"), str) or verdict["rubric"] not in RUBRICS:
        verdict["rubric"] = "other"
    for key in ("reason", "event", "title_ru", "summary_ru", "channel"):
        verdict[key] = _as_text(verdict.get(key))
    if verdict.get("verdict") not in ("send", "skip"):
        verdict["verdict"] = "send" if verdict["score"] >= 7 else "skip"
    return verdict


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(part) for part in value if part is not None)
    if isinstance(value, dict):
        return "\n".join("%s: %s" % (key, _as_text(part)) for key, part in value.items())
    return str(value)


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
