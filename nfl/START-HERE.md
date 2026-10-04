# NFL player props: first coverage check

This is an additive patch for AI-Bettor-Model, not a replacement repository.
It contains a manual workflow and a Python script. No existing files need to change.

## Install on GitHub

1. Unzip this package on your computer.
2. In AI-Bettor-Model, use Add file → Create new file.
3. For the filename, enter `nfl/props/coverage_probe_v1.py`. Open the matching downloaded file in a text editor, copy its contents into GitHub, and commit.
4. Repeat for `.github/workflows/nfl_props_coverage_v1.yml`. If the hidden `.github` folder is not visible in Finder, press Command+Shift+Period.
5. Open Actions → NFL player props coverage check → Run workflow. Choose season 2026 and run once.
6. Open that run. Read its summary, then download the `nfl-props-coverage` artifact near the bottom. Upload that report ZIP to ChatGPT.

Required repository secrets: `ODDS_API_KEY`, `ODDSPAPI_API_KEY`, `API_SPORTS_KEY`.
Do not copy secret values into files. These are the names already configured.

## What the check does

- The Odds API: lists upcoming NFL events and requests receptions for the earliest one, US region only. At most two HTTP calls; the event odds request normally costs one market × one region (one credit). Actual quota headers are recorded.
- OddsPapi v4: retrieves the market catalogue, finds priced NFL games within seven days, and checks the earliest game's receptions. At most three HTTP calls. HTTP call counts are not claimed as exact billed credits.
- API-NFL: checks NFL season coverage, lists games and, if player-stat coverage is enabled, samples the latest completed game's player statistics. At most three HTTP calls. Receiving/target field labels are reported; their completeness still needs review.
- Each odds provider samples independently; games may differ. This is a coverage check, not a cross-provider price comparison.
- No automatic scheduling, retries, training, pick changes, commits, deployments or bet placement.
- Reports contain selected sports data, quota headers and status only. Raw account responses and authenticated URLs are not saved.
- A failed provider produces an ERROR and a failed workflow, while the report is still uploaded. Empty markets are explicitly distinguished from errors. A one-game empty response is inconclusive.

If 2026 player stats are unavailable, run again with 2025 to distinguish current-season access from historical access. Do not repeatedly rerun an authentication error; verify the secret name and provider dashboard first.

## Validation and limits

Locally checked with mocked provider responses: receptions parsing for both odds feeds, receiving-field detection, and missing-secret error/report behavior. Live authentication, current-plan coverage, price freshness and quota charges have not been tested: keys remain in GitHub Secrets.

This is a data audit, not a statistical experiment. Model training will require a separately saved, hashed preregistration and a clean evaluation design.

Official endpoint references checked October 4, 2026:
- https://the-odds-api.com/liveapi/guides/v4/
- https://oddspapi.io/us/docs/get-markets
- https://oddspapi.io/us/docs/get-odds
- https://oddspapi.io/en/docs/get-fixtures
- https://www.api-football.com/news/post/how-to-get-started-with-api-nfl-the-complete-beginners-guide
