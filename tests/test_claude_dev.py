"""Official claude.dev RSS through the real collector and isolated SQLite state."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from newsbot import feeds, http, pipeline
from newsbot.store import Store, canonical_url

ROOT = Path(__file__).resolve().parents[1]
# Public feed metadata captured on 2026-10-16; only the two newest entries retained.
FEED = (ROOT / 'tests/fixtures/claude-dev-rss.xml').read_bytes()
ARTICLE = 'https://claude.dev/blog/solving-code-review-at-anthropic/'
SOURCE = next(s for s in json.loads((ROOT / 'sources.json').read_text())['sources']
              if s['name'] == 'claude_dev')
CFG = dict(json.loads((ROOT / 'config.json').read_text()), politeness_delay_seconds=0)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        stamp = cls(2026, 10, 16, 11, tzinfo=timezone.utc)
        return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)


class ClaudeDevTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / 'news.db'))
        self.clock = patch.object(pipeline, 'datetime', Clock)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def collect(self, result):
        with patch.object(feeds, 'fetch', return_value=result) as request:
            stats = pipeline.collect(self.store, [SOURCE], CFG)
        return stats, request

    def test_real_article_title_date_canonical_and_context(self):
        with patch.object(feeds, 'fetch', return_value=http.FetchResult(200, FEED, {})):
            items, error, _ = pipeline.fetch_source(SOURCE)
        self.assertIsNone(error)
        item = next(i for i in items if i['url'] == ARTICLE)
        self.assertEqual(item['uid'], ARTICLE)
        self.assertEqual(item['title'], 'How the Claude Code team does code review')
        self.assertEqual(item['published'], '2026-10-15T12:00:00+00:00')
        self.assertIn('minimum amount of human review time', item['summary'])

    def test_archive_filtered_and_repeat_does_not_refresh_or_duplicate(self):
        first, _ = self.collect(http.FetchResult(200, FEED, {}))
        self.assertEqual((first['fetched'], first['new'], first['old']), (2, 1, 1))
        before = self.store.pending()[0]
        self.assertEqual(before['extra']['max_age_hours'], 48)
        self.assertNotIn('freshness_at', before['extra'])
        repeat, _ = self.collect(http.FetchResult(200, FEED, {}))
        self.assertEqual((repeat['new'], repeat['seen'], repeat['old']), (0, 1, 1))
        after = self.store.pending()[0]
        self.assertEqual((after['published'], after['discovered']),
                         (before['published'], before['discovered']))
        self.assertFalse(pipeline.too_old_to_send(before, CFG))
        class ExpiredClock(Clock):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 17, 13, tzinfo=timezone.utc)

        self.store.set_score(before['nid'], 10)
        with patch.object(pipeline, 'datetime', ExpiredClock):
            self.assertTrue(pipeline.too_old_to_send(before, CFG))
            self.assertEqual(pipeline.select_due(self.store, [SOURCE], CFG), [])
        self.assertEqual(self.store.item(before['nid'])['status'], 'stale')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sends').fetchone()[0], 0)

    def test_existing_canonical_from_another_source_blocks_changed_guid(self):
        item = feeds.parse_feed(FEED)[0]
        item.update(uid='other-feed-guid', source='another_source',
                    url=ARTICLE + '?utm_source=another_feed')
        self.store.add_item(item)
        stats, _ = self.collect(http.FetchResult(200, FEED, {}))
        self.assertEqual((stats['new'], stats['seen']), (0, 1))
        self.assertEqual(len(self.store.pending()), 1)

    def test_tracking_removed_but_article_identity_query_preserved(self):
        self.assertEqual(canonical_url(ARTICLE + '?utm_source=rss&utm_medium=feed&id=42'),
                         ARTICLE.rstrip('/') + '?id=42')
        self.assertNotEqual(canonical_url(ARTICLE + '?id=42'),
                            canonical_url(ARTICLE + '?id=43'))

    def test_conditional_read_uses_etag_and_304_preserves_state(self):
        self.collect(http.FetchResult(200, FEED, {'ETag': 'fixture-etag'}))
        before = self.store.pending()
        stats, request = self.collect(http.FetchResult(304, b'', {}))
        self.assertEqual(request.call_args.kwargs['etag'], 'fixture-etag')
        self.assertEqual((stats['new'], stats['skipped_unchanged']), (0, 1))
        self.assertEqual(self.store.pending(), before)
        self.assertEqual(self.store.source_state('claude_dev')['etag'], 'fixture-etag')

    def test_http_error_and_html_leave_queue_intact_then_empty_feed_recovers(self):
        self.collect(http.FetchResult(200, FEED, {}))
        before = self.store.pending()
        for result in (http.FetchResult(503, b'', {}, error='HTTP 503'),
                       http.FetchResult(200, b'<html>Unavailable</html>', {})):
            with self.subTest(status=result.status):
                stats, _ = self.collect(result)
                self.assertEqual((stats['failed'], stats['new']), (1, 0))
                self.assertTrue(self.store.source_state('claude_dev')['last_error'])
                self.assertEqual(self.store.pending(), before)
                # Advance past the collector's retry delay without sleeping.
                self.store.db.execute('UPDATE sources SET retry_at=NULL')
                self.store.db.commit()
        stats, _ = self.collect(http.FetchResult(200, b'<rss><channel/></rss>', {}))
        self.assertEqual((stats['ok'], stats['fetched'], stats['new']), (1, 0, 0))
        self.assertIsNone(self.store.source_state('claude_dev')['last_error'])
        self.assertEqual(self.store.pending(), before)
