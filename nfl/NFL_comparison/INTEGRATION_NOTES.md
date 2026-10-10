# Integration notes (2026-10-09)

These notes map each step of the README's integration list to what was built. Model 1.0, its builder, the V1.5 deciders, the tracker and the app's Home picks are untouched. Every change is a new file, except the workflow edit described in step 3.

1. **Package location.** The package lives in `nfl_comparison/`, next to the existing code.
   - `run.py` re-ran the research end to end, and its scoreboard matched the shipped `results/report.json` to about 2e-16.
   - All 5 tests in `tests/` pass. They were run with Python 3.11 and scikit-learn 1.8.
2. **Experimental Comparison tab.** The Lineboard app v38 adds a **Lab** tab.
   - It reads two database collections: `comparison_report`, holding the research scoreboard, registry, registration and live tally, and `comparison_live`, holding one document per logged game.
   - Both collections are filled from `nfl/comparison_lineboard_export.json` by the scheduled sync.
   - `dashboard.html` was also opened in Chromium at 400 px wide: there were no console errors and no sideways scrolling.
3. **Lock workflow.** `nfl/nfl_comparison_v1.py lock` is a new step in `grade_nfl.yml`. It runs after the official lock and the injury report.
   - It copies the run's certified feature CSV, odds CSV and injury report into an immutable snapshot folder (`nfl/comparison_snapshots/<season>_w<week>/<stamp>/`) with SHA-256 hashes.
   - It records two timestamps: the provider quote time (the odds `snapshot_time`) and the local retrieval time (the new `retrieved_utc` column from `fetch_week_odds_v3.py`).
   - It writes every record through `comparison.core.append_record`. That function enforces the chronology checks, and the SQLite tables reject updates and deletes.
4. **Challenger forecasts.** `predict_features.py` runs on the snapshot of the same certified rows that Model 1.0 used.
   - Each member is trained only on seasons before the prediction season.
   - Target scores are never read.
   - Live fits use the NFL workflow's pinned scikit-learn 1.9.1. `requirements.txt` here says <1.9, which applies only to research re-runs. This is disclosed in the registration.
5. **External models.** They are logged as unavailable or not executed, and neither is a live member. See `EXTERNAL_AUDIT_ADDENDUM_2026_10_09.md` and `registry.json`.
6. **Frozen versions.** The members and `rule.json` are frozen by `REGISTRATION_EXPERIMENTAL_V1.json`.
   - Its hash is embedded in every record's `rule_version` as `EXPERIMENTAL_V1+reg:<sha12>`.
   - Any change means a new version.
7. **Originals and settlements.** Original locks and eligibility are saved before kickoff. `settle` adds results as separate append-only events, and predictions are never rewritten.
   - Records made before `start_deadline_utc` are shown in the app as "not counted" and never enter the tally.

No bets are placed, and no stake is recommended anywhere.
