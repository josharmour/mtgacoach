# B2 repeated-trial summary source, 2026-11-14

Transport-free preparation only. No model runner, credential/endpoint configuration, case export, real labels, real trials or acceptance claim.

`tools.benchmark_repeats.summarize_repeats` consumes a fixed hashed labelled-case cohort, 2–100 requested repeats and hash-only trial outcomes/timings. It reports acceptable/blunder rate and min/max/mean/population standard deviation across repeats. Every expected case stays in each repeat denominator: missing, withheld and error outcomes cannot inflate success. All missing counts remain explicit. Unlabelled choices are not silently scored acceptable. Duplicate trials, changed cohort IDs, unexpected/private fields, nonfinite/negative or >24-hour timing fail closed. Outputs contain no per-case identifiers, choices, prompts or personal fields; provenance hashes accompany aggregate counts.

This validates structure only, not that hashes describe genuine labels, a real model was used, background priority was honored, or a measured improvement is causal. Spread is descriptive, not a confidence interval/win rate. A future runner still needs approved execution/destination, actual planner paths and scoped background scheduling; existing B2 acceptance is unchanged.

17 synthetic tests pass under OS deny-network; Ruff clean. No real data replay. Isolated base4e7ddfe. Reviewer requested before integration.
