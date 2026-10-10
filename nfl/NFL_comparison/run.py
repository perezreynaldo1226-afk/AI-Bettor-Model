import argparse
import json
from pathlib import Path
from comparison.research import run
from comparison.report import generate
from comparison.core import append_record

ROOT=Path(__file__).resolve().parent
p=argparse.ArgumentParser()
p.add_argument('command',choices=['research','ingest','decide','serve'])
p.add_argument('--record',type=Path)
p.add_argument('--db',type=Path,default=ROOT/'prospective.sqlite3')
p.add_argument('--port',type=int,default=8080)
a=p.parse_args()
if a.command=='research':
    wide=run(ROOT/'data/features.csv',ROOT/'data/winner_manifest.json',ROOT/'data/history.csv',ROOT/'results')
    report=generate(wide,ROOT/'results',json.loads((ROOT/'registry.json').read_text()))
    template=(ROOT/'dashboard_template.html').read_text()
    # Escape script terminators; external text cannot escape the JSON script.
    content=json.dumps(report).replace('<','\\u003c')
    (ROOT/'dashboard.html').write_text(template.replace('__REPORT_DATA__',content))
    print(json.dumps([{'model':r['model'],'games':r['games'],'log_loss':r['log_loss'],'accuracy':r['accuracy']} for r in report['scoreboard']],indent=2))
elif a.command=='ingest':
    if not a.record: p.error('--record required')
    print(append_record(a.db,json.loads(a.record.read_text())))
elif a.command=='decide':
    if not a.record: p.error('--record required')
    from comparison.core import decide
    r=json.loads(a.record.read_text())
    rule=json.loads((ROOT/'rule.json').read_text())
    if r.get('eligibility')!='ELIGIBLE':
        print(json.dumps({'action':'PASS','reason':'Eligibility missing or not eligible'}))
    else:
        members=[r.get('probabilities',{}).get(m) for m in rule['members']]
        print(json.dumps(decide(members,r.get('home_odds'),r.get('away_odds'),
            r.get('odds_quote_timestamp_utc'),r['deadline_utc'],rule['min_ev'],rule['max_age_minutes'])))
else:
    import functools
    from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
    print(f'Open http://127.0.0.1:{a.port}/dashboard.html')
    ThreadingHTTPServer(('127.0.0.1',a.port),functools.partial(SimpleHTTPRequestHandler,directory=str(ROOT))).serve_forever()
