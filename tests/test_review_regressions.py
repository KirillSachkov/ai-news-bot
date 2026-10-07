"""Independent regression checks from issue #1 review; no external API calls."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from newsbot import feeds, http, pipeline
from newsbot.llm import DeepSeek, LLMError
from newsbot.runner import Runner
from test_audit import Transport
from test_editorial import CFG, FakeEditor, FakeJudge, StoreCase, iso, judged


class ReviewRegressions(StoreCase):
    def test_initial_bootstrap_obeys_pause_received_during_fetch(self):
        self.store.kv_set('chat_id', 1)
        self.add('Distinct Quokka release', verdict=judged(8))
        runner = Runner(Transport(), self.store, [], CFG, FakeEditor(), verbose=False)
        runner.handle_message({'chat': {'id': 1}, 'text': '/pause'})
        runner._results.put(('full', {'new': 1, 'sources': 1, 'ok': 1, 'fetched': 1}))
        runner.finish_collections()
        self.assertTrue(runner.is_paused())
        self.assertEqual(self.store.last_send_id(), 0)

    def test_initial_digest_bootstrap_obeys_mute_received_during_fetch(self):
        self.store.kv_set('chat_id', 1)
        self.add('Distinct Quokka release', verdict=judged(8))
        runner = Runner(Transport(), self.store, [], CFG, FakeEditor(), verbose=False)
        runner.handle_message({'chat': {'id': 1}, 'text': '/digest'})
        runner.handle_message({'chat': {'id': 1}, 'text': '/mute 2'})
        runner._results.put(('digest', {'new': 1, 'sources': 1, 'ok': 1, 'fetched': 1}))
        runner.finish_collections()
        self.assertTrue(runner.is_paused())
        self.assertEqual(self.store.last_send_id(), 0)
        self.assertFalse(runner._digest_busy)

    def test_digest_completion_obeys_pause_received_during_fetch(self):
        self.store.kv_set('chat_id', 1)
        self.store.kv_set('bootstrap_done', 'done')
        nid = self.add('Distinct Quokka release', verdict=judged(8))
        transport = Transport()
        runner = Runner(transport, self.store, [], CFG, FakeEditor(), verbose=False)
        runner.handle_message({'chat': {'id': 1}, 'text': '/digest'})
        runner.handle_message({'chat': {'id': 1}, 'text': '/pause'})
        runner._results.put(('digest', {'new': 1}))
        runner.finish_collections()
        self.assertTrue(runner.is_paused())
        self.assertEqual(self.status(nid), 'pending')
        self.assertEqual(self.store.last_send_id(), 0)
        self.assertFalse(runner._digest_busy)

    def test_digest_completion_obeys_mute_received_during_fetch(self):
        self.store.kv_set('chat_id', 1)
        self.store.kv_set('bootstrap_done', 'done')
        nid = self.add('Distinct Quokka release', verdict=judged(8))
        runner = Runner(Transport(), self.store, [], CFG, FakeEditor(), verbose=False)
        runner.handle_message({'chat': {'id': 1}, 'text': '/digest'})
        runner.handle_message({'chat': {'id': 1}, 'text': '/mute 2'})
        runner._results.put(('digest', {'new': 1}))
        runner.finish_collections()
        self.assertEqual(self.status(nid), 'pending')
        self.assertEqual(self.store.last_send_id(), 0)

    def test_poison_repeat_checks_do_not_starve_valid_candidate(self):
        sent = self.add('Earlier distinct Arduina', verdict=judged(8))
        self.send(sent)
        poisoned = [self.add('Poison candidate %d' % i, verdict=judged(9))
                    for i in range(6)]
        valid = self.add('Valid distinct Quokka', verdict=judged(8))
        editor = FakeEditor()
        calls = []

        def check(item, verdict, recent):
            calls.append(item['nid'])
            if item['nid'] in poisoned:
                raise LLMError('model returned invalid repeat check JSON')
            return None

        cfg = dict(CFG, llm_repeat_checks_per_cycle=6, llm_item_max_failures=2)
        with patch.object(editor, 'same_story', side_effect=check):
            for _ in range(3):
                pipeline.check_repeats(self.store, [], cfg, editor)
        self.assertEqual(pipeline.repeat_state(self.store, valid, self.store.last_send_id()),
                         ('clear', None))
        for nid in poisoned:
            self.assertLessEqual(calls.count(nid), 2)
            self.assertIsNone(pipeline.repeat_state(self.store, nid, self.store.last_send_id()))

    def test_already_judged_items_do_not_take_practical_slots(self):
        for i in range(3):
            self.add('OpenAI Claude launches new model %d' % i,
                     source='practical', verdict=judged(8))
        nid = self.add('Useful small tool', source='practical')
        cfg = dict(CFG, llm_min_heuristic=4, practical_llm_slots=3,
                   practical_min_heuristic=2.5, llm_delay_seconds=0)
        sources = [{'name': 'practical', 'weight': 1.1, 'editorial_lane': 'practical'}]
        editor = FakeJudge()
        result = pipeline.rerank_pending(self.store, sources, cfg, editor)
        self.assertGreaterEqual(self.store.item(nid)['score'], 2.5)
        self.assertLess(self.store.item(nid)['score'], 4)
        self.assertIsNotNone(pipeline.get_verdict(self.store, nid))
        self.assertEqual(result['llm_calls'], 1)

    def test_exhausted_item_failures_do_not_take_practical_slots(self):
        for i in range(3):
            nid = self.add('OpenAI Claude launches new model %d' % i, source='practical')
            for _ in range(2):
                pipeline.note_judge_failure(self.store, nid, 'model returned invalid JSON')
        nid = self.add('Useful small tool', source='practical')
        cfg = dict(CFG, llm_min_heuristic=4, practical_llm_slots=3,
                   practical_min_heuristic=2.5, llm_item_max_failures=2, llm_delay_seconds=0)
        sources = [{'name': 'practical', 'weight': 1.1, 'editorial_lane': 'practical'}]
        result = pipeline.rerank_pending(self.store, sources, cfg, FakeJudge())
        self.assertIsNotNone(pipeline.get_verdict(self.store, nid))
        self.assertEqual(result['llm_calls'], 1)

    def test_flat_interest_cannot_refresh_same_growth_twice(self):
        source = {'type': 'hn_front_page'}
        item = {'uid': 'hn:flat', 'source': 'hn', 'title': 'Old discussion',
                'published': iso(50), 'extra': {'points': 10}}
        self.store.observed_item(item, source, now=iso(7))
        nid = self.store.add_item(item)
        self.store.mark(nid, 'stale')
        item['extra'] = {'points': 30}
        self.store.observed_item(item, source, now=iso(6))
        first = self.store.item(nid)['extra']['freshness_at']
        self.store.mark(nid, 'stale')
        item['extra'] = {'points': 30}
        self.store.observed_item(item, source)
        self.assertEqual(self.store.item(nid)['extra']['freshness_at'], first)
        self.assertEqual(self.status(nid), 'stale')

    def test_fresh_growth_survives_expiration_but_remains_finite(self):
        source = {'type': 'hn_front_page'}
        item = {'uid': 'hn:ttl', 'source': 'hn', 'title': 'Old discussion',
                'published': iso(10), 'extra': {'points': 10}}
        self.store.observed_item(item, source, now=iso(7))
        nid = self.store.add_item(item)
        self.store.db.execute('UPDATE items SET discovered=? WHERE nid=?', (iso(7), nid))
        self.store.db.commit()
        item['extra'] = {'points': 30}
        self.store.observed_item(item, source)
        self.assertFalse(pipeline.too_old_to_send(self.store.item(nid), CFG))
        self.store.expire_pending(6)
        self.assertEqual(self.status(nid), 'pending')
        extra = self.store.item(nid)['extra']
        self.assertEqual(extra['max_age_hours'], 48)
        extra['freshness_at'] = iso(47)
        self.store.db.execute('UPDATE items SET extra=?,discovered=? WHERE nid=?',
                              (json.dumps(extra), iso(49), nid))
        self.store.db.commit()
        self.store.expire_pending(6)
        self.assertEqual(self.status(nid), 'pending')
        extra['freshness_at'] = iso(49)
        self.store.db.execute('UPDATE items SET extra=? WHERE nid=?',
                              (json.dumps(extra), nid))
        self.store.db.commit()
        self.store.expire_pending(6)
        self.assertEqual(self.status(nid), 'expired')


class RepeatResponseValidation(unittest.TestCase):
    def test_only_null_or_valid_integer_is_a_repeat_answer(self):
        editor = DeepSeek(api_key='test-key')
        item, recent = {'title': 'new'}, [{'title': 'old'}]
        for invalid in ('garbage', -1, 0, 999, True, 1.5, [], {}):
            with self.subTest(value=invalid):
                answer = json.dumps({'duplicate_of': invalid})
                with patch.object(editor, '_chat', return_value=(answer, 0, 0)):
                    with self.assertRaises(LLMError):
                        editor.same_story(item, {}, recent)
        for valid, expected in ((None, None), (1, 0)):
            with self.subTest(value=valid):
                answer = json.dumps({'duplicate_of': valid})
                with patch.object(editor, '_chat', return_value=(answer, 0, 0)):
                    self.assertEqual(editor.same_story(item, {}, recent), expected)


class TricklingResponseBudget(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond(200, b'{"ok":true}' if self.path == '/json' else b'x' * 16)

            def do_POST(self):
                self.respond(400)

            def respond(self, status, body=b'x' * 16):
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def test_body_read_has_cumulative_deadline(self):
        started = time.monotonic()
        with http.request_budget(0.15):
            result = http.fetch(self.url, retries=0)
        self.assertFalse(result.ok)
        self.assertLess(time.monotonic() - started, 0.35)

    def test_curl_fallback_obeys_fractional_deadline(self):
        started = time.monotonic()
        with http.request_budget(0.15):
            data, error = feeds._curl_json(self.url + '/json', {})
        self.assertIsNone(data)
        self.assertTrue(error)
        self.assertLess(time.monotonic() - started, 0.35)

    def test_model_error_body_has_cumulative_deadline(self):
        editor = DeepSeek(api_key='test-key', base_url=self.url)
        editor._resolved = 'test-model'
        started = time.monotonic()
        with http.request_budget(0.15):
            with self.assertRaises((LLMError, TimeoutError)):
                editor._chat([{'role': 'user', 'content': 'test'}])
        self.assertLess(time.monotonic() - started, 0.35)


if __name__ == '__main__':
    unittest.main()
