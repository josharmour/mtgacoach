# Retained F2 compiler outputs

These five JSON blocks are copied without content edits from the F2 report appendix.
manifest.json records the source report path and SHA256. compiled_at/compiled_by and
all Unknown effects are preserved; these are actual retained outputs, not new compilation.

The optional-imprint adapter is consumer-only. Fury and Lotus Cobra remain unmodelled
because their original records contain Unknown; the Minsc and Sylvan records likewise
retain their Unknown entries. This does not complete F3 semantic review or its sample.

Compatibility tests deliberately inject records under the card lookup hash to isolate
semantics. They do not prove live hash-source alignment or that a service serves records.
