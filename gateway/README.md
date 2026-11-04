# Hosted inference gateway

A normal Coach installation sends authenticated requests to `https://api.mtgacoach.com/v1` using the model alias `current`. The customer needs a trial or subscriber key. The gateway operator chooses the inference deployment and handles model routing, limits, and billing records. Changing the underlying model does not require customers to edit their environment.

The public API is served by LiteLLM. The [website service](../website/SETUP.md) handles signup and subscription lifecycle. For a deliberate development override, see [DEV.md](../DEV.md).

## Operator checks

- Check `/v1/models` with a scoped customer key to verify the grants that client actually sees. A successful model listing verifies discovery. Use an authenticated inference request to test inference separately.
- Use the running LiteLLM container’s mounted configuration and database-backed settings when investigating live routing. The [tracked configuration](config.yaml) and [Compose file](docker-compose.yml) are historical deployment snapshots. Compare them with the deployment before using them.
- Keep admin credentials in the deployment’s secret store. Customer clients should receive only their scoped keys. Never copy a master key or unredacted authorization headers into an issue.

## Investigating latency and throttling

Check which component returned the error before changing any limits. A per-key LiteLLM parallel-request rejection, a coding admission queue timeout, and a model-server failure need different fixes. Compare request timestamps, authenticated model discovery, and backend activity. The [client call-class tests](../tests/test_proxy_call_classes.py), [priority tests](../tests/test_proxy_priority.py), and [metrics exporter](../tools/monitoring/litellm_exporter/README.md) cover different parts of this path.

The tracked [watchdog](vllm-watchdog/) and [scheduler patch](vllm-patches/) target a particular vLLM deployment. Check the active backend and service configuration before using either. [Historical incident measurements](https://github.com/josharmour/mtgacoach/blob/adbaca97f9076b8ee8be86689154e67410203911/gateway/README.md)

The ArenaOnAir trial API has a separate access policy. [ArenaOnAir service](../website/ARENAONAIR.md)

## Hosted Chatterbox speech

Coach's Chatterbox option always calls `https://api.mtgacoach.com/v1/chatterbox/speech`
and obtains its catalog from `/v1/chatterbox/voices`, using the existing Coach license
key. Legacy LAN URLs, speech key files, and local Chatterbox selections are ignored
by the desktop playback path. Kokoro remains a separate option.

The tunnel's narrow `^/v1/chatterbox/(speech|voices)$` rule precedes the existing
`api.mtgacoach.com` catch-all and targets the R9700 service at `10.0.0.10:18767`.
All other API paths retain the existing inference route. Neither a LiteLLM restart
nor changes to customer model grants are required.

`gateway/chatterbox_service.py` runs in the existing `arenaonair-rocm` environment
under `arenaonair-r9700-tts.service`. Its systemd override selects this entry point;
the existing GPU visibility variables keep synthesis on ROCm device 0 (R9700,
`gfx1201`, 32 GB). It verifies every hosted request against LiteLLM's authenticated
model listing and requires the `current` grant. Invalid or expired keys fail closed.
The original speech-key-protected LAN endpoint remains for ArenaOnAir compatibility.

The model generates short text segments successively. Each segment's 24 kHz mono
PCM is sent immediately; Coach's Qt audio sink starts while later segments are still
being generated. This is segment streaming, not token-level neural synthesis.
Cancellation closes the client stream and discards queued playback. The server
holds its single synthesis slot until a running segment finishes, then stops work
when the disconnected client is detected.

For rollback, remove only the `hosted-coach.conf` systemd override and restart the
speech service to restore its previous module. Remove only the Chatterbox-specific
tunnel rule. Leave the general API route untouched.
