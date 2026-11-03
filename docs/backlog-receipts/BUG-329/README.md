# BUG-329 closeout

Base commit `d267860536265a9a892ea9c26a6d053b27b68edd`.

Added a retained Hydra regression through the engine submission path. The fake bridge receives one pass and no indexed cast for the contradictory cast/hold answer. Existing coverage checks cache, narration, intent, and decision history.

Focused regression: 88 passed in 4.54s. Independent review and live qualification remain open.
