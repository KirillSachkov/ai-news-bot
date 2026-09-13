"""Editorial selection: judge gate, budgets, repeats, reference channels, card.

Stdlib only, like the bot: python3 -m unittest discover -s tests
"""

import collections
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot  # noqa: E402
from newsbot import feeds, pipeline, render  # noqa: E402
from newsbot.llm import DeepSeek, LLMError, _loads_loose  # noqa: E402
from newsbot.store import Store  # noqa: E402

TAG = "tag1"

CFG = {
    "min_score": 7.0, "reject_below": 5.0, "require_verdict": True, "learning": False,
    "max_per_hour": 5, "max_per_day": 10, "min_gap_minutes": 0,
    "breaking_score": 9.0, "breaking_corroborated_score": 8.0,
    "breaking_max_per_hour": 2, "breaking_max_per_day": 3, "breaking_min_gap_minutes": 0,
    "max_age_hours": 6, "corroboration_min_groups": 2, "corroboration_bonus": 0.5,
    "reference_bonus": 1.0, "breaking_min_groups": 3, "dedup_window_hours": 96,
    "politeness_delay_seconds": 0,
}

SOURCES = [
    {"name": "techcrunch_ai", "group": "media"},
    {"name": "hn_front", "group": "community"},
    {"name": "tg_codecamp", "group": "telegram", "role": "reference", "channel": "codecamp"},
]


def iso(hours_ago=0.0):
    stamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return stamp.isoformat(timespec="seconds")


def judged(score, event="", **extra):
    data = {"score": score, "verdict": "send" if score >= 7 else "skip",
            "rubric": "release", "event": event, "profile_tag": TAG}
    data.update(extra)
    return data


class FakeEditor(object):
    enabled = True
    profile_tag = TAG

    def __init__(self, duplicate_index=None):
        self.duplicate_index = duplicate_index
        self.calls = 0

    def same_story(self, item, verdict, recent):
        self.calls += 1
        return self.duplicate_index


class FakeJudge(FakeEditor):
    """Judges everything on format, or fails the way the real API can."""

    def __init__(self, error=None, error_for=()):
        FakeEditor.__init__(self)
        self.error = error
        self.error_for = error_for
        self.judged = collections.Counter()

    def judge(self, item, group=None):
        self.judged[item["title"]] += 1
        if self.error and (not self.error_for or any(p in item["title"] for p in self.error_for)):
            raise LLMError(self.error)
        return judged(8, "item %s" % item["nid"])


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "news.db"))
        pipeline.ensure_verdict_table(self.store)
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, title, source="techcrunch_ai", group="media", hours_ago=1.0,
            summary="", verdict=None, status=None):
        self.counter += 1
        item = {"uid": "test:%d" % self.counter, "source": source, "source_group": group,
                "title": title, "url": "https://example.com/%d" % self.counter,
                "summary": summary, "published": iso(hours_ago), "extra": {}}
        nid = self.store.add_item(item)
        self.store.assign_story(nid, title, summary, source, group, window_minutes=360)
        if verdict is not None:
            pipeline.set_verdict(self.store, nid, verdict)
        if status:
            self.store.mark(nid, status)
        return nid

    def send(self, nid, kind="news"):
        self.store.mark(nid, "sent", sent=True, kind=kind)

    def select(self, cfg=None, llm=None, **kwargs):
        editor = llm if llm is not None else FakeEditor()
        return pipeline.select_due(self.store, SOURCES, cfg or CFG, llm=editor, **kwargs)

    def chosen_nids(self, **kwargs):
        return [row["nid"] for _, _, row in self.select(**kwargs)]

    def status(self, nid):
        return self.store.item(nid)["status"]


class JudgeGateTest(StoreCase):
    def test_unjudged_item_waits_for_the_editor(self):
        nid = self.add("OpenAI ships a new Codex feature")
        self.assertEqual(self.select(), [])
        self.assertEqual(self.status(nid), "pending")

    def test_on_format_item_is_selected(self):
        nid = self.add("Qwen releases Qwen3.9 27B with open weights",
                       verdict=judged(8, "qwen releases qwen3.9 27b"))
        self.assertEqual(self.chosen_nids(), [nid])

    def test_verdict_from_an_older_profile_is_not_trusted(self):
        nid = self.add("Qwen releases Qwen3.9 27B with open weights",
                       verdict=judged(9, "qwen releases qwen3.9 27b", profile_tag="old"))
        self.assertEqual(self.select(), [])
        self.assertEqual(self.status(nid), "pending")

    def test_clear_miss_is_rejected_and_near_miss_waits(self):
        miss = self.add("How Acme automates invoices with Bedrock Agents", verdict=judged(3))
        near = self.add("Mistral updates Le Chat mobile layout", verdict=judged(6))
        self.assertEqual(self.select(), [])
        self.assertEqual(self.status(miss), "rejected")
        self.assertEqual(self.status(near), "pending")

    def test_daily_budget_caps_the_normal_lane(self):
        for index, name in enumerate(("Zephyrine", "Quokkabase")):
            nid = self.add("%s launches a thing" % name,
                           verdict=judged(8, "%s launches thing %d" % (name.lower(), index)))
            self.send(nid)
        candidate = self.add("Mistral ships Magistral 2",
                             verdict=judged(8, "mistral ships magistral 2"))
        pipeline.check_repeats(self.store, SOURCES, CFG, FakeEditor())
        self.assertEqual(self.select(dict(CFG, max_per_day=2)), [])
        self.assertEqual(self.chosen_nids(cfg=dict(CFG, max_per_day=3)), [candidate])

    def test_breaking_needs_story_of_the_day_or_confirmation(self):
        self.add("Anthropic releases Claude Opus 6",
                 verdict=judged(9.5, "anthropic releases claude opus 6"))
        chosen = self.select()
        self.assertTrue(chosen[0][1])

    def test_strong_but_unconfirmed_item_goes_to_the_normal_lane(self):
        self.add("Z.ai releases GLM-5.4", verdict=judged(8.2, "z.ai releases glm-5.4"))
        chosen = self.select()
        self.assertEqual(len(chosen), 1)
        self.assertFalse(chosen[0][1])


class JudgeFailureTest(StoreCase):
    RERANK = dict(CFG, llm_min_heuristic=-100.0, llm_max_per_cycle=20, llm_delay_seconds=0,
                  llm_item_max_failures=2)

    def test_items_that_break_the_judge_do_not_block_the_rest(self):
        for number in range(3):
            self.add("Poisoned OpenAI Anthropic DeepSeek release %d" % number)
        normal = self.add("Mistral ships Magistral 2")
        judge = FakeJudge(error="model returned unparseable JSON: 'oops'", error_for=("Poisoned",))
        for _ in range(3):
            pipeline.rerank_pending(self.store, SOURCES, self.RERANK, judge)
        self.assertIsNotNone(pipeline.get_verdict(self.store, normal))
        poisoned = [count for title, count in judge.judged.items() if title.startswith("Poisoned")]
        self.assertEqual(sorted(poisoned), [2, 2, 2])

    def test_api_outage_stops_the_pass_without_blaming_items(self):
        for number in range(5):
            self.add("Story number %d about Qwen" % number)
        judge = FakeJudge(error="URLError: <urlopen error timed out>")
        pipeline.rerank_pending(self.store, SOURCES, self.RERANK, judge)
        self.assertEqual(sum(judge.judged.values()), 3)
        self.assertEqual(pipeline.judge_failures(self.store), {})


class RepeatTest(StoreCase):
    def test_retelling_of_a_sent_story_is_dropped(self):
        first = self.add("DeepSeek releases V4.1 Flash",
                         verdict=judged(8, "deepseek releases v4.1 flash"))
        self.send(first)
        second = self.add("Вышла DeepSeek V4.1 Flash: обходит GPT-5.6 Sol", source="hn_front",
                          group="community", hours_ago=0.5,
                          verdict=judged(8, "deepseek v4.1 flash beats gpt-5.6 sol"))
        self.assertEqual(self.select(), [])
        self.assertEqual(self.status(second), "duplicate")

    @staticmethod
    def repeat_of(sent_title, sent_event, title, event):
        recent = [pipeline.recent_entry({"nid": 1, "story_id": 10, "title": sent_title},
                                        {"event": sent_event})]
        return pipeline.matches_recent({"nid": 2, "story_id": 11, "title": title},
                                       {"event": event}, recent)

    def test_event_keys_catch_a_retelling_without_shared_story(self):
        self.assertIsNotNone(self.repeat_of(
            "DeepSeek releases V4.1 Flash", "deepseek releases v4.1 flash",
            "Кит вернулся с новой моделью", "deepseek v4.1 flash tops benchmarks"))
        self.assertIsNotNone(self.repeat_of(
            "OpenAI releases GPT-6 Astra", "openai releases gpt-6 astra",
            "OpenAI выкатила GPT-6 Astra — самую мощную модель", "openai launches gpt-6 astra"))

    def test_another_event_of_the_same_lab_is_not_a_repeat(self):
        self.assertIsNone(self.repeat_of(
            "OpenAI releases GPT-6 Astra", "openai releases gpt-6 astra",
            "Astra Pro limits arrive in ChatGPT", "openai sets astra pro message limits"))

    def test_sibling_releases_and_outages_are_not_repeats(self):
        pairs = (
            ("Google releases Gemini 3 Pro", "google releases gemini 3 pro",
             "Google releases Gemini 3 Flash", "google releases gemini 3 flash"),
            ("Anthropic releases Claude Opus 5", "anthropic releases claude opus 5",
             "Anthropic releases Claude Sonnet 5", "anthropic releases claude sonnet 5"),
            ("OpenAI launches ChatGPT Atlas", "openai launches chatgpt atlas",
             "OpenAI launches ChatGPT Pulse", "openai launches chatgpt pulse"),
            ("GitHub is down worldwide", "github outage worldwide",
             "Slack is down worldwide", "slack outage worldwide"),
            ("GitHub blocked in Russia", "github blocked in russia",
             "ChatGPT blocked in Russia", "chatgpt blocked in russia"),
        )
        for pair in pairs:
            with self.subTest(candidate=pair[2]):
                self.assertIsNone(self.repeat_of(*pair))

    def test_candidate_waits_for_the_editor_repeat_check_after_a_send(self):
        sent = self.add("Nvidia agrees to buy Hugging Face",
                        verdict=judged(9, "nvidia acquires hugging face"))
        self.send(sent)
        candidate = self.add("Jensen Huang explains the Poolside bet", source="hn_front",
                             group="community", verdict=judged(8, "jensen huang explains poolside"))
        editor = FakeEditor(duplicate_index=None)
        self.assertEqual(self.select(llm=editor), [])
        self.assertEqual(self.status(candidate), "pending")
        self.assertEqual(editor.calls, 0, "selection must never call the model")

        pipeline.check_repeats(self.store, SOURCES, CFG, editor)
        self.assertEqual(self.chosen_nids(llm=editor), [candidate])
        pipeline.check_repeats(self.store, SOURCES, CFG, editor)
        self.assertEqual(editor.calls, 1, "the answer is kept until something new is sent")

        other = self.add("Mistral ships Magistral 2", verdict=judged(8, "mistral ships magistral 2"))
        self.send(other)
        repeat_editor = FakeEditor(duplicate_index=0)
        pipeline.check_repeats(self.store, SOURCES, CFG, repeat_editor)
        self.assertEqual(self.select(llm=repeat_editor), [])
        self.assertEqual(repeat_editor.calls, 1)
        self.assertEqual(self.status(candidate), "duplicate")

    def test_dry_run_does_not_wait_for_the_repeat_check(self):
        sent = self.add("Nvidia agrees to buy Hugging Face",
                        verdict=judged(9, "nvidia acquires hugging face"))
        self.send(sent)
        self.add("Jensen Huang explains the Poolside bet", source="hn_front", group="community",
                 verdict=judged(8, "jensen huang explains poolside"))
        editor = FakeEditor(duplicate_index=0)
        self.assertEqual(len(self.select(llm=editor, dry=True)), 1)
        self.assertEqual(editor.calls, 0)


class ReferenceChannelTest(StoreCase):
    def test_reference_posts_are_signals_not_queue_items(self):
        post = {"uid": "tg:codecamp/1", "title": "Unsloth выпустили Dynamic 3.0",
                "url": "https://t.me/codecamp/1", "summary": "", "published": iso(0.5),
                "extra": {}}
        original = pipeline.fetch_source
        pipeline.fetch_source = lambda source, etag=None, last_modified=None: ([dict(post)], None, {})
        try:
            pipeline.collect(self.store, [SOURCES[2]], CFG)
        finally:
            pipeline.fetch_source = original
        self.assertEqual(self.store.pending(), [])
        statuses = [r["status"] for r in self.store.db.execute("SELECT status FROM items")]
        self.assertEqual(statuses, ["signal"])

    def test_reference_post_left_pending_by_an_older_version_is_never_sent(self):
        nid = self.add("Unsloth выпустили Dynamic 3.0", source="tg_codecamp", group="telegram",
                       verdict=judged(9.2, "unsloth ships dynamic 3.0"))
        self.assertEqual(self.select(), [])
        self.assertEqual(self.status(nid), "signal")

    def test_reference_confirmation_lifts_a_near_miss_and_shows_on_card(self):
        nid = self.add("Unsloth ships Dynamic 3.0 quantization for Qwen3.8",
                       verdict=judged(6.5, "unsloth ships dynamic 3.0 quantization", rubric="tool"))
        self.add("Unsloth выпустили Dynamic 3.0 для Qwen3.8 — модели влезают в 8 ГБ",
                 source="tg_codecamp", group="telegram", status="signal")
        story = pipeline.story_view(self.store, self.store.item(nid)["story_id"],
                                    pipeline.reference_channels(SOURCES))
        self.assertEqual(story["reference"], ["codecamp"])
        self.assertEqual(story["groups"], 1)

        chosen = self.select()
        self.assertEqual([row["nid"] for _, _, row in chosen], [nid])
        card = render.item_card(chosen[0][2], verdict=pipeline.get_verdict(self.store, nid),
                                story=story)
        self.assertIn("@codecamp", card)

    def test_reference_confirmation_opens_the_breaking_lane_for_a_strong_item(self):
        nid = self.add("Unsloth ships Dynamic 3.0 quantization for Qwen3.8",
                       verdict=judged(8.3, "unsloth ships dynamic 3.0 quantization"))
        self.add("Unsloth выпустили Dynamic 3.0 для Qwen3.8", source="tg_codecamp",
                 group="telegram", status="signal")
        chosen = self.select()
        self.assertEqual(chosen[0][2]["nid"], nid)
        self.assertTrue(chosen[0][1])


class CardTest(unittest.TestCase):
    item = {"title": "Recordly", "source": "hn_show", "published": iso(1),
            "url": "https://github.com/example/recordly", "extra": {}}

    def test_card_explains_the_editorial_call(self):
        verdict = {"score": 8, "rubric": "tool", "channel": "codecamp",
                   "reason": "бесплатная замена платному софту",
                   "title_ru": "Нашли опенсорсный рекордер экрана",
                   "summary_ru": "Recordly умеет автозум на курсор."}
        card = render.item_card(self.item, verdict=verdict,
                                story={"sources": 2, "groups": 2, "reference": ["codecamp"]})
        for part in ("8/10", "Полезное", "в духе @codecamp", "почему: бесплатная",
                     "Recordly умеет", "уже написали: @codecamp", "источников: 2"):
            self.assertIn(part, card)

    def test_legacy_verdict_still_renders(self):
        card = render.item_card(self.item, verdict={"score": 6.5, "category": "model",
                                                    "title_ru": "Старый вердикт"})
        self.assertIn("Релиз", card)
        self.assertIn("6.5/10", card)

    def test_card_escapes_html(self):
        card = render.item_card(dict(self.item, title="<script>x</script>"))
        self.assertIn("&lt;script&gt;", card)
        self.assertNotIn("<script>", card)


class EditorTest(unittest.TestCase):
    def test_profile_is_in_the_prompt_and_in_the_cache_tag(self):
        first = DeepSeek(api_key="test", profile="Профиль А")
        second = DeepSeek(api_key="test", profile="Профиль Б")
        self.assertIn("Профиль А", first.system_prompt())
        self.assertNotEqual(first.profile_tag, second.profile_tag)

    def test_repository_profile_is_what_the_bot_loads(self):
        text = bot.load_editorial({"editorial_path": "editorial.md"})
        self.assertIn("## Рубрики", text)
        self.assertIn("@data_secrets", text)

    def test_verdict_fields_are_forced_to_text(self):
        editor = DeepSeek(api_key="test", profile="x")
        editor._resolved = "test-model"
        answer = {"score": "8", "verdict": "send", "rubric": ["tool"], "event": {"who": "acme"},
                  "title_ru": None, "summary_ru": ["Главное:", "- пункт"]}
        editor._chat = lambda messages, json_mode=True: (json.dumps(answer), 10, 5)
        verdict = editor.judge({"uid": "x", "title": "t"}, use_cache=False)
        self.assertEqual(verdict["score"], 8.0)
        self.assertEqual(verdict["rubric"], "other")
        self.assertEqual(verdict["summary_ru"], "Главное:\n- пункт")
        self.assertEqual(verdict["title_ru"], "")
        self.assertIsInstance(verdict["event"], str)
        self.assertIn("Главное:", render.item_card({"title": "t"}, verdict=verdict))

    def test_judge_is_told_today_and_how_to_treat_old_events(self):
        # A rising repository is "found" today even when its story is days old:
        # the first live card after launch was exactly that.
        editor = DeepSeek(api_key="test", profile="x")
        editor._resolved = "test-model"
        seen = {}

        def fake_chat(messages, json_mode=True):
            seen["messages"] = messages
            return ('{"score": 3, "verdict": "skip"}', 10, 5)

        editor._chat = fake_chat
        editor.judge({"uid": "gh-repo:openai/navierstokesandeuler",
                      "title": "openai/NavierStokesAndEuler: Lean certificates",
                      "summary": "New GitHub repository, created 2026-09-08, 1844 stars.",
                      "published": iso(0)}, use_cache=False)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIn("Сегодня: %s" % today, seen["messages"][1]["content"])
        self.assertIn("Найдено в источнике:", seen["messages"][1]["content"])
        self.assertIn("больше двух суток назад", seen["messages"][0]["content"])

    def test_same_story_maps_the_answer_to_a_recent_index(self):
        editor = DeepSeek(api_key="test", profile="x")
        recent = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
        editor._chat = lambda messages, json_mode=True: ('{"duplicate_of": 2}', 10, 5)
        self.assertEqual(editor.same_story({"title": "B again"}, {}, recent), 1)
        editor._chat = lambda messages, json_mode=True: ('{"duplicate_of": null}', 10, 5)
        self.assertIsNone(editor.same_story({"title": "D"}, {}, recent))
        editor._chat = lambda messages, json_mode=True: ('{"duplicate_of": 9}', 10, 5)
        self.assertIsNone(editor.same_story({"title": "D"}, {}, recent))

    def test_loose_json(self):
        self.assertEqual(_loads_loose('```json\n{"score": 8}\n```'), {"score": 8})
        self.assertEqual(_loads_loose('ответ: {"score": 7} конец'), {"score": 7})


class GithubRisingTest(unittest.TestCase):
    def test_parses_search_results_and_skips_forks(self):
        payload = {"items": [
            {"full_name": "acme/recordly", "description": "Free screen recorder",
             "html_url": "https://github.com/acme/recordly", "stargazers_count": 1200,
             "language": "TypeScript", "topics": ["macos", "recorder"],
             "created_at": "2026-09-10T08:00:00Z"},
            {"full_name": "someone/fork", "fork": True, "html_url": "https://github.com/x/y"},
        ]}
        seen = {}

        def fake_fetch_json(url, **kwargs):
            seen["url"] = url
            return payload, None

        original = feeds.fetch_json
        feeds.fetch_json = fake_fetch_json
        try:
            items, error = feeds.github_rising(days=7, min_stars=100, limit=10)
        finally:
            feeds.fetch_json = original
        self.assertIsNone(error)
        self.assertIn("stars:>=100", seen["url"])
        self.assertEqual([i["uid"] for i in items], ["gh-repo:acme/recordly"])
        self.assertEqual(items[0]["extra"]["points"], 1200)
        self.assertIn("Free screen recorder", items[0]["title"])

    @staticmethod
    def run_with(fake_fetch_json, token, fake_curl_json=None):
        original, original_curl = feeds.fetch_json, feeds._curl_json
        feeds.fetch_json = fake_fetch_json
        feeds._curl_json = fake_curl_json or (lambda url, headers, **kwargs: (None, "curl HTTP 403"))
        try:
            return feeds.github_rising(days=7, min_stars=100, limit=10, token=token)
        finally:
            feeds.fetch_json, feeds._curl_json = original, original_curl

    def test_refused_anonymous_search_is_retried_through_curl(self):
        calls = []

        def fake_curl_json(url, headers, **kwargs):
            calls.append(headers)
            return {"items": [{"full_name": "acme/tool", "html_url": "https://github.com/acme/tool",
                               "stargazers_count": 500}]}, None

        items, error = self.run_with(lambda url, **kwargs: (None, "HTTP 403"), token="",
                                     fake_curl_json=fake_curl_json)
        self.assertIsNone(error)
        self.assertEqual([i["uid"] for i in items], ["gh-repo:acme/tool"])
        self.assertNotIn("Authorization", calls[0])

    def test_token_is_sent_only_when_configured(self):
        seen = []

        def fake_fetch_json(url, **kwargs):
            seen.append(kwargs.get("extra_headers") or {})
            return {"items": []}, None

        self.run_with(fake_fetch_json, token="github_pat_test")
        self.run_with(fake_fetch_json, token="")
        self.assertEqual(seen[0].get("Authorization"), "Bearer github_pat_test")
        self.assertNotIn("Authorization", seen[1])
        self.assertEqual(seen[1].get("Accept"), "application/vnd.github+json")

    def test_rate_limit_without_a_token_says_how_to_fix_it(self):
        items, error = self.run_with(lambda url, **kwargs: (None, "HTTP 403"), token="")
        self.assertEqual(items, [])
        self.assertIn("GITHUB_TOKEN", error)
        self.assertIn("GITHUB_TOKEN", render.short_reason(error))
        curl_calls = []

        def curl_must_not_run(url, headers, **kwargs):
            curl_calls.append(headers)
            return None, "curl HTTP 403"

        _, error_with_token = self.run_with(lambda url, **kwargs: (None, "HTTP 403"),
                                            token="github_pat_test",
                                            fake_curl_json=curl_must_not_run)
        self.assertEqual(error_with_token, "HTTP 403")
        self.assertEqual(curl_calls, [], "a token must never be handed to curl")


if __name__ == "__main__":
    unittest.main()
