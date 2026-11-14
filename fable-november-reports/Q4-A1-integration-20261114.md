# Q4 + A1 isolated integration verification

Sources: Q4 e5431923b7e93bcd413ef172cf2ca3b892bef532 (NAS master 1f0a1fc descendant) and independently verified A1 ddffea88. Merge prepared only in isolated /Users/joshu/q4-dotty-20261114, branch dotty/q4-a1-integration-20261114.

A1's branch predates other master work. Three-way merge preserves newer master fixes, rather than replacing files from its older tree. F1 schema, validator and schema-test add/add conflicts are formatting-only: independently parsed ASTs equal after normalizing import order; retained master formatting. Conftest conflict retained A1's explicit offline facts lookup guards. Result adds 5 A1 files atop Q4. Synthesizer, line-search implementation/tests and conftest exactly match ddffea88; standalone composes the once-per-match record-log reset hook with current master's match transition.

Verification: 315 focused tests passed in 88.58s. Files: test_q4_outcomes, test_match_bundle_endpoint, test_card_facts_schema, test_card_facts_client, test_ability_synthesizer, test_line_search, test_line_search_opponent, test_line_search_pings_20261019, test_line_search_walkers_20261019. Tests used source PYTHONPATH, fake providers and an IPv4/IPv6 connect guard, no inference. Ruff passed affected source/test files. No unresolved conflicts; staged diff check clean. Full project suite deferred until whole plan completed.

Safety: Coach desktop, engine and Arena active on NAS master; live checkout has only untracked improvements tracker, no tracked modifications. Do not infer safe handoff from completed tests. No live checkout update, push, deployment, restart or stable promotion performed. Integration awaits confirmed stopped safe boundary, then FF-only NAS update and authorized master push/CI verification.
