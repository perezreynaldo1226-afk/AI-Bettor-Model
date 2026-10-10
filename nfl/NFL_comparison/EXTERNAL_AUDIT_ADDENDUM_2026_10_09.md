# External audit addendum (2026-10-09)

This adds to `EXTERNAL_AUDIT.md` and does not replace it. Nothing below runs either external model inside the app or the live lock, and nothing here changes Model 1.0, the `EXPERIMENTAL_V1` rule or its members.

## ShamgarBN/nfl-bet-engine

**Status:** `runner_ready_not_executed`.

- **Commit:** `9aa4326c96d6aa5d771b7b68be8226d9f96a7023`. HEAD was re-checked on 2026-10-09 and is unchanged.
- **License:** MIT, and the LICENSE file is present at that commit.

### Static audit

The source was read at the exact commit. File and line references are to that commit.

1. **How the probability is made.** The home-win probability comes from a Monte-Carlo score simulator fed by LightGBM score models. The simulator rounds scores, so some probability lands on ties, and `P(tie)` is computed but dropped (`model/simulate.py:94-95`).
2. **Market inputs.** The closing spread, total and moneyline are model inputs: `features/market.py:61-86`, the `mkt_*` columns. In nflverse schedules, open equals close, so the "move" features are mostly zero.
3. **Features that see the future.** Each of these uses information that does not exist before kickoff:
   - `lookahead.py:88-110` looks up each opponent's *latest* strength anywhere in the warehouse, which includes later games.
   - `ol_continuity` takes its returning-starter share from the target game's own snaps.
   - `qb_form` and `qb_tier` are keyed to the starter who actually played in the target game.
   - `injuries` and `star_players` use final game status.
   - `surface_weather` uses observed archive weather.
   - `officiating` uses an expanding mean that is not grouped by referee.
4. **Refit schedule.** The model refits once per season (`backtest/walkforward.py:88-95`). The docstring says weekly; the code comment admits per season.
5. **Calibration.** The isotonic calibrators are fit on the pooled out-of-fold predictions of every season. They are not applied to the backtest probabilities and cannot be used for historical evaluation.
6. **Journal.** The backtest journal stores spread rows only. There is no per-game moneyline export.

### Resolution of the blockers

| Blocker in EXTERNAL_AUDIT.md | Resolution |
|---|---|
| Needs Python 3.13, LightGBM, DuckDB and its own warehouse | Workflow `.github/workflows/nfl_external_bet_engine_v1.yml` (run manually) clones the exact commit, verifies HEAD and LICENSE, installs its own `uv.lock` (`uv sync --frozen`), and builds its warehouse from free nflverse data into scratch paths. It cannot run in the integration sandbox, because packages could not be installed there. |
| No per-game export; only season summaries | `external/runners/nfl_bet_engine_export_v1.py` calls the repo's own `run_walkforward_with_drop_groups` and captures its out-of-fold frame in memory. It writes every game with `training_cutoff_utc` set to the end of season S-1 and `record_type RESEARCH_OOS`. The external repo is not edited. |
| Closing lines and look-ahead features | **SAFE** variant: drops the `market`, `injuries`, `officiating`, `star_players`, `qb_form`, `qb_tier`, `surface_weather`, `ol_continuity` and `lookahead` groups, and replaces `lookahead.build` with a no-op. **AS_IS** variant: unchanged; reference only, never ranked. |
| Calibrators | Not loaded and not saved: the capture hook replaces `_fit_and_save_calibrators`. |
| Ties discarded | Exported `probability = P(margin>0) + 0.5*P(margin=0)`, with the raw values kept in separate columns. |
| Scoring on identical games | `scoreboard_with_external.py` scores every model on the intersection of games. Lookahead-safe exports are ranked; the others appear under `reference_not_ranked`. |

### What was tested

- The runner was tested against a stub `nfl_model` package for both variants, and its output passed `import_external.normalize`.
- `scoreboard_with_external.py` was tested on a temporary synthetic export. That synthetic data was not shipped.
- **No real external results exist yet.** "SAFE is lookahead-safe" is a conclusion from reading the code. It should be re-checked once the run exists, for example by comparing SAFE against SAFE with the `team_epa` cutoff shifted.
- **Status after a run:** any result is development evidence on previously examined seasons (2019-2025). It would not be a registered evaluation.
- **Live use:** using the model live would be a **new** registered version (for example EXPERIMENTAL_V2) with its own hash and start deadline. It would never be added to EXPERIMENTAL_V1.

## moiz-manzoor/nfl-game-predictor

**Status:** `unavailable_license_unconfirmed`. It is still blocked.

- **Commit:** HEAD is `8677d3851cf923829e0dab214ba0f0bcbc522d4b` (5 commits), unchanged on 2026-10-09.
- **License:** there is no LICENSE file.

This blocker cannot be resolved technically: only the author can grant permission. Until the author answers, no code from that repo is copied, run or bundled here.

### Draft request to the author

This is a draft for the owner to post or send. It has not been sent.

> Hi, I'd like to re-run your nfl-game-predictor pipeline (team rolling features + logistic regression) inside my own private research comparison harness, refit chronologically on prior seasons only, and record its per-game home-win probabilities next to my own model. The repo has no LICENSE file, so I wanted to ask first: would you be willing to add an open-source license (e.g. MIT) or give permission for this use? I'd credit the repo and commit and won't redistribute your trained models. Thanks!

If permission arrives:
1. Re-clone and record the commit and license.
2. Rebuild its features from point-in-time data.
3. Refit inside the chronological harness, never using its full-data production model.
4. Export all games through `import_external.py`.
