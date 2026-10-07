import copy
import json
import unittest
from pathlib import Path
from newsbot import x_html

FIXTURES=json.loads((Path(__file__).parent/'fixtures/x-profile.json').read_text())


def page(entries):
    # Both quoted JSON keys and unquoted JS data keys are supported. The
    # TimelineTweet discriminator matches the publicly observed serialization.
    return ''.join('content:$R[%d]=%s;'%(n,json.dumps(e).replace('{"__isTimelineTimelineItemContent": "TimelineTweet"','{__isTimelineTimelineItemContent:"TimelineTweet"',1)) for n,e in enumerate(entries))


class ProfileTest(unittest.TestCase):
    def test_fixture_parents_preserve_authors_dates_and_dedupe_ids(self):
        for handle in ['karpathy','HamelHusain','mattpocockuk']:
            entries=[e for e in FIXTURES if e['profile_handle']==handle]
            items=x_html.parse_profile(page(entries+entries),handle,limit=10)
            self.assertEqual(len(items),5)
            for item in items:
                self.assertEqual(item['extra']['author'].lower(),handle.lower())
                self.assertIn('/%s/status/'%handle,item['url'])
                self.assertTrue(item['published'])
            self.assertEqual(items,sorted(items,key=lambda i:i['published'],reverse=True))

    def test_full_note_text_and_parent_timestamp_win_over_quote(self):
        entry=copy.deepcopy(FIXTURES[0]);tweet=entry['tweet_results']['result']
        tweet['note_tweet']={'note_tweet_results':{'result':{'text':'LONG AUTHOR POST '+('useful '*100)}}}
        item=x_html.parse_profile(page([entry]),'karpathy')[0]
        self.assertIn('LONG AUTHOR',item['summary'])
        self.assertEqual(item['published'],'2026-10-02T06:35:51+00:00')
        self.assertEqual(item['uid'],'x:2105909609487872075')
        self.assertTrue(item['extra']['has_quote'])

    def test_long_summary_explicitly_marks_truncation(self):
        from newsbot import render
        entry=copy.deepcopy(FIXTURES[0])
        entry['tweet_results']['result']['note_tweet']={'note_tweet_results':{'result':{'text':'word '*400}}}
        item=x_html.parse_profile(page([entry]),'karpathy')[0]
        self.assertTrue(item['extra']['summary_truncated'])
        self.assertEqual(len(item['summary']),1200)
        self.assertIn('описание сокращено',render.item_card(item))

    def test_other_author_and_retweet_do_not_masquerade_as_owner(self):
        entry=copy.deepcopy(FIXTURES[0]);entry['tweet_results']['result']['core']['user_results']['result']['core']['screen_name']='other'
        with self.assertRaises(ValueError): x_html.parse_profile(page([entry]),'karpathy')
        entry=copy.deepcopy(FIXTURES[0]);entry['tweet_results']['result']['legacy']['retweeted_status_result']={'result':{'rest_id':'123456'}}
        with self.assertRaises(ValueError): x_html.parse_profile(page([entry]),'karpathy')

    def test_legacy_and_visibility_wrapper_and_cross_references(self):
        entry=copy.deepcopy(FIXTURES[0]);tweet=entry['tweet_results']['result']
        tweet['core']['user_results']['result']={'legacy':{'screen_name':'karpathy'}}
        tweet['legacy'].update(full_text='Legacy text',created_at='Fri Oct 02 06:35:51 +0000 2026');tweet.pop('details')
        entry['tweet_results']['result']={'__typename':'TweetWithVisibilityResults','tweet':tweet}
        item=x_html.parse_profile(page([entry]),'karpathy')[0]
        self.assertEqual(item['summary'],'Legacy text')
        raw=page([entry]).replace('"user_results": {"result": {"legacy": {"screen_name": "karpathy"}}}', '"user_results": {"result": $R[99]}')+'$R[99]={legacy:{screen_name:"karpathy"}};'
        self.assertEqual(x_html.parse_profile(raw,'karpathy')[0]['uid'],item['uid'])

    def test_shared_references_have_an_expansion_budget(self):
        reader=x_html.DataReader('')
        reader.refs[0]={'text':'leaf'}
        for n in range(1,22):
            reader.refs[n]=[x_html.Reference(n-1),x_html.Reference(n-1)]
        with self.assertRaises(ValueError): reader.resolve(x_html.Reference(21))
        self.assertLessEqual(reader.resolved_nodes,200001)

    def test_changed_or_login_markup_and_bad_dates_fail_visibly(self):
        for text in ['<html>Sign in</html>','content:$R[1]={__isTimelineTimelineItemContent:"TimelineTweet",danger:runCode()};']:
            with self.assertRaises(ValueError): x_html.parse_profile(text,'karpathy')
        entry=copy.deepcopy(FIXTURES[0]);entry['tweet_results']['result']['details']['created_at_ms']='not a date'
        with self.assertRaises(ValueError): x_html.parse_profile(page([entry]),'karpathy')
        with self.assertRaises(ValueError):x_html.parse_profile('x'*(3*1024*1024+1),'karpathy')
