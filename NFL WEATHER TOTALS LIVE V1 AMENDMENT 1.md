# NFL_WEATHER_TOTALS_LIVE_V1 — Amendment 1 (2026-09-29)

**Written before any live pick exists.** The pre-registration (`NFL_WEATHER_TOTALS_LIVE_V1_PREREG.md`, sha256 `18c885d58673b7c0dd11be6485cfc117d2c1770f49a373169a5dc64f60fe235b`) is unchanged.

## What happened
The original model file (sha256 `67cf03fe…9908`) and the original script were never uploaded to the repo and can't be found. No picks were ever made with them.

## What changes
- The script `nfl_weather_totals_live_v1.py` was rebuilt from the pre-registration's spec.
- The model is refit once, on the first GitHub Actions run, using the exact same spec: `LogisticRegression(C=1.0)` on standardized features; all non-push nflverse games 2006–2025 with closing totals odds; features as listed in the pre-registration, defined as in WEATHER_MATCHUP_V1.
- The new model's sha256 is printed in that run's log and stored in the DB (`meta.model_sha`). From that point it is frozen: the script stops if the model file ever changes.
- If the training count is far from 5,216, the run log warns; that is checked before the first pick counts.

## What does not change
Pick rule (EV ≥ +0.02 at Pinnacle), evaluation (one look at 300 graded picks or 2028-02-15), PASS bar, secondary analyses, and every "will not happen" item.
