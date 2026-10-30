"""Authenticated, bounded model discovery for the configured hosted gateway."""

import hashlib
import json
import time
import urllib.error
import urllib.request

_cache = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def current_models(base_url, key, headers):
    """Never select an arbitrary backend or send a key to a redirected host.

    A cached catalog lasts five minutes. Transport failures retain the stable
    current contract; auth failures or an authoritative missing alias return no
    selection, so they cannot silently select a different provider or model.
    """
    cache_key = (base_url.rstrip("/"), hashlib.sha256(key.encode()).hexdigest())
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < 300:
        return list(cached[1])
    if not key:
        return []
    request_headers = dict(headers, Authorization="Bearer " + key)
    req = urllib.request.Request(cache_key[0] + "/models", headers=request_headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=3) as response:
            data = json.loads(response.read(131073))
        found = any(isinstance(m, dict) and m.get("id") == "current" for m in data.get("data", []))
        result = [("Current hosted model", "current")] if found else []
    except urllib.error.HTTPError:
        _cache.pop(cache_key, None)
        return []
    except (OSError, ValueError, TypeError, AttributeError):
        # The model name is stable even when discovery is temporarily offline.
        result = list(cached[1]) if cached else [("Current hosted model (discovery unavailable)", "current")]
        _cache[cache_key] = (now - 270, result)  # retry after 30 seconds
        return result
    _cache[cache_key] = (now, result)
    return list(result)
