# Reviewed integration patch — 2026-10-09

Upload these folders to your existing repo, keeping their paths. No repository push or app deployment was performed here.

Commit before 2026-10-14T17:00:00Z (1 pm Eastern). Full Git history is fetched in CI. Missing or late matching registration evidence causes records to remain not counted. Commit dates are repository evidence, not independent timestamp certification. The registration JSON, rule.json, training features and model implementations are unchanged.

From the repository root, validate before committing:

```bash
(cd nfl_comparison && python3 -m unittest discover -s tests -v)
(cd nfl_comparison && sha256sum -c MANIFEST.sha256)
sha256sum -c INTEGRATION_MANIFEST.sha256
```

Ten tests passed locally, including registration timing, injury coverage, export consistency and external quarantine. The full production lock cannot be run from this ZIP: existing tracker, model bundle, feature builder, injury generator and app are not included. Run a manual export smoke check after integration; inspect the next scheduled lock logs.

Injury reports must explicitly include both team keys (empty lists are acceptable). Missing team coverage, future dates or stale reports produce UNKNOWN and PASS. Confirm the existing injury generator emits this coverage; do not infer a clean report from an omitted team.

External SAFE and AS_IS results are reference only. The external workflow has not been run; neither external model enters the registered average. Independently validate the external data pipeline and leakage before introducing a separately reviewed ranking policy.

The app Lab tab and courier deployment cannot be verified or updated from this ZIP. Confirm they consume comparison_lineboard_export.json. No bets are placed.

Changes: repository registration evidence is logged and gates counting; full checkout history enabled; injury validation fails closed; export uses each decision's own member probabilities; frozen reference copies are logged at comparison observation time with their original source time retained; external exports are quarantined. Registration and pick thresholds are preserved.

The frozen reference still depends on the existing tracker schema and latest lock row. End-to-end verification must confirm that row belongs to the official lock from the same run. Historical predictions without registration evidence stay not counted.
