# NFL_WEATHER_TOTALS_LIVE_V1_PREREG — live-forward confirmation of the weather/totals lead

**Written:** 2026-09-29, before any live pick exists. The sha256 of this file is hard-coded into the future evaluation script.

## Why
WEATHER_MATCHUP_V1 Test A (historical, 2011–2025) passed the money criterion (+4.8% ROI, CI [+0.16%, +9.5%], 11 of 15 seasons profitable) but failed the log-loss criterion, so it is officially a FAIL and only a lead. The historical weather was game-day actuals, not forecasts. This test checks whether the edge holds with information that is really available before kickoff, against real current prices.

## Frozen model
- `nfl_weather_totals_model_v1.json`, sha256 `67cf03fe17268dc9a0bfff194de0b62cb76d1a536182ea94c84803305ffc9908`.
- It is Test A's logistic regression refit on all 5,216 non-push nflverse games from 2006–2025. The features are unchanged: logit(q_over), total_line, wind_out, wind_high (≥15 mph), temp_out, cold (≤32°F), roof_closed.
- It is never refit during this test.

## Live data (collected by `nfl_weather_totals_live_v1.py`, run from GitHub Actions)
- **Timing:** each run covers NFL games starting within the next 3.5h. Runs are shortly before the Thursday, Sunday (early, afternoon, night), Monday and Saturday kickoff windows.
- **Price:** Pinnacle's totals line and over/under prices at that snapshot.
  - `q_over` = the de-vigged Pinnacle over probability; `total_line` = Pinnacle's line.
- **Venue and roof:** from ESPN's scoreboard (`venue.indoor`). Indoor means roof_closed = 1, with wind 0 and temp 60.
- **Weather:** the open-meteo forecast for the venue city at the kickoff hour: 10 m wind (mph) and temperature (°F).
- **Result:** the final total from ESPN's scoreboard.

## Pick rule
- `p_over` comes from the frozen model.
- `EV = p × (dec − 1) − (1 − p)` at Pinnacle's price for each side. A pick is made on the higher-EV side if EV ≥ +0.02. A push returns the stake.
- Every game is logged, including passes and skipped games with a reason.

## Evaluation (one look only)
- **When:** at the first of (a) 300 graded picks, or (b) 2028-02-15.
- **Primary:** ROI of picks at Pinnacle's price. PASS requires a 10,000-resample bootstrap 95% CI lower bound > 0 and at least 100 picks.
- **Secondary (descriptive):**
  - ROI at the best user-book price on the same line (DraftKings, Hard Rock, Kalshi, Polymarket);
  - outdoor vs indoor;
  - Over vs Under;
  - wind ≥ 15.
- **Interim season summaries** may be reported as descriptive only. They are not tests and change nothing.

## Honest limits
- **Power is low.** Historical picks ran about 146 per season. At a true +4.8% ROI, one season (about 100–150 picks) cannot confirm the edge; 300 picks is a minimum. A FAIL at that size means "not confirmed," not "disproved."
- The forecast is a 10 m wind speed at the city geocode, not the stadium.
- Retractable roofs follow ESPN's indoor flag.
- International and neutral-site venues come from ESPN's venue city.

## What will NOT happen
- No refit, threshold change or feature change after picks begin. Pipeline bug fixes are allowed but must be logged with a date.
- No bets are placed.
