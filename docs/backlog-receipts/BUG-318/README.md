# BUG-318 closeout

Base commit `683ef634cfdc524545584bb3c1c4c8e6272a5dcb`.

Coverage-only closeout. The native request boundary separates sideboarding from mulligan, generic cancellation and initial deck mutation. The sideboard adapter/driver tests exercise registered pool and request identity, invalid deck changes, concurrent/reissued submissions, short timers, and return of ownership to gameplay. The BO3 resume test distinguishes a game result from the end of the match. Constructed and uncertain requests retain their documented manual limits.

Focused regression: 63 passed in 0.35s. Independent review and live qualification remain open.
