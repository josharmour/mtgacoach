"""Narrated draft picks: select the cards, say why, pause, then confirm (2026-10-07).

Live PickTwoDraft_FRA: "Draft autoplay: P1p1: took Twinned Vision and Extended
Absence (model)" was logged and the cards were gone before "Taking Twinned
Vision and Extended Absence: ..." was spoken. With commentary on, a pick is now
selected in Arena first (viewers see it), explained, and confirmed after a
beat; the hold is bounded by the pick clock so a draft never stalls.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from tests.test_draft_autoplay import PICK_STATE, draft_world, driver_for

from arenamcp import draft_event
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.speech_completion import SpeechCompletion, narrate_and_wait


@pytest.fixture
def primer():
    from tests.test_draft_autoplay import PAIRS, RATINGS

    from arenamcp.set_primer import data_primer

    return data_primer("TST", RATINGS, PAIRS, lambda grp, name: {"oracle_text": f"{name} text", "cmc": 3.0})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Bridge:
    """Records each command with the (fake) time it was sent."""

    connected = True

    def __init__(self, clock: Clock, events: list, replies: dict) -> None:
        self.clock, self.events, self.replies = clock, events, replies

    def draft_command(self, action, **fields):
        self.events.append((action, self.clock.now, fields))
        reply = self.replies.get(action, {"ok": False, "error": "unscripted"})
        return reply(fields) if callable(reply) else reply


def state(seconds=60, **changes) -> dict:
    return {**PICK_STATE, "pick_seconds_remaining": seconds, **changes}


def world(primer, *, seconds=60, replies=None, narrate=None, commentary=True, **kwargs):
    clock, events, spoken = Clock(), [], []
    base = {
        "get_screen": {"ok": True, "draft": True},
        "get_draft_state": state(seconds),
        "preview_draft_pick": {"ok": True, "previewed": True},
        "confirm_draft_pick": {"ok": True},
        "submit_draft_pick": {"ok": True},
    }
    bridge = Bridge(clock, events, {**base, **(replies or {})})

    def default_narrate(text, cancelled):
        events.append(("narrate", clock.now, {"text": text}))
        clock.now += 4.0  # the desktop reports "finished" four seconds later
        return True

    driver = driver_for(
        bridge,
        primer,
        speak_fn=spoken.append,
        commentary_fn=lambda: commentary,
        narrate_fn=narrate or default_narrate,
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )
    return SimpleNamespace(clock=clock, events=events, spoken=spoken, bridge=bridge, driver=driver)


def actions(w) -> list[str]:
    return [name for name, _at, _fields in w.events]


def at(w, name: str) -> float:
    return next(when for action, when, _f in w.events if action == name)


def test_pick_is_selected_explained_then_confirmed_after_a_beat(primer):
    w = world(primer)
    w.driver._step()
    assert actions(w) == [
        "get_screen",
        "get_draft_state",
        "preview_draft_pick",
        "narrate",
        "get_draft_state",
        "confirm_draft_pick",
    ]
    text = next(f["text"] for a, _t, f in w.events if a == "narrate")
    assert text.startswith("Choosing Blue Ace: ")
    # Confirmed only after the narration finished, plus a one-second beat.
    assert at(w, "confirm_draft_pick") - at(w, "narrate") >= 4.0 + draft_event.PICK_CONFIRM_BEAT_S
    confirm = next(f for a, _t, f in w.events if a == "confirm_draft_pick")
    assert confirm["cards"] == [{"grp_id": 1, "title_id": 10}]
    assert w.driver.run.picks[-1]["grp_ids"] == [1] and w.driver.run.preview is None
    assert w.spoken == []  # nothing is said after the cards are gone


def test_commentary_off_picks_at_once_without_selecting_or_waiting(primer):
    w = world(primer, commentary=False)
    w.driver._step()
    assert actions(w) == ["get_screen", "get_draft_state", "submit_draft_pick"]
    assert w.clock.now == 1000.0 and w.spoken == ["Taking Blue Ace."]


def test_muted_voice_holds_for_reading_time_but_never_past_the_cap(primer):
    def muted(text, cancelled):
        w.events.append(("narrate", w.clock.now, {"text": text}))
        long_text = text + " word" * 80  # ~37 s of reading time
        return narrate_and_wait(
            long_text, send=None, completion=None, cancelled=cancelled, muted=True, sleep=w.clock.sleep
        )

    w = world(primer, narrate=muted)
    start = w.clock.now
    w.driver._step()
    assert actions(w)[-1] == "confirm_draft_pick"
    held = at(w, "confirm_draft_pick") - start
    assert draft_event.PICK_NARRATION_MAX_S - 0.3 <= held <= draft_event.PICK_NARRATION_MAX_S + 0.3


def test_short_muted_narration_holds_its_reading_time_then_confirms(primer):
    def muted(text, cancelled):
        w.events.append(("narrate", w.clock.now, {"text": text}))
        return narrate_and_wait(
            text, send=None, completion=None, cancelled=cancelled, muted=True, sleep=w.clock.sleep
        )

    w = world(primer, narrate=muted)
    w.driver._step()
    waited = at(w, "confirm_draft_pick") - at(w, "narrate")
    assert 3.0 + draft_event.PICK_CONFIRM_BEAT_S <= waited < draft_event.PICK_NARRATION_MAX_S


def test_low_pick_clock_picks_first_and_explains_after(primer):
    w = world(primer, seconds=14)
    w.driver._step()
    assert actions(w) == ["get_screen", "get_draft_state", "submit_draft_pick"]
    assert len(w.spoken) == 1 and w.spoken[0].startswith("Taking Blue Ace: ")


def test_narration_is_cut_short_to_confirm_before_the_pick_clock_runs_out(primer):
    def endless(text, cancelled):
        w.events.append(("narrate", w.clock.now, {"text": text}))
        while not cancelled():
            w.clock.sleep(0.25)
        return False

    w = world(primer, seconds=16, narrate=endless)
    start = w.clock.now
    w.driver._step()
    assert actions(w)[-1] == "confirm_draft_pick"
    assert at(w, "confirm_draft_pick") - start <= 16 - draft_event.PICK_CLOCK_RESERVE_S + 0.01


def test_autoplay_off_while_narrating_never_confirms_and_resumes_without_repeating(primer):
    def interrupted(text, cancelled):
        w.events.append(("narrate", w.clock.now, {"text": text}))
        w.driver.set_enabled(False)
        return not cancelled()

    w = world(primer, narrate=interrupted)
    w.driver._step()
    assert "confirm_draft_pick" not in actions(w) and "submit_draft_pick" not in actions(w)
    assert w.driver.run.preview is not None  # the cards stay selected in Arena

    # Switched back on with our selection still showing: confirm it, no second narration.
    w.events.clear()
    w.bridge.replies["get_draft_state"] = state(60, reserved_count=1)
    w.driver.set_enabled(True)
    w.driver._step()
    assert actions(w) == ["get_screen", "get_draft_state", "preview_draft_pick", "confirm_draft_pick"]
    assert w.driver.run.picks[-1]["grp_ids"] == [1]


def test_someone_elses_selection_is_left_alone(primer):
    w = world(primer, replies={"get_draft_state": state(60, reserved_count=1)})
    w.driver._step()
    assert actions(w) == ["get_screen", "get_draft_state"]


def test_pack_that_moved_on_during_narration_is_not_picked_again(primer):
    states = iter([state(60), state(60, pick_number=2, pack_cards=[12, 3])])
    w = world(primer, replies={"get_draft_state": lambda fields: next(states)})
    w.driver._step()
    assert "confirm_draft_pick" not in actions(w) and "submit_draft_pick" not in actions(w)
    assert w.driver.run.preview is None


def test_bridge_without_preview_still_narrates_first_then_picks(primer):
    unsupported = {"ok": False, "unsupported": True, "error": "not supported"}
    w = world(primer, replies={"preview_draft_pick": unsupported})
    w.driver._step()
    assert actions(w)[-2:] == ["get_draft_state", "submit_draft_pick"]
    assert actions(w).index("narrate") < actions(w).index("submit_draft_pick")
    assert not w.driver.paused_reason


def test_pick_two_line_names_both_cards_before_they_are_confirmed(primer):
    w = world(primer, replies={"get_draft_state": state(60, pick_num_cards_to_take=2)})
    w.driver._step()
    text = next(f["text"] for a, _t, f in w.events if a == "narrate")
    # Both cards share a role: said once, in the plural, with their 17Lands standing.
    assert text.startswith(
        "Choosing Blue Ace and Red Ace: both key uncommons for Izzet, "
        "and the two best cards in this pack on 17Lands data."
    )
    confirm = next(f for a, _t, f in w.events if a == "confirm_draft_pick")
    assert {card["grp_id"] for card in confirm["cards"]} == {1, 3}


# ---------------------------------------------------------------------------
# The coach's narration hook (desktop speech handshake, bounded)
# ---------------------------------------------------------------------------


def test_pick_narration_waits_for_the_desktop_and_stops_at_the_deadline():
    from arenamcp.standalone_draft_event import _DraftEventMixin

    completion = SpeechCompletion()
    coach = _DraftEventMixin()
    coach._voice_output = SimpleNamespace(current_voice=("am_eric", "Eric"), speed=1.0, muted=False)

    def finished(**kwargs):
        threading.Timer(
            0.05, lambda: [completion.update(kwargs["speech_id"], s) for s in ("started", "finished")]
        ).start()

    coach.ui = SimpleNamespace(speech_completion=completion, emit_speech_request=finished)
    assert coach._narrate_draft_pick("Choosing Blue Ace.", lambda: False)

    # A desktop that never acknowledges: the pick's deadline ends the wait.
    coach.ui = SimpleNamespace(speech_completion=completion, emit_speech_request=lambda **kwargs: None)
    deadline = time.monotonic() + 0.4
    began = time.monotonic()
    coach._narrate_draft_pick("Choosing Blue Ace.", lambda: time.monotonic() >= deadline)
    assert time.monotonic() - began < 1.5

    # In-process voice: played in the background, held only while not cancelled.
    calls = []
    coach.ui = SimpleNamespace()
    coach._voice_output = SimpleNamespace(
        muted=False, speed=1.0, speak=lambda text, blocking: calls.append(blocking)
    )
    began = time.monotonic()
    coach._narrate_draft_pick("Choosing Blue Ace.", lambda: time.monotonic() - began > 0.3)
    assert calls == [False] and time.monotonic() - began < 1.5


# ---------------------------------------------------------------------------
# Mac bridge hands: select (single-click path) and confirm (the Confirm button)
# ---------------------------------------------------------------------------


def selection_world(*, take=2, selected=(), confirm_works=True):
    w = draft_world()
    controller, manager, pod = 100, 500, 600
    w.objects[pod]["PickNumCardsToTake"] = take
    chosen = set(selected)

    def sync():
        w.objects[controller]["NumberOfCardsCurrentlySelected"] = len(chosen)
        w.objects[controller]["AtMaxReservedCards"] = len(chosen) == take

    def toggle(args):
        view = args[0]["h"]
        chosen.symmetric_difference_update({view})
        sync()

    def confirm(args):
        if confirm_works and len(chosen) == take:
            w.objects[controller]["_okToPickCard"] = False

    w.results[(manager, "ReservedCardCount")] = lambda args: len(chosen)
    w.results[(manager, "IsCardAlreadyReserved")] = lambda args: args[0]["h"] in chosen
    w.results[(controller, "ToggleCardReservation")] = toggle
    w.results[(controller, "HandleOnConfirmPickButtonClicked")] = confirm
    sync()
    return w, chosen


def methods(w) -> list[str]:
    return [
        method for _h, method, _a in w.calls if method not in ("IsCardAlreadyReserved", "ReservedCardCount")
    ]


CARDS = [{"grp_id": 1, "title_id": 10}, {"grp_id": 3, "title_id": 30}]


def test_preview_selects_with_single_clicks_and_is_idempotent():
    w, chosen = selection_world()
    adapter = MacBridgeAdapter(w.send)
    result = adapter.handle({"action": "preview_draft_pick", "cards": CARDS})
    assert result["ok"] and result["newly_selected"] == 2 and chosen == {300, 301}
    assert methods(w) == ["ToggleCardReservation", "ToggleCardReservation"]
    w.calls.clear()
    again = adapter.handle({"action": "preview_draft_pick", "cards": CARDS})
    assert again["ok"] and again["newly_selected"] == 0 and chosen == {300, 301}
    assert methods(w) == []  # a second toggle would have deselected the cards
    # Never the double-click or lock-in paths, and never Confirm.
    assert not any(m in ("ReserveCardAndLockIn", "HandleOnConfirmPickButtonClicked") for _h, m, _a in w.calls)


def test_preview_finishes_a_half_done_selection_and_refuses_someone_elses():
    w, chosen = selection_world(selected={300})
    assert MacBridgeAdapter(w.send).handle({"action": "preview_draft_pick", "cards": CARDS})["ok"]
    assert chosen == {300, 301} and methods(w) == ["ToggleCardReservation"]

    w, chosen = selection_world(selected={302})
    result = MacBridgeAdapter(w.send).handle({"action": "preview_draft_pick", "cards": CARDS})
    assert not result["ok"] and "manual selection" in result["error"]
    assert chosen == {302} and methods(w) == []


def test_confirm_presses_confirm_only_for_exactly_our_selection():
    w, _chosen = selection_world(selected={300, 301})
    result = MacBridgeAdapter(w.send).handle({"action": "confirm_draft_pick", "cards": CARDS})
    assert result["ok"] and result["submitted_type"] == "DraftPickConfirm"
    assert methods(w) == ["HandleOnConfirmPickButtonClicked"]

    w, _chosen = selection_world(selected={300, 302})
    result = MacBridgeAdapter(w.send).handle({"action": "confirm_draft_pick", "cards": CARDS})
    assert not result["ok"] and methods(w) == []


def test_confirm_reports_a_press_that_did_not_draft():
    w, _chosen = selection_world(selected={300, 301}, confirm_works=False)
    result = MacBridgeAdapter(w.send).handle({"action": "confirm_draft_pick", "cards": CARDS})
    assert not result["ok"] and "did not draft" in result["error"]
