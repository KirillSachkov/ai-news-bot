"""Functional audit on isolated SQLite state and a fake Telegram transport."""
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import threading
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot
from newsbot import feeds, http, pipeline, render
from newsbot.llm import DeepSeek, LLMError
from newsbot.runner import Runner
from newsbot.store import Store
from test_editorial import CFG, TAG, FakeEditor, FakeJudge, StoreCase, judged, iso


class Transport:
    def __init__(self):
        self.messages, self.answers, self.edits = [], [], []
        self.updates = []
        self.fail = False

    def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError('transport unavailable')
        self.messages.append((chat_id, text, kw))
        return {'message_id': len(self.messages)}

    def answer_callback(self, cid, text):
        self.answers.append(text)

    def edit_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))

    def get_updates(self, **kwargs):
        return self.updates


class ParsingTest(unittest.TestCase):
    def test_dates_and_empty_feed_are_distinguished_from_html(self):
        self.assertEqual(feeds.to_iso('Tue, 06 Oct 2026 12:00:00 +0300'), '2026-10-06T09:00:00+00:00')
        self.assertIsNone(feeds.to_iso('yesterday'))
        self.assertEqual(feeds.parse_feed('<rss><channel/></rss>'), [])
        for body in ['<html><body>Blocked</body></html>', '<error/>', 'no feed']:
            with self.assertRaises(ValueError):
                feeds.parse_feed(body)

    def test_atom_xhtml_and_newest_items_before_limit(self):
        body = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>old</id><title>Old</title><updated>2026-10-01T00:00:00Z</updated></entry><entry><id>new</id><title>New</title><updated>2026-10-07T00:00:00Z</updated><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Try coding agents</p></div></content><link href="https://example.com/new"/></entry></feed>'
        with patch.object(feeds, 'fetch', return_value=http.FetchResult(200, body, {})):
            items, error, _ = feeds.rss('https://example.com', limit=1)
        self.assertIsNone(error)
        self.assertEqual(items[0]['uid'], 'new')
        self.assertIn('coding agents', items[0]['summary'])

    def test_telegram_keeps_latest_posts_not_first_posts(self):
        body = ''.join('<div class="tgme_widget_message " data-post="test/%d"><div class="tgme_widget_message_text">Message %d</div><div class="tgme_widget_message_footer"><time datetime="2026-10-07T%02d:00:00Z"></time></div></div>' % (i,i,i) for i in range(1,6))+'<section></section></section>'
        with patch.object(feeds, 'fetch', return_value=http.FetchResult(200, body.encode(), {})):
            items, error = feeds.telegram_web('test', limit=2)
        self.assertIsNone(error)
        self.assertEqual([r['uid'] for r in items], ['tg:test/5','tg:test/4'])

    def test_hn_body_is_available_to_editor(self):
        payload={'hits':[{'objectID':'1','title':'Show HN: Small useful app','story_text':'<p>Sandbox coding agents locally</p>','created_at':iso()}]}
        with patch.object(feeds, 'fetch_json', return_value=(payload,None)):
            items, _ = feeds.hn_show()
        self.assertIn('Sandbox', items[0]['summary'])

    def test_http_budget_is_nested_and_restored(self):
        with http.request_budget(0):
            with self.assertRaises(TimeoutError):
                http.remaining_timeout(25)
        self.assertEqual(http.remaining_timeout(25),25)
        with http.request_budget(10):
            with http.request_budget(100):
                self.assertLessEqual(http.remaining_timeout(100),10)


class AuditPipelineTest(StoreCase):
    def test_future_and_invalid_dates_cannot_be_delivered(self):
        nid=self.add('Future release', hours_ago=-48, verdict=judged(9))
        invalid=self.add('Broken date', verdict=judged(9))
        self.store.db.execute('UPDATE items SET published=? WHERE nid=?',('garbage',invalid)); self.store.db.commit()
        self.assertEqual(self.select(),[])
        self.assertEqual(self.status(nid),'stale')
        self.assertLess(pipeline.base_score(self.store.item(invalid),self.store,CFG),7)

    def test_stale_unjudged_rows_leave_queue_without_model_calls(self):
        nid=self.add('OpenAI release',hours_ago=7)
        judge=FakeJudge()
        result=pipeline.rerank_pending(self.store,[],dict(CFG,llm_min_heuristic=-100),judge)
        self.assertEqual(result['llm_calls'],0)
        self.assertEqual(self.status(nid),'stale')

    def test_item_errors_are_bounded_by_attempt_allowance(self):
        for i in range(8): self.add('OpenAI release poisoned %d'%i)
        judge=FakeJudge(error='model returned unparseable JSON')
        result=pipeline.rerank_pending(self.store,[],dict(CFG,llm_min_heuristic=-100,llm_max_per_cycle=2,llm_delay_seconds=0),judge)
        self.assertEqual(sum(judge.judged.values()),2)
        self.assertEqual(result['llm_calls'],2)
        self.assertFalse(pipeline.is_item_fault('HTTP 401 unauthorized'))

    def test_practical_slot_reaches_editor_without_increasing_allowance(self):
        practical=self.add('Show HN: Small developer tool', source='hn_show')
        for i in range(4): self.add('OpenAI Anthropic DeepSeek launches model %d'%i)
        sources=[{'name':'hn_show','editorial_lane':'practical','weight':1.1}]
        judge=FakeJudge()
        result=pipeline.rerank_pending(self.store,sources,dict(CFG,llm_min_heuristic=8,llm_max_per_cycle=2,practical_min_heuristic=2.5,practical_llm_slots=1,llm_delay_seconds=0),judge)
        self.assertIsNotNone(pipeline.get_verdict(self.store,practical))
        self.assertEqual(result['llm_calls'],2)

    def test_failed_repeat_check_remains_unknown_and_retries(self):
        old=self.add('Earlier Zorb',verdict=judged(8));self.send(old)
        new=self.add('New distinct Quokka',verdict=judged(8))
        judge=FakeEditor()
        with patch.object(judge,'same_story',side_effect=RuntimeError('timeout')):
            pipeline.check_repeats(self.store,[],CFG,judge)
        self.assertIsNone(pipeline.repeat_state(self.store,new,self.store.last_send_id()))
        self.assertEqual(self.select(),[])
        pipeline.check_repeats(self.store,[],CFG,judge)
        self.assertEqual(self.chosen_nids(),[new])

    def test_network_repeat_failures_allow_other_candidates_next_cycle(self):
        old=self.add('Earlier Zorb',verdict=judged(8));self.send(old)
        ids=[self.add('Distinct candidate '+str(i),verdict=judged(8)) for i in range(3)]
        judge=FakeEditor()
        calls=[]
        def fail(row,*args):
            calls.append(row['nid'])
            raise RuntimeError('network timeout')
        with patch.object(judge,'same_story',side_effect=fail):
            pipeline.check_repeats(self.store,[],CFG,judge,limit=2)
            pipeline.check_repeats(self.store,[],CFG,judge,limit=2)
        self.assertEqual(set(calls),set(ids))
        self.assertTrue(all(pipeline.repeat_state(self.store,n,self.store.last_send_id()) is None for n in ids))
        self.assertEqual(self.store.db.execute('SELECT SUM(failures) FROM repeat_failures').fetchone()[0],0)

    def test_discovery_preserves_real_date_and_does_not_reset_on_poll(self):
        item={'uid':'hf:old','title':'Mature model','source':'hf','url':'https://hf.co/old','published':iso(500),'extra':{'points':50,'discovery_kind':'model'}}
        source={'type':'hf_trending_models','freshness_hours':48}
        self.store.observed_item(item,source)
        self.assertTrue(pipeline.is_fresh_enough(item,CFG))
        nid=self.store.add_item(item)
        first=item['extra']['freshness_at']
        self.store.observed_item(item,source,now=iso(-.5))
        self.assertEqual(item['extra']['freshness_at'],first)
        self.assertEqual(self.store.item(nid)['published'],iso(500))
        self.assertFalse(pipeline.is_breaking_now(9,9,item,CFG,{}, {},0,judged(9)))

    def test_growth_requires_two_timed_observations_and_preserves_sent_feedback(self):
        source={'type':'hn_front_page'}
        item={'uid':'hn:growth','source':'hn','title':'Old discussion','published':iso(12),'extra':{'points':10}}
        self.store.observed_item(item,source,now=iso(2))
        self.assertNotIn('growth_delta',item['extra'])
        nid=self.store.add_item(item)
        self.store.mark(nid,'expired')
        item['extra']={'points':30}
        self.store.observed_item(item,source)
        self.assertEqual(item['extra']['growth_delta'],20)
        self.assertEqual(item['extra']['max_age_hours'],48)
        self.assertEqual(self.status(nid),'pending')
        self.assertTrue(pipeline.is_fresh_enough(item,CFG))
        self.send(nid);self.store.set_feedback(nid,'good')
        item['extra']={'points':100}
        self.store.observed_item(item,source,now=iso(-8))
        self.assertEqual(self.status(nid),'sent')
        self.assertEqual(self.store.item(nid)['feedback'],'good')

    def test_practical_ttl_is_finite_and_news_ttl_unchanged(self):
        nid=self.add('Practical old but useful',hours_ago=20,verdict=judged(8))
        self.store.db.execute('UPDATE items SET extra=?,discovered=? WHERE nid=?',(json.dumps({'max_age_hours':48}),iso(20),nid));self.store.db.commit()
        self.store.expire_pending(6)
        self.assertEqual(self.status(nid),'pending')
        self.assertIn(nid,self.chosen_nids())
        self.store.db.execute('UPDATE items SET discovered=? WHERE nid=?',(iso(49),nid));self.store.db.commit()
        self.store.expire_pending(6)
        self.assertEqual(self.status(nid),'expired')

    def test_source_exception_isolated_and_cooldown_observed(self):
        sources=[{'name':'bad','type':'rss'}, {'name':'quiet','type':'rss'}]
        with patch.object(pipeline,'fetch_source',side_effect=[RuntimeError('boom'),([],None,{})]):
            stats=pipeline.collect(self.store,sources,dict(CFG,politeness_delay_seconds=0))
        self.assertEqual((stats['failed'],stats['ok']),(1,1))
        self.assertTrue(self.store.source_state('bad')['retry_at'])
        with patch.object(pipeline,'fetch_source',return_value=([],None,{})) as fetch:
            stats=pipeline.collect(self.store,sources,dict(CFG,politeness_delay_seconds=0))
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(stats['cooldown'],1)
        self.assertIsNone(self.store.source_state('quiet')['last_error'])

    def test_conditional_source_preserves_validators(self):
        source={'name':'rss','type':'rss'}
        self.store.touch_source_ok('rss',etag='etag1')
        with patch.object(pipeline,'fetch_source',return_value=([],None,{'not_modified':True})) as fetch:
            stats=pipeline.collect(self.store,[source],dict(CFG,politeness_delay_seconds=0))
        self.assertEqual(fetch.call_args.kwargs['etag'],'etag1')
        self.assertEqual(stats['skipped_unchanged'],1)
        self.assertEqual(self.store.source_state('rss')['etag'],'etag1')

    def test_delivery_failure_does_not_mark_sent_and_success_survives_restart(self):
        nid=self.add('Useful release',verdict=judged(8)); tg=Transport();tg.fail=True
        chosen=self.select()
        self.assertEqual(pipeline.deliver(tg,self.store,1,chosen,dict(CFG,send_delay_seconds=0)),0)
        self.assertEqual(self.status(nid),'pending')
        tg.fail=False
        self.assertEqual(pipeline.deliver(tg,self.store,1,chosen,dict(CFG,send_delay_seconds=0)),1)
        with_store=Store(self.store.path)
        self.assertEqual(with_store.item(nid)['status'],'sent');with_store.close()
        self.assertFalse(self.store.kv_get('delivery_error'))

    def test_normal_gap_and_hour_budget_survive_restart(self):
        old=self.add('Old Arduina',verdict=judged(8));self.send(old)
        new=self.add('New Quokka',verdict=judged(8))
        pipeline.check_repeats(self.store,[],CFG,FakeEditor())
        cfg=dict(CFG,max_per_hour=1,min_gap_minutes=40)
        self.assertEqual(self.select(cfg=cfg),[])
        self.assertEqual(self.store.item(new)['status'],'pending')

    def test_dry_selection_preserves_statuses_and_does_not_call_editor(self):
        nid=self.add('Old stale',hours_ago=20,verdict=judged(8))
        self.assertEqual(self.select(dry=True),[])
        self.assertEqual(self.status(nid),'pending')


class CommandsTest(StoreCase):
    def setUp(self):
        super().setUp();self.tg=Transport();self.store.kv_set('chat_id',1);self.store.kv_set('bootstrap_done','done')
        self.runner=Runner(self.tg,self.store,[{'name':'fast','tier':'fast','enabled':True},{'name':'slow'}],dict(CFG,learning=True),verbose=False)

    def command(self,text,chat=1): self.runner.handle_message({'chat':{'id':chat},'text':text})

    def test_status_sources_last_help_and_control_commands(self):
        for command in ['/status','/sources','/last','/help']: self.command(command)
        self.assertGreaterEqual(len(self.tg.messages),4)
        self.command('/pause'); self.assertTrue(self.runner.is_paused())
        self.command('/resume'); self.assertFalse(self.runner.is_paused())
        self.command('/mute 2'); self.assertTrue(self.runner.is_paused())
        self.command('/resume'); self.assertFalse(self.runner.is_paused())
        self.command('/mute inf'); self.assertTrue(self.runner.is_paused())
        self.assertEqual(len(self.runner.fast_sources()),1)

    def test_foreign_commands_and_callback_do_not_change_state(self):
        self.command('/pause',chat=2)
        self.assertFalse(self.runner.is_paused())
        self.runner.handle_callback({'id':'c','data':'fb|g|1'})
        self.assertEqual(self.store.counts()['feedback'],{})

    def test_digest_is_async_and_pause_responds_during_collection(self):
        with patch.object(self.runner,'fetch_now',side_effect=AssertionError('blocking fetch')):
            self.command('/digest');self.command('/pause');self.command('/digest')
        self.assertTrue(self.runner._digest_requested.is_set())
        self.assertTrue(self.runner.is_paused())
        self.assertIn('уже запрошен',self.tg.messages[-1][1])
        self.runner._results.put(('digest',{'new':0}))
        self.runner.finish_collections()
        self.assertFalse(self.runner._digest_busy)
        self.assertIn('на паузе',self.tg.messages[-1][1])

    def test_last_links_and_feedback_are_idempotent(self):
        nid=self.add('OpenAI launches something',verdict=judged(8));self.send(nid)
        self.command('/last'); self.assertIn('Источник',json.dumps(self.tg.messages[-1],ensure_ascii=False))
        callback={'id':'x','data':'fb|g|%s'%nid,'message':{'message_id':7,'chat':{'id':1},'text':'A & B'}}
        self.runner.handle_callback(callback);self.runner.handle_callback(callback)
        self.assertEqual(self.store.counts()['feedback'],{'good':1})
        self.assertEqual(self.store.weights()['source']['techcrunch_ai'][1],1)
        self.assertEqual(len(self.tg.edits),1)
        self.assertIn('&amp;',self.tg.edits[0][0][2])
        self.assertIn('Источник',json.dumps(self.tg.edits[0],ensure_ascii=False))

    def test_learning_off_still_records_vote_without_weights(self):
        self.runner.cfg['learning']=False
        nid=self.add('Qwen release',verdict=judged(8));self.send(nid)
        self.runner.handle_callback({'id':'x','data':'fb|b|%s'%nid,'message':{'chat':{'id':1}}})
        self.assertEqual(self.store.counts()['feedback'],{'bad':1})
        self.assertEqual(self.store.weights(),{})

    def test_update_offset_persisted_even_when_command_fails(self):
        self.tg.updates=[{'update_id':44,'message':{'chat':{'id':1},'text':'/help'}}];self.tg.fail=True
        self.runner.poll_updates()
        self.assertEqual(self.store.kv_get('update_offset'),'45')

    def test_worker_handles_both_cadences_and_digest(self):
        self.runner._worker_store=self.store
        self.runner.next_fetch=0
        collected=[]
        def collect(store,sources,label,fast=False):
            collected.append((label,fast))
            self.runner._last_stats={'new':0}
            if len(collected)==1:
                self.runner.next_fast=0
            else:
                self.runner._stop.set()
            return {'new':0}
        with patch.object(self.runner,'_collect',side_effect=collect),patch.object(self.runner._wake,'wait'):
            self.runner._worker_loop()
        self.assertEqual(collected,[('full fetch',False),('fast lane',True)])


class CardSafetyTest(unittest.TestCase):
    def test_long_card_is_valid_html_and_fits_telegram(self):
        item={'title':'<&>'*2000,'summary':'<&>'*2000,'source':'test','url':'https://example.org','extra':{}}
        text=render.item_card(item,verdict={'reason':'<&>'*2000})
        self.assertLessEqual(len(text),4000)
        ET.fromstring('<root>'+text+'</root>')

    def test_large_source_list_splits_between_complete_lines(self):
        text='\n'.join('<i>source %d %s</i>'%(i,'x'*100) for i in range(100))
        parts=render.message_parts(text)
        self.assertGreater(len(parts),1)
        for part in parts:
            self.assertLessEqual(len(part),3900);ET.fromstring('<root>'+part+'</root>')

    def test_malformed_repeat_json_is_not_a_clear_result(self):
        llm=DeepSeek(api_key='fake')
        with patch.object(llm,'_chat',return_value=('garbage',10,5)):
            with self.assertRaises(LLMError): llm.same_story({'title':'new'}, {}, [{'title':'old'}])

    def test_dry_run_snapshots_before_build_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as path:
            db=os.path.join(path,'news.db');c=sqlite3.connect(db);c.execute('CREATE TABLE sentinel (x)');c.execute('INSERT INTO sentinel VALUES (1)');c.commit();c.close()
            before=Path(db).read_bytes()
            def fake_build(args,need_telegram=False):
                self.assertNotEqual(args.db,db);self.assertFalse(need_telegram)
                state=Store(args.db);tg=Transport();editor=FakeEditor()
                return [],CFG,state,tg,editor
            with patch.object(bot,'build',side_effect=fake_build),patch.object(bot.pipeline,'rerank_pending',return_value={'scanned':0,'judged':0,'llm_calls':0}),patch('sys.stdout',new=io.StringIO()):
                bot.main(['--db',db,'once','--dry-run','--offline','--no-fetch'])
            self.assertEqual(before,Path(db).read_bytes())


if __name__=='__main__': unittest.main()
