"""MageZero RL Neural Value & Policy Client for MTGA Coach.

Communicates with the live MageZero *coaching* inference server to evaluate game
state tensors, extract deep neural network position values (V(s) in [-1, +1]),
and retrieve policy priors.

Endpoint policy (task 01):
- Coaching never contacts the self-play / candidate-evaluation service
  (port 50052) automatically. Falling back to it would expose an unpromoted
  checkpoint and contend with the running training fleet.
- Default discovery targets the dedicated coaching server only
  (127.0.0.1:50054, plus 10.0.0.10:50054 when LAN opt-in is enabled).
- Operators can still aim coaching at any endpoint - including 50052 - for
  deliberate diagnostics via MAGEZERO_SERVER_URL.
- Discovery runs under an overall latency budget (MAGEZERO_DISCOVERY_BUDGET,
  default 2.5 seconds) and reports why it fell back via
  ``MageZeroClient.last_fallback_reason()``. No model discovery happens here.

Response validation (task 02):
- ``evaluate`` / ``evaluate_batch`` enforce a strict response contract and
  reject - as a whole, never element-wise - responses that are not exactly one
  fresh, finite, correctly shaped result per requested item. Accepted items are
  annotated with their request index (``request_index``) so a future versioned
  protocol can build on them.
- Rejection is binary: the caller falls back to the heuristic path and a
  structured reason is exposed via ``MageZeroClient.last_reject_reason()``.
"""

from __future__ import annotations

import logging
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Dedicated coaching endpoint(s). The self-play/candidate-evaluation service on
# port 50052 is intentionally absent: automatic fallback to it would expose an
# unpromoted model to coaching and contend with the live training fleet.
DEFAULT_ENDPOINTS: tuple[str, ...] = ("http://127.0.0.1:50054",)

# LAN coaching endpoints are only queried when the user explicitly opts in
# (MAGEZERO_ENABLE_LAN / ARENAMCP_LAN_EVAL) to avoid connect-timeout latency on
# user networks. Port 50052 is not here for the same fleet-isolation reason.
LAN_ENDPOINTS: tuple[str, ...] = ("http://10.0.0.10:50054",)


def _get_candidate_hosts() -> list[str]:
    """Return the ordered list of coach endpoints to probe.

    MAGEZERO_SERVER_URL, when set, is an explicit operator override and
    replaces the candidate list entirely (single endpoint, deliberate).
    """
    explicit = os.environ.get("MAGEZERO_SERVER_URL", "").strip()
    if explicit:
        return [explicit]

    hosts = list(DEFAULT_ENDPOINTS)
    lan_enabled = (
        os.environ.get("MAGEZERO_ENABLE_LAN", "").strip().lower() in ("1", "true", "yes")
        or os.environ.get("ARENAMCP_LAN_EVAL", "").strip().lower() in ("1", "true", "yes")
    )
    if lan_enabled:
        return list(LAN_ENDPOINTS) + hosts
    return hosts


def _discovery_budget() -> float:
    """Overall latency budget (seconds) for one discovery pass."""
    raw = os.environ.get("MAGEZERO_DISCOVERY_BUDGET", "").strip()
    try:
        budget = float(raw) if raw else 2.5
    except ValueError:
        budget = 2.5
    return max(0.1, min(30.0, budget))


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


def _finite_float(value: Any) -> float | None:
    """Convert ``value`` to a finite float, or return None (NaN/Inf/text/bool-ish)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if not math.isfinite(f):
        return None
    return f


def _finite_list(value: Any, width: int | None = None) -> list[float] | None:
    """Validate a (possibly 128-wide) numeric vector; None if malformed."""
    if not isinstance(value, list):
        return None
    if width is not None and len(value) != width:
        return None
    out: list[float] = []
    for item in value:
        f = _finite_float(item)
        if f is None:
            return None
        out.append(f)
    return out


def _validate_result(
    data_list: Any,
    expected: int,
    width: int,
    expected_model_id: str | None = None,
    expected_checkpoint_hash: str | None = None,
    served_meta: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Validate the decoded response synchronised to the request items."""
    if served_meta:
        served_model = served_meta.get("served_model_id") or served_meta.get("model_id")
        if expected_model_id and served_model and served_model != expected_model_id:
            return [], f"served-model-mismatch: expected {expected_model_id}, got {served_model}"
        served_hash = served_meta.get("served_checkpoint_hash") or served_meta.get("checkpoint_hash")
        if expected_checkpoint_hash and served_hash and served_hash != expected_checkpoint_hash:
            return [], f"served-checkpoint-mismatch: expected {expected_checkpoint_hash}, got {served_hash}"

    if not isinstance(data_list, list):
        return [], f"response-not-a-list ({type(data_list).__name__})"
    if len(data_list) != expected:
        return [], f"result-count {len(data_list)} != requested {expected}"

    # Ordering policy: request_index echo is all-or-none. A partially indexed
    # response (some rows echo, some omit) is rejected immediately — position
    # synthesis for the missing rows would create an unverified ordering claim.
    # A fully unindexed response is LEGACY COMPATIBILITY MODE: rows are synced
    # by position and NO verified ordering is claimed for it.
    echo_flags = [
        isinstance(item, dict) and "request_index" in item for item in data_list
    ]
    if any(echo_flags) and not all(echo_flags):
        missing = [i for i, flag in enumerate(echo_flags) if not flag]
        return [], (
            f"partial request_index echo: rows {missing} omit the index while "
            "others include it; all-or-none required"
        )
    has_request_index = all(echo_flags)
    results: list[dict[str, Any]] = []
    for i, item_data in enumerate(data_list):
        if not isinstance(item_data, dict):
            return [], f"result[{i}] not-a-dict ({type(item_data).__name__})"
        req_idx = item_data.get("request_index", i)
        if not isinstance(req_idx, int) or isinstance(req_idx, bool):
            return [], f"result[{i}] invalid request_index"
        if not 0 <= req_idx < expected:
            return [], f"result[{i}] request_index {req_idx} out of range 0..{expected - 1}"
        if has_request_index:
            if req_idx != i:
                return [], (
                    f"result[{i}] out of order: request_index {req_idx} (strict "
                    "request/response order required)"
                )
        value = _finite_float(item_data.get("value"))
        if value is None:
            return [], f"result[{i}] value missing/non-finite/non-numeric"
        if not -1.0 <= value <= 1.0:
            return [], f"result[{i}] value {value} outside contract [-1.0, 1.0]"
        policy_player = _finite_list(item_data.get("policy_player"), width)
        if policy_player is None:
            return [], f"result[{i}] policy_player missing/not-{width}-wide/non-finite"
        policy_opponent = _finite_list(item_data.get("policy_opponent"), width)
        if policy_opponent is None:
            return [], f"result[{i}] policy_opponent missing/not-{width}-wide/non-finite"
        win_p = max(0.0, min(1.0, (value + 1.0) / 2.0))
        results.append(
            {
                "request_index": req_idx,
                "value": value,
                "win_probability": round(win_p, 3),
                "policy_player": policy_player,
                "policy_opponent": policy_opponent,
                "source": "magezero_nn",
            }
        )
    return results, None


class MageZeroClient:
    """Client for MageZero neural net evaluation and position scoring."""

    _active_host: str | None = None
    _last_health_check: float = 0.0
    _is_healthy: bool = False
    _fallback_reason: str | None = None
    _reject_reason: str | None = None

    @classmethod
    def reset_health_cache(cls) -> None:
        """Clear cached health check state."""
        cls._last_health_check = 0.0
        cls._is_healthy = False
        cls._active_host = None
        cls._fallback_reason = None
        cls._reject_reason = None

    @classmethod
    def last_fallback_reason(cls) -> str | None:
        """Return why the last discovery pass found no healthy endpoint."""
        return cls._fallback_reason

    @classmethod
    def last_reject_reason(cls) -> str | None:
        """Return why the last response failed validation (None = accepted)."""
        return cls._reject_reason

    @classmethod
    def get_active_endpoint(cls) -> str | None:
        """Return the currently connected coaching endpoint, or None.

        Runs a (budget-bounded, cached) health/discovery pass when the cache
        is cold or stale; never contacts port 50052 (task 01 policy) and never
        blocks beyond the discovery budget. This is the single endpoint
        accessor for model-zoo discovery (task 05) — the host it returns is
        the same one ``evaluate``/``evaluate_batch`` will use, so manifest
        residency scoping and inference destination cannot disagree.
        """
        return cls._active_host if cls.check_health() else None

    @classmethod
    def check_health(cls, timeout: float = 0.5, force: bool = False) -> bool:
        """Check if any MageZero coaching endpoint is reachable.

        Caches positive health checks for 5 seconds and negative health checks
        for 15 seconds to prevent GUI / inference thread blocking.

        All candidates are probed under the overall discovery budget
        (MAGEZERO_DISCOVERY_BUDGET, default 2.5s); each attempt is additionally
        capped by ``timeout`` and the remaining budget so the caller never
        blocks longer than the budget.
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

        if not hosts:
            cls._is_healthy = False
            cls._active_host = None
            cls._last_health_check = now
            cls._fallback_reason = "no-coaching-endpoint-configured"
            logger.info("MageZero coaching disabled: no candidate endpoint configured")
            return False

        budget = _discovery_budget()
        started = time.monotonic()
        elapsed = 0.0
        for host in hosts:
            remaining = budget - elapsed
            if remaining <= 0.0:
                cls._is_healthy = False
                cls._active_host = None
                cls._last_health_check = now
                cls._fallback_reason = (
                    f"discovery-budget-exhausted ({elapsed:.2f}s budget {budget:.2f}s)"
                )
                logger.info(
                    "MageZero coaching unavailable: %s (last fallback reason)",
                    cls._fallback_reason,
                )
                return False
            attempt_timeout = max(0.05, min(timeout, remaining))
            try:
                url = f"{host.rstrip('/')}/healthz"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=attempt_timeout) as resp:
                    if resp.status in (200, 204):
                        cls._active_host = host
                        cls._is_healthy = True
                        cls._last_health_check = now
                        cls._fallback_reason = None
                        logger.debug("MageZero coaching server online at %s", host)
                        return True
            except Exception as exc:
                elapsed = time.monotonic() - started
                logger.debug("MageZero coaching endpoint %s failed: %s", host, exc)
                continue

        elapsed = time.monotonic() - started
        cls._is_healthy = False
        cls._active_host = None
        cls._last_health_check = now
        if elapsed >= budget:
            cls._fallback_reason = (
                f"discovery-budget-exhausted ({elapsed:.2f}s budget {budget:.2f}s)"
            )
        else:
            cls._fallback_reason = (
                f"no-healthy-coaching-endpoint; probed={hosts} ({elapsed:.2f}s)"
            )
        logger.info("MageZero coaching unavailable: %s", cls._fallback_reason)
        return False

    @classmethod
    def is_available(cls) -> bool:
        """Return True if a MageZero coaching server is currently connected."""
        return cls.check_health()

    @classmethod
    def evaluate(
        cls,
        game_state: dict[str, Any],
        opponent_hand_cards: list[str] | None = None,
        model_id: str | None = None,
        checkpoint_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """Score a game state using the MageZero neural inference server."""
        if not cls.check_health():
            return None

        host = cls._active_host
        if not host:
            return None
        url = f"{host.rstrip('/')}/evaluate"

        try:
            import msgpack
            from arenamcp.magezero_encoder import MageZeroStateEncoder

            indices = MageZeroStateEncoder.encode(game_state, opponent_hand_cards=opponent_hand_cards)
            if not indices:
                return None

            req_dict: dict[str, Any] = {"indices": indices}
            if model_id:
                req_dict["model"] = model_id
                req_dict["model_id"] = model_id
            if checkpoint_hash:
                req_dict["checkpoint_hash"] = checkpoint_hash
            payload = msgpack.packb(req_dict, use_bin_type=True)
            headers = _get_auth_headers()
            headers["Content-Type"] = "application/x-msgpack"
            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
            )

            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if resp.status != 200:
                    cls._reject_reason = f"http-status {resp.status}"
                    return None
                data = msgpack.unpackb(resp.read(), raw=False)
                served_meta = data if isinstance(data, dict) else None
                row_data = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [data])
                results, reject_reason = _validate_result(
                    row_data,
                    expected=1,
                    width=128,
                    expected_model_id=model_id,
                    expected_checkpoint_hash=checkpoint_hash,
                    served_meta=served_meta,
                )
                if reject_reason:
                    cls._reject_reason = reject_reason
                    logger.info("MageZero /evaluate rejected: %s", reject_reason)
                    return None
                cls._reject_reason = None
                return results[0]
        except urllib.error.HTTPError as e:
            cls._reject_reason = f"http-status {e.code}"
            logger.debug("MageZero evaluate call failed: %s", e)
        except Exception as e:
            cls._reject_reason = f"transport-error: {type(e).__name__}"
            logger.debug("MageZero evaluate call failed: %s", e)

        return None

    @classmethod
    def evaluate_batch(
        cls,
        items: list[tuple[dict[str, Any], list[str] | None]],
        model_id: str | None = None,
        checkpoint_hash: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Score multiple game states / afterstates in a single batched HTTP request.

        Returns validated results (one per requested item, request/response
        order locked by request_index) or None if the response fails the
        contract. ``MageZeroClient.last_reject_reason()`` explains rejections.
        """
        if not items or not cls.check_health():
            return None

        host = cls._active_host
        if not host:
            return None
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

            req_dict: dict[str, Any] = {
                "indices": all_indices,
                "offsets": offsets,
                "items": [{"request_index": i, "offset": offsets[i]} for i in range(len(items))],
            }
            if model_id:
                req_dict["model"] = model_id
                req_dict["model_id"] = model_id
            if checkpoint_hash:
                req_dict["checkpoint_hash"] = checkpoint_hash
            payload = msgpack.packb(req_dict, use_bin_type=True)
            headers = _get_auth_headers()
            headers["Content-Type"] = "application/x-msgpack"
            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
            )

            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status != 200:
                    cls._reject_reason = f"http-status {resp.status}"
                    return None
                raw_data = msgpack.unpackb(resp.read(), raw=False)
                served_meta = raw_data if isinstance(raw_data, dict) else None
                row_data = raw_data.get("results", []) if isinstance(raw_data, dict) else raw_data
                results, reject_reason = _validate_result(
                    row_data,
                    expected=len(items),
                    width=128,
                    expected_model_id=model_id,
                    expected_checkpoint_hash=checkpoint_hash,
                    served_meta=served_meta,
                )
                if reject_reason:
                    cls._reject_reason = reject_reason
                    logger.info("MageZero /evaluate rejected: %s", reject_reason)
                    return None
                cls._reject_reason = None
                return results
        except urllib.error.HTTPError as e:
            cls._reject_reason = f"http-status {e.code}"
            logger.debug("MageZero evaluate_batch call failed: %s", e)
        except Exception as e:
            cls._reject_reason = f"transport-error: {type(e).__name__}"
            logger.debug("MageZero evaluate_batch call failed: %s", e)

        return None
