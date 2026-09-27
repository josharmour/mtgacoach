# ArenaOnAir hosted service

`arenaonair.py` mounts `/api/arenaonair` in the website app. It is independent of
MTGA Coach's existing seven-day trial. Both products use `glm-5.3-flash`.

- `POST /trial`: provision a device trial using a client-generated possession
  token and SHA-256 device identifier. Only the token hash is stored server-side.
  A device identifier cannot recover someone else's token. Signup is throttled
  to ten new trials per address per day; only the address digest is retained.
- `GET /status`, `GET /v1/models`: authenticated access checks; no match consumed.
- `POST /v1/chat/completions`: requires the trial token and `X-ArenaOnAir-Match`.
  A SQLite transaction reserves one of five distinct match IDs. Reconnects and
  BO3 games share that ID. Failed first gateway requests release the reservation.
  Each match is bounded to four hours, 1,200 requests, and 60 requests/minute.
  Output is capped at 1,800 tokens and the model is selected server-side.

The desktop reads Arena's match ID. These are device trials, not verified human
identities: modified clients can spoof device and match IDs. Session bounds and
signup throttling limit abuse; they do not provide account-level anti-fraud.
The server accepts only the chat route and a request-field allowlist. LiteLLM
admin credentials stay inside the website service and are never issued as trial
credentials. Patreon customers use scoped LiteLLM keys directly at the gateway.

## Deployment

The live website is the `mtgacoach` container on blackwell, with build context
`/home/joshu/docker-stack/appdata/mtgacoach`. The deployed app is older than the
repository's router refactor. Copy the standalone module, add its router to the
live app, and update `PATRON_KEY_MODELS` to `["glm-5.3-flash"]`; avoid replacing the
whole live app with a refactored version as part of this change. Back up source
files first, copy the same files into the container, restart `mtgacoach`, and
rebuild `mtgacoach-site` from that context so recreations retain the change.

Tables are added idempotently to the existing website DB. Do not delete trial
rows to reset customer usage. Keep the DB volume and credentials across deploys.
Rollback removes the router include; additive trial tables can remain safely.

Validation: `tests/test_arenaonair_trial.py`, Patreon/key tests, then a synthetic
trial through the public TLS endpoint. Remove only the synthetic test trial.
The September 2026 migration changed 341 Patreon/Coach trial key model grants;
its rollback record is on the website data volume, never in Git.
