"""Predict challenger probabilities from certified upcoming feature rows."""
import argparse
import hashlib
import json
from pathlib import Path
import pandas as pd
from comparison.core import utc
from comparison.research import MEMBERS,model


def main():
    root=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--features',type=Path,required=True)
    p.add_argument('--deadline-utc',required=True)
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();deadline=utc(a.deadline_utc)
    train=pd.read_csv(root/'data/features.csv')
    train=train[(train.home_score!=train.away_score)&train.home_score.notna()&train.away_score.notna()]
    # Only seasons completed before prediction year; no current-season refit.
    train=train[train.season<deadline.year]
    if train.empty:raise ValueError('No eligible training history')
    upcoming=pd.read_csv(a.features)
    cols=json.loads((root/'data/winner_manifest.json').read_text())['feature_order']
    if not set(cols+['game_id']).issubset(upcoming.columns):
        raise ValueError('Certified 63-feature columns and game_id required')
    records=[]
    for name in MEMBERS:
        fitted=model(name).fit(train[cols],train.home_win.astype(int))
        for (_,r),prob in zip(upcoming.iterrows(),fitted.predict_proba(upcoming[cols])[:,1]):
            records.append({'game_id':r.game_id,'model_id':name,'model_version':'EXPERIMENTAL_V1',
                'p_home_win':float(prob),'training_cutoff_date':str(train.gameday.max()),
                'training_max_season':int(train.season.max()),'deadline_utc':a.deadline_utc,
                'feature_snapshot_ref':str(a.features.resolve()),
                'feature_snapshot_sha256':hashlib.sha256(a.features.read_bytes()).hexdigest(),
                'data_status':'UNLOGGED_RESEARCH_INFERENCE',
                'note':'Attach actual prediction time, kickoff, quote times and eligibility before prospective ingestion.'})
    a.out.write_text(json.dumps(records,indent=2,allow_nan=False))
    print(f'{len(records)} challenger predictions; not prospective until validated and logged.')

if __name__=='__main__':main()
