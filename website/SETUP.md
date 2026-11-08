# Website and trial development

The FastAPI app serves the Coach website, trial signup, Patreon integration, and the ArenaOnAir trial routes. The desktop app sends hosted model requests to the [LiteLLM gateway](../gateway/README.md). Keep the public website and public inference API routed to their respective services.

## Work on the website locally

From the repository root, set up the [development environment](../DEV.md). The website uses the dependencies in [requirements.txt](requirements.txt). Run the relevant tests with their isolated databases and mocked providers.

```sh
uv run --no-sync python -m pytest tests/test_website_pages.py tests/test_trial_endpoint.py tests/test_patreon_litellm.py tests/test_website_subscription.py -q
```

For a local server, configure development values using [.env.example](.env.example) and [config.yaml](config.yaml). From the `website` directory, start the service with `docker compose up --build`. The [Compose file](docker-compose.yml) exposes port 8443 and mounts the database directory. Use separate development credentials and data when exercising signup or Patreon callbacks.

## Transactional email

Trial verification codes, Patreon license-key delivery, and trial-expiration notices default to `support@mtgacoach.com` as the sender. All three set `Reply-To: support@mtgacoach.com`, including when the SMTP login uses another address. Verify the sender with the SMTP provider before enabling delivery. `SMTP_FROM` can override the From address for deployments using a different verified sender; replies still go to support. Configure inbound forwarding separately from SMTP delivery.

Run `python -m pytest tests/test_transactional_email.py -q` to check the sender, reply address, and message delivery using a mocked SMTP connection. These tests send no real email and mint no keys.

## Check a deployment

Verify the public landing page, its platform download links, and the website’s `/health` route. For account changes, also test email-code verification, trial issuance, expiration, and subscriber access with a test account. A healthy website alone does not establish that an inference request will succeed.

Production uses a separately managed container configuration. Confirm the live image, mounts, and persistent data before a deployment. Keep provider, mail, Patreon, and gateway credentials in the deployment environment. For ArenaOnAir match accounting and verification, see [ARENAONAIR.md](ARENAONAIR.md).

## Account recovery security

Patreon sign-in starts at `/patreon/login`. The callback requires the browser's
short-lived, secure OAuth state cookie, a verified Patreon email, and an active
membership linked to campaign `1310866` (the official mtgacoach campaign).
`PATREON_CAMPAIGN_ID` can explicitly override that ID for a separate deployment.
Missing membership information fails closed. Webhook gateway revocation errors
return a retryable failure without marking the local subscriber revoked.

A machine ID is an identifier, never proof of trial ownership. Active trial
recovery requires the existing bearer key or a fresh single-use email code for
the original trusted signup email. The database migration adds
`recovery_email_verified`, defaulting false for older records because the former
email-backfill endpoint could mark unverified input as verified. Existing cached
keys remain usable. Historical lost-key cases require support and independently
verified ownership; do not promote historical email flags in bulk.

These checks prevent future unauthorized recovery. They do not establish whether
previously returned bearer keys were copied or invalidate any such copies.
