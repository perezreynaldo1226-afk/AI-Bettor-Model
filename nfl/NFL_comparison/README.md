# NFL Comparison V1

An experimental, local comparison package for Claude to integrate into the existing app. Production models and pick rules remain in the original handoff, unmodified.

## Start

Python 3.11+ is supported. From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python3 run.py research
python3 run.py serve
```

Open http://127.0.0.1:8080/dashboard.html. The generated dashboard also opens directly as a local file; it embeds its report and uses no network.

Generated outputs are already included. Re-running research fits three models at the start of each season, using only previous seasons. Median imputation/scaling are fitted on training rows only. Tied games are excluded consistently for all research models, so these fits are not byte-for-byte reproductions of the deployed model. The original deployment artifact is never loaded, edited, or replaced.

## What actually runs

- BASELINE_LR: architecture-equivalent logistic baseline with C=.2.
- STRONG_REG_LR: original challenger with C=.02.
- TREE_CHALLENGER: original shallow histogram gradient boosting challenger.
- SIMPLE_AVERAGE: arithmetic mean of these three.
- WEIGHTED_ENSEMBLE: nonnegative weights chosen on a fixed .1 simplex grid using earlier season-forward out-of-sample predictions. Needs 250 earlier predictions; no evaluation-year outcomes enter weights.
- MARKET_CLOSE_BENCHMARK: proportional de-vig of both closing moneylines, benchmark only.

The scoreboard restricts EVERY member to the same games, including weighted warmup and market availability. Reports include seasonal metrics, calibration counts, paired slate-bootstrap log-loss intervals, residual correlation, and disagreements. Historical results are development evidence; no independent holdout or profitable strategy is claimed.

The two external GitHub candidates are not running models in this package. Their inspected commits, licenses, and blockers are in registry.json and EXTERNAL_AUDIT.md. Do not label the original challengers as those repositories.

## Prospective ingestion and decision API

```bash
python3 run.py ingest --record examples/prospective_record.json --db prospective.sqlite3
python3 run.py decide --record examples/decision_input.json
```

Examples are SYNTHETIC and use future timestamps for exercising the interface. They are not picks or real game records. Use a temporary DB for examples.

Generate challenger forecasts from the existing certified upcoming feature file:

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python3 predict_features.py \
  --features /absolute/path/to/certified_upcoming_features.csv \
  --deadline-utc 2026-10-14T17:00:00Z --out upcoming_challengers.json
```

This output is unlogged inference, not automatically prospective evidence. Attach the actual generation time, kickoff, odds metadata and eligibility, then validate and append before the registered deadline. The example inference shipped in results was generated retrospectively from the supplied Week 1 sample and is explicitly not test evidence.

`comparison.core.append_record` validates training cutoff < prediction <= deadline < kickoff. It stores immutable JSON records with a SHA-256 identity. Duplicate payloads are rejected; later locks append new records. Null provider quote timestamps remain null. A quote timestamp is required to issue a bet decision, even if a retrieval timestamp exists.

`rule.json` freezes the initial illustrative combination and decision rule: all three required probabilities, simple average, >=5% estimated EV, provider quote age <=30 minutes, eligible game. The Python command fails to PASS on missing members, eligibility, missing prices, or stale/unknown quotes. This rule is a research default, not a registered prospective strategy. Register its hash and an explicit start deadline before evaluating future performance; evaluate only subsequently logged games. Do not treat old live games as fresh evidence for this new rule.

For production, pass model probabilities and odds from the SAME game and deadline; log model metadata using `append_record` before aggregating. The lightweight calculator assumes this association, so it is not an autonomous live execution engine. No bets are placed.

## Integrating external predictions

Use `import_external.py` to normalize season-forward research exports. The importer requires explicit game IDs, probability orientation, model version, training cutoff, and compatible NFL moneyline market. It checks kickoff and training/prediction/deadline metadata for prospective records via `append_record`.

External exports do not automatically enter rankings or the fixed decider. Rebuild a common panel only after inspecting provenance and aligning prediction deadlines. Never substitute missing model predictions with a market probability or another model.

## Claude integration instructions

1. Add this package beside the existing code; do not replace Model 1.0 or its builder.
2. Add a separate Experimental Comparison tab using dashboard.html or report.json.
3. Connect the existing lock workflow to immutable feature snapshots, odds provider quote timestamps, local retrieval timestamps, and prospective ingestion.
4. Produce live forecasts for challengers using the same certified feature rows; train only on prior completed data. Do not read target scores from incoming feature rows.
5. Integrate external models only after independently reproducing their data/feature pipelines and resolving licensing. Log them as unavailable until then.
6. Keep frozen members and rule version for the registered evaluation. A change starts a new version.
7. Save original locks and injury eligibility decisions before kickoff. Add results as separate settlement events; never rewrite predictions.

This is a runnable research/integration starter, not a live deployment of the complete external ensemble. No stake recommendation, validated ROI, or automated weight update from individual wins is provided.
