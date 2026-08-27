"""MageZero RL Neural Value & Policy Client for MTGA Coach.

Communicates with the live MageZero inference server (running on localhost:50052
or LAN host 10.0.0.10:50052) to evaluate game state tensors, extract deep neural
network position values (V(s) in [-1, +1]), and retrieve policy priors.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
import urllib.request

logger = logging.getLogger(__name__)

# Fallback hosts to check for live MageZero inference
_DEFAULT_HOSTS = [
    os.environ.get("MAGEZERO_SERVER_URL", "").strip(),
    "https://api.mtgacoach.com/magezero",
    "http://127.0.0.1:50052",
    "http://10.0.0.10:50052",
]
_DEFAULT_HOSTS = [h for h in _DEFAULT_HOSTS if h]


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
    def check_health(cls, timeout: float = 0.5) -> bool:
        """Check if any MageZero inference server endpoint is reachable."""
        now = time.time()
        # Cache health check result for 5 seconds
        if cls._is_healthy and (now - cls._last_health_check) < 5.0:
            return True

        headers = _get_auth_headers()
        for host in _DEFAULT_HOSTS:
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
    def evaluate_game_state(cls, game_state: dict[str, Any]) -> dict[str, Any] | None:
        """Query MageZero inference server to score the game state tensor.

        Returns a dictionary with 'value' (scalar in [-1, +1]) and 'win_probability' (in [0.0, 1.0]),
        or None if the server is offline or request fails.
        """
        if not cls.check_health():
            return None

        host = cls._active_host or _DEFAULT_HOSTS[0]
        url = f"{host.rstrip('/')}/evaluate"

        try:
            import msgpack

            # Extract basic feature bag from game state
            indices = cls._extract_feature_indices(game_state)
            if not indices:
                return None

            payload = msgpack.packb({"indices": indices, "offsets": [0]}, use_bin_type=True)
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
                            "source": "magezero_nn",
                        }
        except Exception as e:
            logger.debug("MageZero evaluate call failed: %s", e)

        return None

    @staticmethod
    def _extract_feature_indices(game_state: dict[str, Any]) -> list[int]:
        """Convert game state into a bag-of-features index vector for MageZero."""
        indices: list[int] = []
        if not isinstance(game_state, dict):
            return indices

        # Feature hashing space (GLOBAL_MAX is 2048576)
        SPACE = 2000000

        # Encode turn, phase, life totals
        turn = game_state.get("turn") or {}
        t_num = int(turn.get("turn_number") or 1)
        indices.append(100 + min(50, t_num))

        players = game_state.get("players") or []
        local_seat = game_state.get("local_seat_id", 1)
        hero_life, opp_life = 20, 20
        for p in players:
            if isinstance(p, dict):
                if p.get("is_local") or p.get("seat_id") == local_seat:
                    hero_life = int(p.get("life_total") or 20)
                else:
                    opp_life = int(p.get("life_total") or 20)

        indices.append(200 + max(0, min(100, hero_life)))
        indices.append(400 + max(0, min(100, opp_life)))

        # Encode battlefield cards by GRP ID / name hash
        battlefield = game_state.get("battlefield") or []
        for card in battlefield:
            if not isinstance(card, dict):
                continue
            grp = int(card.get("grp_id") or 0)
            if not grp:
                name = str(card.get("name") or "")
                grp = abs(hash(name)) % 100000
            ctrl = card.get("controller_seat_id") or card.get("owner_seat_id")
            is_hero = ctrl == local_seat
            is_tapped = bool(card.get("is_tapped"))

            offset = 10000 if is_hero else 100000
            if is_tapped:
                offset += 50000
            indices.append((offset + grp) % SPACE)

        # Encode hand cards
        hand = game_state.get("hand") or []
        for card in hand:
            if not isinstance(card, dict):
                continue
            grp = int(card.get("grp_id") or 0)
            if not grp:
                name = str(card.get("name") or "")
                grp = abs(hash(name)) % 100000
            indices.append((500000 + grp) % SPACE)

        return sorted(set(indices))
