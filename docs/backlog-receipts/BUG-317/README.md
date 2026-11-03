# BUG-317 closeout

Base commit `fbf3ee273b8e5e5d74b4ee9f4cbf181a5dcf3d06`.

Coverage-only closeout. The draft advisor validates the recommended pick against distinct pack alternatives, uses supplied quality bands or remaining heuristic ranks, and discloses missing rationale. The draft driver preserves the comparison in short narration. The focused cases exercise higher/equal/unknown bands, misleading rankings, and a one-choice pack. No external score or live audio result is inferred.

Focused regression: 10 passed in 0.07s. Independent review and live qualification remain open.
