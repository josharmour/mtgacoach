#!/usr/bin/env bash
# The watchdog runs this when the vLLM container has been gone for two polls.
# The user unit owns the container (serve.sh's foreground `docker run --rm`),
# so restart the unit rather than starting a copy of an old config outside it.
exec systemctl --user -M joshu@ --no-block restart glm53-2x-rtxpro6000.service
