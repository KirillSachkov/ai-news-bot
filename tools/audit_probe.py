"""Public source probe: python3 tools/audit_probe.py DISPOSABLE.db > private.jsonl.
Includes full public item bodies for offline replay; keep output out of git.
Each source runs in a child with a 50-second process deadline.
"""
import sys, json, time, subprocess
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from newsbot import pipeline, x_sources
from newsbot.store import Store
# Run against a disposable database, never the live state.
if len(sys.argv)<2: raise SystemExit('usage: audit_probe.py DISPOSABLE.db')
DB=str(Path(sys.argv[1]).resolve())
if len(sys.argv)>2:
    source=json.loads(sys.argv[2]); store=Store(DB); x_sources.set_store(store)
    started=time.monotonic(); items,error,validators=pipeline.fetch_source(source)
    ages=[pipeline.item_age_hours(i) for i in items]
    result={'name':source['name'],'type':source['type'],'enabled':source.get('enabled',True),'seconds':round(time.monotonic()-started,2),'count':len(items),'newest':max((i.get('published') or '' for i in items),default=''),'undated':sum(a is None for a in ages),'last6h':sum(a is not None and 0<=a<=6 for a in ages),'last24h':sum(a is not None and 0<=a<=24 for a in ages),'unseen6h':sum(pipeline.is_fresh_enough(i,{'lookback_hours':6}) and not store.exists(i['uid']) for i in items),'error':error,'samples':items[:2],'items':items}
    print(json.dumps(result,ensure_ascii=False)); sys.exit()
for source in json.loads((ROOT/'sources.json').read_text())['sources']:
    started=time.monotonic()
    try:
        r=subprocess.run([sys.executable,__file__,DB,json.dumps(source)],capture_output=True,text=True,timeout=50)
        result=json.loads(r.stdout) if r.returncode==0 else {'name':source['name'],'error':r.stderr[-500:]}
    except subprocess.TimeoutExpired:
        result={'name':source['name'],'error':'probe hard timeout 50s','seconds':50}
    print(json.dumps(result,ensure_ascii=False),flush=True)
