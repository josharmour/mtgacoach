"""MageZero RL Neural Value & Policy Client for MTGA Coach.

Communicates with the live MageZero inference server (running on localhost:50052
or LAN host 10.0.0.10:50052) to evaluate game state tensors, extract deep neural
network position values (V(s) in [-1, +1]), and retrieve policy priors.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Fallback hosts to check for live MageZero inference
def _get_candidate_hosts() -> list[str]:
    explicit = os.environ.get("MAGEZERO_SERVER_URL", "").strip()
    if explicit:
        return [explicit]

    hosts = [
        "http://127.0.0.1:50054",
        "http://127.0.0.1:50052",
    ]
    # LAN addresses on Blackwell R9700 are only queried if explicitly enabled
    # to avoid connect-timeout latency on customer networks
    lan_enabled = (
        os.environ.get("MAGEZERO_ENABLE_LAN", "").strip().lower() in ("1", "true", "yes")
        or os.environ.get("ARENAMCP_LAN_EVAL", "").strip().lower() in ("1", "true", "yes")
    )
    if lan_enabled:
        hosts.insert(0, "http://10.0.0.10:50054")
        hosts.insert(1, "http://10.0.0.10:50052")
    return hosts


def _get_auth_headers() -> dict[str, str]:
    headers = {"User-Agent": "MtgACoach/2.7"}
    try:
        from arenamcp.settings import get_settings

        key = get_settings().get("license_key") or os.environ.get("MTGACOACH_LICENSE_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key.strip()}"
    except Exception:
        pass
    return headers


class MageZeroClient:
    """Client for MageZero neural net evaluation and position scoring."""

    _active_host: str | None = None
    _last_health_check: float = 0.0
    _is_healthy: bool = False

    @classmethod
    def reset_health_cache(cls) -> None:
        """Clear cached health check state."""
        cls._last_health_check = 0.0
        cls._is_healthy = False
        cls._active_host = None

    @classmethod
    def check_health(cls, timeout: float = 0.5, force: bool = False) -> bool:
        """Check if any MageZero inference server endpoint is reachable.

        Caches positive health checks for 5 seconds and negative health checks
        for 15 seconds to prevent GUI / inference thread blocking.
        """
        now = time.time()
        if not force:
            if cls._is_healthy and (now - cls._last_health_check) < 5.0:
                return True
            if not cls._is_healthy and (now - cls._last_health_check) < 15.0:
                return False

        headers = _get_auth_headers()
        hosts = _get_candidate_hosts()
        # If an active host was previously healthy, try it first
        if cls._active_host and cls._active_host in hosts:
            hosts.remove(cls._active_host)
            hosts.insert(0, cls._active_host)

        for host in hosts:
            try:
                url = f"{host.rstrip('/')}/healthz"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status in (200, 204):
                        cls._active_host = host
                        cls._is_healthy = True
                        cls._last_health_check = now
                        logger.debug("MageZero inference server online at %s", host)
                        return True
            except Exception:
                continue

        cls._is_healthy = False
        cls._active_host = None
        cls._last_health_check = now
        return False

    @classmethod
    def is_available(cls) -> bool:
        """Return True if MageZero server is currently connected and healthy."""
        return cls.check_health()

    @classmethod
    def evaluate(
        cls,
        game_state: dict[str, Any],
        opponent_hand_cards: list[str] | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Score a game state using the MageZero neural inference server."""
        if not cls.check_health():
            return None

        hosts = _get_candidate_hosts()
        host = cls._active_host or (hosts[0] if hosts else "http://127.0.0.1:50054")
        url = f"{host.rstrip('/')}/evaluate"

        try:
            import msgpack
            from arenamcp.magezero_encoder import MageZeroStateEncoder

            indices = MageZeroStateEncoder.encode(game_state, opponent_hand_cards=opponent_hand_cards)
            if not indices:
                return None

            req_dict = {"indices": indices}
            if model_id:
                req_dict["model"] = model_id
            payload = msgpack.packb(req_dict, use_bin_type=True)
            headers = _get_auth_headers()
            headers["Content-Type"] = "application/x-msgpack"
            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
            )

            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if resp.status == 200:
                    data = msgpack.unpackb(resp.read(), raw=False)
                    if isinstance(data, dict):
                        raw_val = float(data.get("value", 0.0))
                        win_p = max(0.02, min(0.98, (raw_val + 1.0) / 2.0))
                        return {
                            "value": raw_val,
                            "win_probability": round(win_p, 3),
                            "policy_player": data.get("policy_player", []),
                            "policy_opponent": data.get("policy_opponent", []),
                            "source": "magezero_nn",
                        }
        except Exception as e:
            logger.debug("MageZero evaluate call failed: %s", e)

        return None

    @classmethod
    def evaluate_batch(
        cls,
        items: list[tuple[dict[str, Any], list[str] | None]],
        model_id: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Score multiple game states / afterstates in a single batched HTTP request.

        Args:
            items: List of (game_state, opponent_hand_cards) tuples.
            model_id: Optional model_id to route evaluation to on multi-model inference servers.

        Returns:
            List of result dicts each containing 'value', 'win_probability', 'policy_player',
            or None if request fails.
        """
        if not items or not cls.check_health():
            return None

        hosts = _get_candidate_hosts()
        host = cls._active_host or (hosts[0] if hosts else "http://127.0.0.1:50054")
        url = f"{host.rstrip('/')}/evaluate"

        try:
            import msgpack
            from arenamcp.magezero_encoder import MageZeroStateEncoder

            all_indices: list[int] = []
            offsets: list[int] = []

            for g_state, opp_hand in items:
                offsets.append(len(all_indices))
                ind = MageZeroStateEncoder.encode(g_state, opponent_hand_cards=opp_hand)
                all_indices.extend(ind)

            if not all_indices:
                return None

            req_dict = {"indices": all_indices, "offsets": offsets}
            if model_id:
                req_dict["model"] = model_id
            payload = msgpack.packb(req_dict, use_bin_type=True)
            headers = _get_auth_headers()
            headers["Content-Type"] = "application/x-msgpack"
            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
            )

            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    raw_data = msgpack.unpackb(resp.read(), raw=False)
                    results: list[dict[str, Any]] = []
                    data_list = raw_data if isinstance(raw_data, list) else [raw_data]
                    for item_data in data_list:
                        if isinstance(item_data, dict):
                            raw_val = float(item_data.get("value", 0.0))
                            win_p = max(0.02, min(0.98, (raw_val + 1.0) / 2.0))
                            results.append(
                                {
                                    "value": raw_val,
                                    "win_probability": round(win_p, 3),
                                    "policy_player": item_data.get("policy_player", []),
                                    "policy_opponent": item_data.get("policy_opponent", []),
                                    "source": "magezero_nn",
                                }
                            )
                    return results
        except Exception as e:
            logger.debug("MageZero evaluate_batch call failed: %s", e)

        return None
