# Website and trial development

The FastAPI app serves the Coach website, trial signup, Patreon integration, and the ArenaOnAir trial routes. The desktop app sends hosted model requests to the [LiteLLM gateway](../gateway/README.md). Keep the public website and public inference API routed to their respective services.

## Work on the website locally

From the repository root, set up the [development environment](../DEV.md). The website uses the dependencies in [requirements.txt](requirements.txt). Run the relevant tests with their isolated databases and mocked providers.

```sh
uv run --no-sync python -m pytest tests/test_website_pages.py tests/test_trial_endpoint.py tests/test_patreon_litellm.py tests/test_website_subscription.py -q
```

For a local server, configure development values using [.env.example](.env.example) and [config.yaml](config.yaml). From the `website` directory, start the service with `docker compose up --build`. The [Compose file](docker-compose.yml) exposes port 8443 and mounts the database directory. Use separate development credentials and data when exercising signup or Patreon callbacks.

## Check a deployment

Verify the public landing page, its platform download links, and the website’s `/health` route. For account changes, also test email-code verification, trial issuance, expiration, and subscriber access with a test account. A healthy website alone does not establish that an inference request will succeed.

Production uses a separately managed container configuration. Confirm the live image, mounts, and persistent data before a deployment. Keep provider, mail, Patreon, and gateway credentials in the deployment environment. For ArenaOnAir match accounting and verification, see [ARENAONAIR.md](ARENAONAIR.md).
