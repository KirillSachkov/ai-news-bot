"""Replay source collection on a private state snapshot; no network/model/Telegram."""
import json,sys,sqlite3,tempfile,copy,collections
from pathlib import Path
# Arguments: checkout, baseline.db, probe.jsonl, evaluation ISO timestamp.
sys.path.insert(0,sys.argv[1])
from newsbot import pipeline
from newsbot.store import Store
from bot import load_json
cfg=load_json(str(Path(sys.argv[1])/'config.json'))
sources=load_json(str(Path(sys.argv[1])/'sources.json'))['sources']
for key in ['politeness_delay_seconds','x_politeness_delay_seconds']:cfg[key]=0
fixtures={r['name']:r for r in [json.loads(l) for l in open(sys.argv[3])]}
from datetime import datetime, timezone
class Frozen(datetime):
 @classmethod
 def now(cls,tz=None):
  value=datetime.fromisoformat(sys.argv[4]).astimezone(timezone.utc)
  return value.astimezone(tz) if tz else value.replace(tzinfo=None)
from newsbot import store as store_module, score
pipeline.datetime=store_module.datetime=score.datetime=Frozen
with tempfile.TemporaryDirectory() as path:
 db=str(Path(path)/'replay.db')
 a=sqlite3.connect('file:'+sys.argv[2]+'?mode=ro',uri=True);b=sqlite3.connect(db);a.backup(b);a.close();b.close()
 store=Store(db)
 before=store.db.execute('SELECT MAX(nid) n FROM items').fetchone()['n']
 def fetch(source,**kwargs):
  r=fixtures.get(source['name'])
  if r is None: raise ValueError('missing source fixture: '+source['name'])
  return (copy.deepcopy(r.get('items',[])),r.get('error'),{})
 pipeline.fetch_source=fetch
 stats=pipeline.collect(store,sources,cfg)
 added=[dict(r) for r in store.db.execute('SELECT source,title,url,published FROM items WHERE nid>?',(before,))]
 counts=dict(collections.Counter(r['source'] for r in added))
 result={'version':sys.argv[1],'evaluated_at':sys.argv[4],'stats':stats,'added_by_source':counts,'added':added,'model_calls':0,'telegram_calls':0}
 print(json.dumps(result,ensure_ascii=False))
 store.close()
