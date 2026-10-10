"""Normalize explicit external exports; never auto-activate a model."""
import argparse
import csv
import json
import math
from pathlib import Path
from comparison.core import utc,append_record


def normalize(r):
    required=['game_id','model_id','model_version','sport','market','probability_orientation',
              'probability','training_cutoff_utc','record_type']
    if any(not r.get(k) for k in required):
        raise ValueError('Required external metadata missing')
    if r['sport']!='NFL' or r['market']!='moneyline':
        raise ValueError('Only NFL moneyline supported')
    p=float(r['probability'])
    if not math.isfinite(p) or not 0<=p<=1:
        raise ValueError('Invalid probability')
    if r['probability_orientation']=='away_win':p=1-p
    elif r['probability_orientation']!='home_win':
        raise ValueError('Explicit home_win or away_win orientation required')
    out=dict(r,p_home_win=p)
    utc(r['training_cutoff_utc'])
    if r['record_type']=='RESEARCH_OOS':
        if not r.get('evaluation_start_utc'):
            raise ValueError('Research evaluation period start required')
        if utc(r['training_cutoff_utc'])>=utc(r['evaluation_start_utc']):
            raise ValueError('Training overlaps evaluation period')
    elif r['record_type']!='LIVE_PREGAME':
        raise ValueError('Unsupported evidence type')
    for field in ['odds_quote_timestamp_utc','odds_retrieval_timestamp_utc']:
        if not out.get(field):out[field]=None
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--db',type=Path)
    a=p.parse_args()
    records=[normalize(r) for r in csv.DictReader(a.input.open())]
    if any(r['record_type']=='LIVE_PREGAME' for r in records) and not a.db:
        p.error('LIVE_PREGAME imports require --db for chronology validation')
    for r in records:
        if r['record_type']=='LIVE_PREGAME':append_record(a.db,r)
    a.out.write_text(json.dumps(records,indent=2,allow_nan=False))
    print(f'Normalized {len(records)} records; model remains unactivated.')

if __name__=='__main__':main()
