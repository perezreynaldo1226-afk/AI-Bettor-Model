# External candidate inspection — October 9, 2026

Both repositories were cloned and their local README/source/configuration inspected. Neither external training or inference pipeline was executed. No external pretrained pickle was loaded. Their claims are not independently verified.

## ShamgarBN/nfl-bet-engine

URL: https://github.com/ShamgarBN/nfl-bet-engine

Inspected commit: 9aa4326c96d6aa5d771b7b68be8226d9f96a7023

MIT LICENSE present. pyproject.toml requires Python >=3.13, LightGBM, nflreadpy, DuckDB, and many additional dependencies. Current execution environment uses Python 3.11 and has no LightGBM. The uploaded handoff supplies the existing model's features, not this repository's full feature warehouse.

An unmodified source snapshot, including its MIT license, is bundled in external/nfl-bet-engine for Claude to continue from this inspected commit. It is not installed, executed, or connected to the scoreboard.

Source `backtest/walkforward.py` fits on prior seasons, simulates per-week predictions, and uses closing spread/total columns. The function docstring says weekly refitting but the implementation refits per season. It fits/saves calibrators after accumulating backtest predictions. Verify precisely how those calibrators are loaded into live inference before considering any calibrated historical result valid. Inspect feature-matrix selection to establish which closing-market values enter the moneyline probability path.

The CLI backtest output is season-level; per-game forecasts are written to its journal. An integration must export per-game home-win probabilities with training cutoffs and honest market timing from that journal, then feed the explicit importer. Do not pass a season summary CSV as game predictions.

Smallest next step on Claude's machine: create a separate Python 3.13 environment, clone this exact commit, install its locked environment, obtain its data warehouse, reproduce the per-game chronological backtest and inspect feature timing before activating the adapter. Do not silently substitute this package's original tree challenger for this external model.

## moiz-manzoor/nfl-game-predictor

URL: https://github.com/moiz-manzoor/nfl-game-predictor

Inspected commit: 8677d3851cf923829e0dab214ba0f0bcbc522d4b

No LICENSE file found in the inspected checkout. Public source availability alone does not establish redistribution rights; external source/artifacts are not redistributed in this package. Resolve usage permission before bundling this code in the app.

README describes team-level rolling features and logistic regression. It explicitly discloses hyperparameter selection on the reported test seasons, historical feature-selection and lookup bugs, limited leakage-test coverage, and no fresh holdout for the production refit. Original published results are therefore research references, not independent evidence for our decider.

Smallest next step: resolve licensing, rebuild its feature table from data with verified point-in-time availability, refit within our chronological comparison harness, and export all-game probabilities with metadata. Do not import its full-data fitted production artifact into historical test periods.

## What the delivered system uses instead

Three original implementations running on the supplied certified feature matrix: baseline logistic architecture, stronger regularization, and shallow histogram gradient boosting. These demonstrate the full chronological ranking and ensemble workflow now; they do not recreate either external repository's full pipeline. External adapters are explicit record importers, not claims of a completed external integration.
