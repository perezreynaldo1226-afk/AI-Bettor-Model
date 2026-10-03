# LOWTOTAL_DOG_LIVE_V1_PREREG: live test of the low-total underdog rule (NFL)

**Written:** 2026-10-03, before any live pick. The sha256 of this file will be hard-coded into the evaluation script.

## Why
SPREAD_RULES_V2 R3 (backtest, 2011–2025, real closing prices): 460 bets, ROI +2.3%, 98.3% CI [−8.4%, +12.5%], profitable in 9 of 15 seasons, positive in both eras. It failed its pass rule and is on the watchlist. This tests it going forward. No extra Odds API credits are used.

## Rule (fixed now)
- **Spread and price:** the NFL tracker's latest locked market line for the game (the line the app shows), dog spread price from that lock.
- **Total:** Pinnacle's total line from the NFL_WEATHER_TOTALS_LIVE_V1 official snapshot (taken ≤ 3.5h before kickoff), whatever that game's weather pick is (OVER, UNDER or PASS). Games with no official total are skipped.
- **Bet:** the underdog when it gets +3 to +7.5 points (inclusive) and the total is ≤ 41. 1 unit at the locked dog spread price. A push returns the stake.
- Before the official total exists, the app may show a preview from the preview total. Previews are never counted.

## Grading
Final scores from the NFL tracker's grader (the same `outcome` the app uses).

## Evaluation (one look only)
- **When:** at the first of (a) 150 graded bets, or (b) 2028-02-15.
- **PASS** = ROI bootstrap 95% CI lower bound > 0 (10,000 resamples), with at least 60 bets.
- About 1–3 qualifying games per week, so expect a long test. A FAIL at this size means "not confirmed", not "disproved".

## What will NOT happen
No change to the rule, thresholds or criteria after data is seen. Pipeline bug fixes are logged with a date. No bets are placed by any script.
