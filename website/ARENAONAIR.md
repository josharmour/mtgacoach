# ArenaOnAir hosted service

`arenaonair.py` mounts `/api/arenaonair` in the website app. It is independent of
MTGA Coach's existing seven-day trial. Coach requests the gateway alias `current`. The ArenaOnAir trial router selects its model server-side using `ARENAONAIR_MODEL`.

- `POST /trial/request-code`: request an emailed six-digit code, bound to the
  device, email, and client possession token. Codes expire after 15 minutes,
  allow five attempts, and are invalidated by resends. Shared code-request
  throttles allow three requests/device/hour and twenty/network/hour. SMTP
  must be configured; delivery failures do not activate trials.
- `POST /trial`: verify email/code and provision a device trial using the same
  client-generated possession token and SHA-256 device identifier. Existing
  trials with their original token are grandfathered. Only token and code
  digests are stored server-side; the verified email is retained with the trial.
  A device identifier cannot recover someone else's token. Signup is throttled
  to ten new trials per address per day; only the address digest is retained.
- `GET /status`, `GET /v1/models`: authenticated access checks; no match consumed.
- `POST /v1/chat/completions`: requires the trial token and `X-ArenaOnAir-Match`.
  A SQLite transaction reserves one of five distinct match IDs. Reconnects and
  BO3 games share that ID. Failed first gateway requests release the reservation.
  Each match is bounded to four hours, 1,200 requests, and 60 requests/minute.
  Output is capped at 1,800 tokens and the model is selected server-side.

The desktop reads Arena's match ID. Email verification proves access to an inbox, not a unique human
identity: modified clients can spoof device and match IDs. Session bounds and
signup throttling limit abuse; they do not provide account-level anti-fraud.
The server accepts only the chat route and a request-field allowlist. LiteLLM
admin credentials stay inside the website service and are never issued as trial
credentials. Patreon customers use scoped LiteLLM keys directly at the gateway.

## Deployment and verification

Use the [website guide](SETUP.md) for the service and test environment. Confirm the deployed router configuration and scoped key grants before applying a change. Preserve the persistent website database across deployments.

Exercise `tests/test_arenaonair_trial.py` and the Patreon/key tests. Use a synthetic trial to verify the public TLS endpoint, email-code flow, and match accounting. Delete only the synthetic test data.

The router initializes its trial tables and preserves existing allowances. Keep the router and mail helpers compatible when changing the email flow. The server bounds reply size and controls the model and reasoning options; see [arenaonair.py](arenaonair.py) for the current limits.
