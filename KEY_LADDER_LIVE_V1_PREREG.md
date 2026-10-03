# KEY_LADDER_LIVE_V1_PREREG — live test of key-number alternate-spread pricing (NFL)

**Written:** 2026-10-03, before any live ladder has been pulled. The sha256 of this file will be hard-coded into the evaluation script.

## Idea (the user's)
Straight NFL spreads are about a coin flip. NFL margins cluster on 3, 7 and 10, while books price alternate spreads with a roughly smooth cost per half-point. So some rungs of a book's alternate-spread ladder may be priced better than the real chance of covering, especially rungs that cross 3/7/10, whether buying or selling points.

## Frozen fair-value table
- **File:** `nfl_key_ladder_fair_table_v1.json`, sha256 `5c32c251adb90ec54cce647fc5987d4a5a6b6506de143ae48b2ef58c4fc2eea2`.
- **Source:** nflverse games.csv 1999–2025 (7,276 games, commit ae82be2f0f).
- **For each closing home spread s** (half-point grid): the histogram of final home margins among games with |spread_line − s| ≤ w, using the smallest w in {1, 1.5, 2, 3} that gives at least 200 games.
- **Real cover chance** of team T at alternate point a: share of those games where T's margin + a > 0, among non-pushes. Pushes are counted separately. The 95% range is the Wilson interval.

## Live data (`nfl_key_ladder_live_v1.py`, GitHub Actions)
- **Pull:** Odds API event odds, markets `spreads,alternate_spreads`, books pinnacle, draftkings, hardrockbet_fl (2 credits per game).
- **Schedule:** Thursday 22:30 UTC (games in the next 6h) and Sunday 13:30 UTC (games in the next 36h). Manual runs are allowed.
- **First snapshot rule:** only a game's first snapshot is used; it is never replaced.
- **Main line:** Pinnacle's main home spread, or else the first user book's. `s` is the main home spread with sign flipped to the home-favored-positive convention.
- **Which rungs get priced:** user-book rungs within 14 points of the main line, where the table row has at least 200 games.
- **EV** = (1 − p_push) × [p × (dec − 1) − (1 − p)], at the book's price; `EV_low` uses the Wilson low end of p.
- **Pinnacle agreement:** if Pinnacle quotes the same rung and its mirror, compare the book price against Pinnacle's de-vigged fair probability. Agree = EV > 0 against it. If Pinnacle doesn't quote the rung, agreement is unknown.

## Tiers (fixed now)
- **HIGH:** EV_low ≥ 0, and Pinnacle agreement is not "disagree".
- **MEDIUM:** EV ≥ +0.03 (and not HIGH).
- **LEAN:** +0.01 ≤ EV < +0.03 (information only).
- **Official bet:** one per game, the highest-EV rung among HIGH/MEDIUM (HIGH preferred), 1 unit at the book price. A push returns the stake.

## Evaluation (one look only)
- **When:** at the first of (a) 250 graded official bets, or (b) 2028-02-15.
- **Primary:** ROI of official bets. PASS = 10,000-resample bootstrap 95% CI lower bound > 0, with at least 100 bets.
- **Secondary (descriptive):** ROI by tier (HIGH / MEDIUM / LEAN, all logged rungs); by book; by whether the rung crosses 3/7/10; buying vs selling points; calibration of p versus actual cover rates.
- **Interim:** season summaries are descriptive only.

## What will NOT happen
- No change to the table, tiers, thresholds, books, schedule rule or criteria after data is seen. Pipeline bug fixes are allowed but must be logged with a date.
- No bets are placed by any script.
- No 2026 outcomes are in the fair table.

## Honest limits
- The table conditions only on the closing spread, not on the total or teams. A high total widens margins, which this table ignores.
- Prices move between the snapshot and when the user could bet.
- Pinnacle may not post alternate spreads, so agreement is often unknown.
- Power is low: a few official bets per week.
