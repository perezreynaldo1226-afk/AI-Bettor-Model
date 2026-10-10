# Validation performed

- Full season-forward research run completed: 1,954 games per base model across 2019–2025; 1,688 games in the complete shared comparison panel.
- Five Python integrity tests passed. These cover odds/probability orientation, stale/future/missing quote behavior, missing-model PASS, append-only persistence, metadata chronology, external training overlap rejection, base fit cutoff ordering, ensemble weight cutoff ordering, and identical scoreboard samples.
- Example inference completed for all three challengers on four supplied rows (12 probabilities). This retrospective example is not prospective test evidence.
- Experimental decision command ran on synthetic inputs and returned the expected 6.15% model-estimated home EV.
- Dashboard JavaScript executed in a Node VM with a minimal DOM stub: six scoreboard rows, season/opposite-side filters and empty-probability rejection passed.
- Visual browser verification was not completed: the environment lacked a browser executable and browser downloads failed. No screenshot or responsive-layout validation is claimed. Claude should check the delivered dashboard in its browser before integrating it.
- External repository source inspected, but external model training/inference not executed.

These checks establish a runnable research scaffold, not live sportsbook integration or a proven betting edge.
