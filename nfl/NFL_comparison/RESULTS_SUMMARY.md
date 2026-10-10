# First runnable research comparison

Shared panel: 1,688 non-tied NFL games, 2020–2025, after weighted-ensemble warmup and market availability restrictions. All rows below use identical games. Lower log loss means better probability quality.

| Model | Accuracy | Log loss |
|---|---:|---:|
| Closing market benchmark | 66.5% | 0.6070 |
| Simple average | 63.9% | 0.6350 |
| Chronologically weighted ensemble | 64.3% | 0.6357 |
| Stronger regularization | 64.2% | 0.6369 |
| Baseline logistic architecture | 64.3% | 0.6401 |
| Shallow tree challenger | 63.7% | 0.6420 |

The simple average improves log loss over the baseline by approximately 0.0051 per game. A descriptive season/week cluster-bootstrap interval for average-minus-baseline is [-0.0096, -0.0009]. This is historical development evidence after extensive prior examination of these seasons, not untouched validation or proof of a tradable edge. It also does not improve winner accuracy on this panel.

Closing-market log loss remains better than all tested candidates. Closing prices reflect later information than an early prediction deadline. No historical ensemble ROI is reported because timestamped executable prices are missing.

The two external repository models remain unavailable, with exact blockers in EXTERNAL_AUDIT.md. The three running models are original implementations using the supplied feature matrix. Existing production model files and rules are not changed.

Verification: five integrity tests passed, including probability orientation, odds math, stale/future quote rejection, missing-member PASS, append-only storage, external training overlap rejection, chronological base-model fits, chronological ensemble weights, and equal scoreboard samples. The example inference produced 12 probabilities from four supplied game rows. Browser verification is recorded separately in VALIDATION.md.
