"""Fixed autopilot sleeps became ~30 ms polls on the condition, bounded by the old sleep.

2026-10-11 audit: the GRE answers an UpdateAttacker in ~24 ms, yet the
autopilot slept 0.4 s (attackers), 0.8 s (blockers), 0.35 s (bridge retry),
0.4 s (post-action) and 0.25 s (between actions) before looking.

The waits run on a virtual clock, so each bound is asserted exactly and no
test spends wall time (or flakes under load).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.fakes import VirtualClock, make_engine

from arenamcp import autopilot, autopilot_bridge
from arenamcp.combat_targets import recipient_key


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = VirtualClock()
    monkeypatch.setattr(autopilot, "time", clock)
    monkeypatch.setattr(autopilot_bridge, "time", clock)
    return clock


class _Feed:
    """get_pending_actions answers ``after`` once ``delay`` seconds have passed, ``before`` until then."""

    def __init__(self, clock, before, after, delay):
        self.clock = clock
        self.before, self.after = before, after
        self.switch_at = clock.monotonic() + delay
        self.calls = 0

    def get_pending_actions(self):
        self.calls += 1
        return self.after if self.clock.monotonic() >= self.switch_at else self.before


def _engine(bridge):
    return make_engine(bridge, MagicMock(), dry_run=True)


IDLE = {"ok": True, "has_pending": False}
PENDING = {"ok": True, "has_pending": True, "request_type": "ActionsAvailable"}


def test_bridge_poll_returns_as_soon_as_a_request_is_pending(clock):
    engine = _engine(_Feed(clock, IDLE, PENDING, 0.05))
    started = clock.now
    assert engine._poll_bridge_for_pending(0.35) is PENDING
    assert clock.now - started < 0.05 + engine._BRIDGE_POLL_S + 1e-9


def test_bridge_poll_is_bounded_by_the_old_sleep(clock):
    feed = _Feed(clock, IDLE, IDLE, 0.0)
    engine = _engine(feed)
    started = clock.now
    assert engine._poll_bridge_for_pending(0.2) is IDLE
    assert clock.now - started == pytest.approx(0.2) and feed.calls >= 3


def _blocker_request(gsid, msg):
    return {
        "has_pending": True,
        "request_class": "DeclareBlockersRequest",
        "game_state_id": gsid,
        "msg_id": msg,
    }


def test_blockers_finalize_as_soon_as_the_request_is_reissued(clock):
    old, new = _blocker_request(10, 1), _blocker_request(11, 2)
    started = clock.now
    assert autopilot_bridge._await_blockers_refresh(_Feed(clock, old, new, 0.05), old) is new
    assert clock.now - started < 0.05 + autopilot_bridge._COMBAT_ACK_POLL_S + 1e-9  # was a fixed 0.8 s


def test_blockers_wait_the_full_bound_without_a_reissued_request(clock):
    old = _blocker_request(10, 1)
    feed = _Feed(clock, old, IDLE, 0.02)  # an empty poll is not proof of anything
    started = clock.now
    assert autopilot_bridge._await_blockers_refresh(feed, old) is IDLE
    assert clock.now - started == pytest.approx(autopilot_bridge._BLOCK_ACK_MAX_WAIT_S)


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


def test_attack_acknowledgment_returns_on_the_reissued_request(clock):
    assert recipient_key(_attack_entry()["damageRecipient"])  # the comparison the wait makes
    started = clock.now
    feed = _Feed(clock, _attack_request(10, 1, False), _attack_request(11, 2, True), 0.05)
    verdict, key = _await_attack(feed)
    assert (verdict, key) == ("acknowledged", (11, 2))
    assert clock.now - started < autopilot_bridge._COMBAT_ACK_SETTLE_S  # was at least 0.4 s


def test_an_attack_request_gone_for_a_moment_is_not_read_as_advanced(clock):
    # Between the GRE's messages the request is briefly absent: before the
    # settle time that must not end the wait; the re-issued request then acknowledges.
    verdict, _ = _await_attack(_Feed(clock, IDLE, _attack_request(11, 2, True), 0.1))
    assert verdict == "acknowledged"


def test_an_attack_request_that_stays_gone_is_advanced_after_the_settle_time(clock):
    started = clock.now
    verdict, _ = _await_attack(_Feed(clock, IDLE, IDLE, 0.0))
    elapsed = clock.now - started
    assert verdict == "advanced"
    settle = autopilot_bridge._COMBAT_ACK_SETTLE_S
    assert settle <= elapsed < settle + autopilot_bridge._COMBAT_ACK_POLL_S + 1e-9


def test_verification_sees_a_bridge_state_advance_without_the_fixed_delay(clock):
    engine = _engine(MagicMock())
    states = iter([{"_bridge_game_state_id": 7}, {"_bridge_game_state_id": 8}])
    engine._get_game_state = lambda: next(states, {"_bridge_game_state_id": 8})
    action = SimpleNamespace(card_name="", action_type=None)
    started = clock.now
    assert engine._verify_action(action, {"_bridge_game_state_id": 7}) is True
    assert clock.now - started < engine._config.post_action_delay
