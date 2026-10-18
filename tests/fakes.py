"""Shared test mock objects and fakes."""

import json
from typing import Any
from unittest.mock import patch


class FakeResponse:
    """Fake HTTP response for mock HTTP clients."""

    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeLiteLLM:
    """Stands in for LiteLLM HTTP client — records /key/generate and /key/delete."""

    def __init__(self, key_prefix: str = "sk-fake-"):
        self.minted = []
        self.deleted = []
        self.fail_alias_once = False
        self.key_prefix = key_prefix

    async def post(
        self, url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None, **kw
    ):
        if url.endswith("/key/generate"):
            alias = json.get("key_alias") if json else ""
            if self.fail_alias_once:
                self.fail_alias_once = False
                return FakeResponse(400, {"error": "alias already exists"})
            key = f"{self.key_prefix}{len(self.minted)}"
            self.minted.append({"alias": alias, "payload": json, "headers": headers or {}, "key": key})
            return FakeResponse(200, {"key": key})
        if url.endswith("/key/delete"):
            deleted_keys = json.get("keys", []) if json else []
            self.deleted.extend(deleted_keys)
            return FakeResponse(200, {"deleted_keys": deleted_keys})
        raise AssertionError(f"unexpected POST {url}")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


class VirtualClock:
    """Stands in for a module's ``time``: sleep() advances the clock instantly.

    Patch it over the module under test (``monkeypatch.setattr(module, "time",
    clock)``) so bounded waits and polls run in no wall time and their
    durations can be asserted exactly.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    time = perf_counter = monotonic

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


# ---------------------------------------------------------------------------
# Autopilot engine
# ---------------------------------------------------------------------------


class FakePlanner:
    """ActionPlanner stand-in that plans nothing and counts plan_actions calls."""

    def __init__(self):
        self._timeout = 0.1
        self._backend = object()
        self.plan_calls = 0

    def plan_actions(self, *args, **kwargs):
        from arenamcp.action_planner import ActionPlan

        self.plan_calls += 1
        return ActionPlan(actions=[], overall_strategy="noop")

    def get_recent_diagnostics(self) -> list[dict]:
        return []


class FakeBridge:
    """A GRE bridge that shows ``pending_response`` and refuses every submission."""

    def __init__(self, pending_response: dict | None = None, connected: bool = True):
        self.connected = connected
        self.pending_response = pending_response or {"ok": True, "has_pending": False}

    def connect(self) -> bool:
        return self.connected

    def get_pending_actions(self) -> dict:
        return self.pending_response

    def auto_respond(self) -> bool:
        return False

    def submit_pass(self) -> bool:
        return False

    def cancel_action(self) -> bool:
        return False

    def submit_mulligan(self, keep: bool) -> bool:
        return False

    def submit_choose_starting_player(self, seat_id: int) -> bool:
        return False

    def submit_action_by_match(self, **kwargs) -> bool:
        return False

    def submit_action_by_index(self, *args, **kwargs) -> bool:
        return False

    def submit_attackers(self, attackers) -> bool:
        return False

    def submit_attackers_raw(self, attackers):
        return None

    def submit_blockers(self, assignments) -> bool:
        return False

    def submit_targets(self, target_instance_id: int) -> bool:
        return False

    def submit_selection(self, ids: list[int]) -> bool:
        return False


class TypedBridge:
    """A connected bridge showing one scripted poll; records what the typed path submits."""

    def __init__(self, poll):
        self.connected = True
        self.poll_resp = poll
        self.submitted = []

    def connect(self):
        return True

    def get_pending_actions(self):
        return self.poll_resp

    def submit_targets(self, iid):
        self.submitted.append(("targets", iid))
        return True

    def submit_mulligan(self, keep):
        self.submitted.append(("mulligan", keep))
        return True

    def submit_selection(self, ids):
        self.submitted.append(("selection", ids))
        return True

    def submit_pass(self):
        self.submitted.append(("pass",))
        return True


class ScriptedBackend:
    """LLM backend that answers every call with ``response``; keeps the prompts."""

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, system, user, *args, **kwargs):
        self.calls += 1
        self.prompts.append(user)
        return self.response


def planner_with(response):
    """A bare ActionPlanner whose model always answers ``response``."""
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._backend = ScriptedBackend(response)
    planner._timeout = 5.0
    return planner


# A one-target SelectTargets window: the opponent's face (2) or a creature (161).
TARGET_POLL = {
    "has_pending": True,
    "request_type": "SelectTargets",
    "target_candidates": [
        {"targetInstanceId": 2, "grpId": 0},
        {"targetInstanceId": 161, "grpId": 91644},
    ],
    "target_selections": [{"minTargets": 1, "maxTargets": 1}],
}


def target_state(*, known_source=False):
    """The log snapshot that goes with TARGET_POLL (a Shock on the stack if ``known_source``)."""
    state = {
        "turn": {"turn_number": 5, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2}],
        "_bridge_connected": True,
        "_bridge_request_type": "SelectTargets",
        "pending_decision": "Select Targets",
        "battlefield": [],
        "hand": [],
    }
    if known_source:
        state["stack"] = [
            {"instance_id": 900, "name": "Shock", "oracle_text": "Shock deals 2 damage to target player."}
        ]
    return state


def make_engine(bridge=None, planner=None, *, game_state=None, dry_run=False, **options):
    """An AutopilotEngine on a fake bridge, with the real-time bridge waits turned off.

    ``bridge`` defaults to a disconnected FakeBridge and ``planner`` to a
    FakePlanner. ``game_state`` is the engine's state getter (a callable or a
    dict to copy). ``options`` are engine callbacks (``*_fn``) or
    AutopilotConfig fields. The 4 s bridge-reconnect wait and the 0.35 s
    "bridge lags the log" retry default to 0; pass ``bridge_reconnect_wait``
    to test the wait itself.
    """
    from arenamcp import autopilot as autopilot_module
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    callbacks = {key: options.pop(key) for key in list(options) if key.endswith("_fn")}
    config = AutopilotConfig(**{"dry_run": dry_run, "bridge_reconnect_wait": 0, **options})
    if bridge is None:
        bridge = FakeBridge(connected=False)
    if game_state is None:
        game_state = {}
    getter = game_state if callable(game_state) else (lambda: dict(game_state))
    with patch.object(autopilot_module, "get_bridge", lambda: bridge):
        engine = AutopilotEngine(
            planner=planner if planner is not None else FakePlanner(),
            get_game_state=getter,
            config=config,
            **callbacks,
        )
    engine._BRIDGE_CATCH_UP_WAIT_S = 0
    return engine
