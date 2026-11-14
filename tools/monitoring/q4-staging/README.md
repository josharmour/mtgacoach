# Q4 staged server dashboard changes (not deployed)

These are artifacts for owner/verifier review, **not live configuration**.
Real server root is `/home/joshu/vllm-monitoring`; it is distinct from the similarly named NAS repository.

`coach_outcomes.py`, `dashboard_snapshot.py`, `gen_gateway_usage.py` and the original viewer source were matched by SHA256 to the live server by the board-review worker at 07:11 UTC November 14, 2026. Source copies came from the November 3 deployed evidence. `ai_performance_panels.py` was retrieved read-only from that server during this work. `api_cost.py` is the preserved generator dependency; pricing behavior is unchanged.

Changed server artifacts:
- `coach_outcomes.py`: backward-compatible old-schema reads plus deduplicated mode/format/deck views.
- `dashboard_snapshot.py`: separate autopilot, manual, unknown-mode and per-deck panels.
- `litellm-usage-viewer.patch`: minimal endpoint view routing patch against the verified source.
- `grafana/dashboards/gateway-usage.json`: full generated dashboard definition, with unrelated panels retained.

Unchanged generator dependencies are included solely to reproduce the JSON offline. To regenerate, run `python gen_gateway_usage.py` **inside this staging directory**, never the live server provisioning directory. `python example_query.py` produces synthetic fixture query output; `example-query-output.json` is the saved result.

The corresponding intake migration/extraction is in repository `website/match_bundles.py`. It adds nullable autopilot_enabled, format_name and deck_name columns. Legacy rows stay unknown; there is no automatic backfill or live DB mutation here. A deployed updated intake service would migrate its DB at normal init. Coordinate that eventual rollout with the dashboard server owner. Neither rollout nor service restart is performed by these artifacts.

Unknown or conflicting metadata is never inferred to be manual. Duplicate reports are resolved per user/match before dimension grouping. Existing user/totals views remain compatible. Deck name is a bounded strategy first line, not verified deck-list identity; missing or conflicting labels remain unknown. No external requests, inference or customer payloads are needed for tests.
