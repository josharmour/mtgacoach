"""Fixed autopilot sleeps became ~30 ms polls on the condition, bounded by the old sleep.

2026-10-11 audit: the GRE answers an UpdateAttacker in ~24 ms, yet the
autopilot slept 0.4 s (attackers), 0.8 s (blockers), 0.35 s (bridge retry),
0.4 s (post-action) and 0.25 s (between actions) before looking.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from arenamcp import autopilot_bridge
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_models import AutopilotConfig
from arenamcp.combat_targets import recipient_key


class _Feed:
    """get_pending_actions answers after ``delay`` seconds switch from ``before`` to ``after``."""

    def __init__(self, before, after, delay):
        self.before, self.after = before, after
        self.switch_at = time.monotonic() + delay
        self.calls = 0

    def get_pending_actions(self):
        self.calls += 1
        return self.after if time.monotonic() >= self.switch_at else self.before


def _engine(bridge):
    engine = AutopilotEngine(
        planner=MagicMock(),
        mapper=MagicMock(),
        controller=None,
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=True),
    )
    engine._gre_bridge = bridge
    return engine


IDLE = {"ok": True, "has_pending": False}
PENDING = {"ok": True, "has_pending": True, "request_type": "ActionsAvailable"}


def test_bridge_poll_returns_as_soon_as_a_request_is_pending():
    engine = _engine(_Feed(IDLE, PENDING, 0.05))
    started = time.monotonic()
    assert engine._poll_bridge_for_pending(0.35) is PENDING
    assert time.monotonic() - started < 0.2


def test_bridge_poll_is_bounded_by_the_old_sleep():
    feed = _Feed(IDLE, IDLE, 0.0)
    engine = _engine(feed)
    started = time.monotonic()
    assert engine._poll_bridge_for_pending(0.2) is IDLE
    assert 0.18 <= time.monotonic() - started < 0.4 and feed.calls >= 3


def _blocker_request(gsid, msg):
    return {"has_pending": True, "request_class": "DeclareBlockersRequest", "game_state_id": gsid, "msg_id": msg}


def test_blockers_finalize_as_soon_as_the_request_is_reissued():
    old, new = _blocker_request(10, 1), _blocker_request(11, 2)
    started = time.monotonic()
    assert autopilot_bridge._await_blockers_refresh(_Feed(old, new, 0.05), old) is new
    assert time.monotonic() - started < 0.4  # was a fixed 0.8 s


def test_blockers_wait_the_full_bound_without_a_reissued_request():
    old = _blocker_request(10, 1)
    feed = _Feed(old, IDLE, 0.02)  # an empty poll is not proof of anything
    started = time.monotonic()
    assert autopilot_bridge._await_blockers_refresh(feed, old) is IDLE
    assert time.monotonic() - started >= autopilot_bridge._BLOCK_ACK_MAX_WAIT_S - 0.01


def _attack_entry():
    return {"attackerInstanceId": 5, "damageRecipient": {"playerSystemSeatId": 2}}


def _attack_request(gsid, msg, selected):
    entry = dict(_attack_entry())
    if selected:
        entry["selectedDamageRecipient"] = entry["damageRecipient"]
    return {
        "has_pending": True,
        "request_class": "DeclareAttackerRequest",
        "game_state_id": gsid,
        "msg_id": msg,
        "attackers": [entry],
        "can_submit": True,
    }


def _await_attack(feed, native=True):
    engine = SimpleNamespace(_abort_event=None)
    return autopilot_bridge._BridgeSubmitMixin._await_attack_selection(
        engine, feed, (10, 1), [_attack_entry()], native, require_submit=True
    )


def test_attack_acknowledgment_returns_on_the_reissued_request():
    assert recipient_key(_attack_entry()["damageRecipient"])  # the comparison the wait makes
    started = time.monotonic()
    verdict, key = _await_attack(_Feed(_attack_request(10, 1, False), _attack_request(11, 2, True), 0.05))
    assert (verdict, key) == ("acknowledged", (11, 2))
    assert time.monotonic() - started < 0.35  # was at least 0.4 s


def test_an_attack_request_gone_for_a_moment_is_not_read_as_advanced():
    # Between the GRE's messages the request is briefly absent: before 0.4 s
    # that must not end the wait; the re-issued request then acknowledges.
    verdict, _ = _await_attack(_Feed(IDLE, _attack_request(11, 2, True), 0.1))
    assert verdict == "acknowledged"


def test_an_attack_request_that_stays_gone_is_advanced_after_the_settle_time():
    started = time.monotonic()
    verdict, _ = _await_attack(_Feed(IDLE, IDLE, 0.0))
    elapsed = time.monotonic() - started
    assert verdict == "advanced"
    assert autopilot_bridge._COMBAT_ACK_SETTLE_S - 0.01 <= elapsed < 0.8


def test_verification_sees_a_bridge_state_advance_without_the_fixed_delay():
    engine = _engine(MagicMock())
    states = iter([{"_bridge_game_state_id": 7}, {"_bridge_game_state_id": 8}])
    engine._get_game_state = lambda: next(states, {"_bridge_game_state_id": 8})
    action = SimpleNamespace(card_name="", action_type=None)
    started = time.monotonic()
    assert engine._verify_action(action, {"_bridge_game_state_id": 7}) is True
    assert time.monotonic() - started < engine._config.post_action_delay
