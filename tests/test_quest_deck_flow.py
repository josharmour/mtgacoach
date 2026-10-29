"""Grind's whole owned-card build/save/readback path against asynchronous bridge IO."""

import copy
import json
import uuid
from unittest.mock import Mock

import pytest

from arenamcp.auto_quest import QuestSnapshot
from arenamcp.deck_library import records_from_bridge
from arenamcp.standalone_auto_quest import AutoQuestMixin


class Bridge:
    connected = True
    client_runtime = "bepinex"

    def __init__(self, log):
        self.log = log
        self.calls = []
        self.decks = [
            {
                "deck_id": str(uuid.uuid4()),
                "name": "Original blue",
                "format": "Standard",
                "main": [{"grp_id": 500, "count": 36}, {"grp_id": 501, "count": 24}],
                "sideboard": [{"grp_id": 500, "count": 1}],
                "command_zone": [],
                "companions": [],
            }
        ]
        self.collection = {str(gid): 4 for gid in range(100, 109)}
        self.pending_clone = None
        self.pending_update = None
        self.after_read = None
        self.fail_create = False

    def draft_command(self, action, **kwargs):
        self.calls.append((action, copy.deepcopy(kwargs)))
        if action == "get_screen":
            return {"ok": True, "home": True}
        if action == "get_collection":
            return {"ok": True, "cards": self.collection}
        if action == "get_decks":
            reply = {"ok": True, "decks": copy.deepcopy(self.decks)}
            if self.after_read:
                self.after_read()
            return reply
        if action == "create_deck":
            clone = copy.deepcopy(next(d for d in self.decks if d["deck_id"] == kwargs["source_deck_id"]))
            clone.update(deck_id=str(uuid.uuid4()), name=kwargs["name"])
            self.pending_clone = clone
            return {"ok": not self.fail_create, "error": "timed out"} if self.fail_create else {"ok": True}
        if action == "update_deck":
            self.pending_update = kwargs
            return {"ok": True, "submitted_type": "DeckUpdate"}
        raise AssertionError(f"Unexpected bridge operation {action}")

    def publish_clone(self):
        self.decks.append(self.pending_clone)
        self.pending_clone = None

    def return_to_home(self):
        self.calls.append(("return_to_home", {}))
        return True

    def publish_cache(self):
        update = self.pending_update
        deck = next(d for d in self.decks if d["deck_id"] == update["deck_id"])
        for pile, pairs in update["piles"].items():
            deck[pile] = [{"grp_id": gid, "count": count} for gid, count in pairs]

    def acknowledge(self, success=True):
        update = self.pending_update
        deck = next(d for d in self.decks if d["deck_id"] == update["deck_id"])
        summary = {
            "DeckId": deck["deck_id"],
            "Name": deck["name"],
            "Attributes": [{"name": "Format", "value": "Standard"}],
        }
        piles = {
            "MainDeck": [{"cardId": gid, "quantity": count} for gid, count in update["piles"]["main"]],
            "Sideboard": [],
            "CommandZone": [],
            "Companions": [],
            "CardSkins": [],
        }
        request_id = str(uuid.uuid4())
        envelope = {
            "id": request_id,
            "request": json.dumps({"Summary": summary, "Deck": piles, "ActionType": "Updated"}),
        }
        with self.log.open("a") as stream:
            stream.write("[UnityCrossThreadLogger]==> DeckUpsertDeckV3 " + json.dumps(envelope) + "\n")
            stream.write(f"<== DeckUpsertDeckV3({request_id})\n")
            stream.write(json.dumps(summary if success else {"error": "save rejected"}) + "\n")


class Runtime(AutoQuestMixin):
    def __init__(self, bridge, cards, path):
        self.bridge = bridge
        self.cards = cards
        self.ui = Mock()
        self._auto_queue_enabled = True
        self._quest_deck_journal_path = path
        self.ap = "AP:ON"
        self._auto_quest = dict(
            goal="quests",
            deadline=5000,
            started=990,
            next_poll=0,
            observed=None,
            prepared=None,
            home=None,
            target=None,
        )

    def _deck_library_bridge(self):
        return self.bridge

    def _autopilot_control_status(self):
        return self.ap

    def _suspend_auto_queue(self):
        pass

    def collect_decks(self):
        return records_from_bridge(self.bridge.draft_command("get_decks")), ""

    def _card_info_batch(self, ids):
        return copy.deepcopy(self.cards)

    def tick(self):
        self._auto_quest["next_poll"] = 0
        return self._prepare_auto_quest({})


@pytest.fixture
def flow(tmp_path, monkeypatch):
    from arenamcp import server, standalone_auto_quest

    log = tmp_path / "Player.log"
    log.write_text("initial log\n")
    snapshot = QuestSnapshot(quests=[{"locKey": "red", "goal": 20, "endingProgress": 0}], observed_at=1000)
    monkeypatch.setattr(standalone_auto_quest.time, "time", lambda: 1000)
    monkeypatch.setattr(standalone_auto_quest, "read_snapshot", lambda path: snapshot)
    monkeypatch.setattr(
        standalone_auto_quest, "quest_descriptions", lambda: {"red_Text": "Cast {quantity} red spells."}
    )
    monkeypatch.setattr(server, "_log_path", lambda: log)
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: {})
    cards = {
        gid: {
            "name": f"Red {gid}",
            "colors": "4",
            "mana_cost": "{1}{R}",
            "type_line": "Creature",
            "legalities": {"standard": "legal"},
        }
        for gid in range(100, 109)
    }
    cards[200] = {
        "name": "Mountain",
        "free_basic": True,
        "type_line": "Basic Land — Mountain",
        "legalities": {"standard": "legal"},
    }
    bridge = Bridge(log)
    runtime = Runtime(bridge, cards, tmp_path / "operation.json")
    return runtime, bridge, snapshot


def writes(bridge, action):
    return [kwargs for command, kwargs in bridge.calls if command == action]


def test_build_save_readback_then_exact_free_queue(flow, monkeypatch):
    runtime, bridge, snapshot = flow
    original = copy.deepcopy(bridge.decks)
    assert not runtime.tick()  # clone submitted
    assert not runtime.tick()  # delayed visibility never re-clones
    assert len(writes(bridge, "create_deck")) == 1
    bridge.publish_clone()
    assert not runtime.tick()  # populate all piles
    update = writes(bridge, "update_deck")[0]
    assert update["piles"]["main"][-1] == (200, 24)  # no collection entry needed for free basics
    assert all(update["piles"][pile] == [] for pile in ("sideboard", "command_zone", "companions"))
    bridge.publish_cache()
    assert not runtime.tick()  # cache alone cannot establish persistence
    bridge.acknowledge()
    assert runtime.tick(), runtime.ui.status.call_args_list
    target = runtime._auto_quest["target"]
    assert target["deck_id"] == update["deck_id"] and target["event_id"] == "Play"
    assert runtime._auto_quest["idle_ready"]
    assert snapshot.pairs == {}  # no fabricated observed queue pair
    assert bridge.decks[:1] == original
    assert len(writes(bridge, "update_deck")) == 1

    # Hand off the actual verified target to the real navigation implementation.
    import threading

    from tests.test_auto_queue import navigator

    nav, *_ = navigator()
    nav._get_game_state = lambda: {}
    nav.set_enabled(True)
    assert nav.start_idle_grind(target, {})
    queue_bridge = Mock()
    queue_bridge.queue_match.return_value = True
    nav._step_bridge(nav._generation, threading.Event(), queue_bridge)
    queue_bridge.queue_match.assert_called_once_with(event="Play", deck_id=target["deck_id"])
    assert not nav._queue_confirmed
    nav._queue_selection = lambda: {**target, "recorded_at": 1000}
    nav._step_bridge(nav._generation, nav._abort, queue_bridge)
    assert nav._queue_confirmed
    queue_bridge.queue_match.assert_called_once()

    # An actual new match hands control to autoplay, then Grind observes it.
    assert not nav.process_tick({"match_id": "next-match"})
    assert runtime._prepare_auto_quest({"match_id": "next-match"})
    assert runtime._auto_quest["observed"] == "next-match"
    from arenamcp import server

    monkeypatch.setattr(
        server,
        "get_completed_match_for_navigation",
        lambda: {"match_id": "next-match", "completed_at": 1000},
    )
    snapshot.quests[0]["endingProgress"] = 4
    assert not runtime.tick()  # Leave the completed match first.
    assert runtime.tick()  # Reuse the saved deck, continue the same quest.
    assert runtime._auto_quest["target"]["deck_id"] == target["deck_id"]
    assert len(writes(bridge, "create_deck")) == len(writes(bridge, "update_deck")) == 1


def test_unknown_create_result_reconciles_after_restart_without_duplicate(flow):
    runtime, bridge, _ = flow
    bridge.fail_create = True
    assert not runtime.tick()
    replacement = Runtime(bridge, runtime.cards, runtime._quest_deck_journal_path)
    assert not replacement.tick()
    assert len(writes(bridge, "create_deck")) == 1
    bridge.publish_clone()
    assert not replacement.tick()
    assert len(writes(bridge, "update_deck")) == 1


def test_definite_clone_rejection_requires_new_explicit_session_before_retry(flow):
    runtime, bridge, _ = flow
    original = bridge.draft_command

    def reject(action, **kwargs):
        if action == "create_deck":
            bridge.calls.append((action, kwargs))
            return {"ok": False, "not_submitted": True, "error": "no method set_Name/1"}
        return original(action, **kwargs)

    bridge.draft_command = reject
    assert not runtime.tick()
    assert not runtime.tick()
    assert len(writes(bridge, "create_deck")) == 1
    bridge.draft_command = original
    runtime._auto_quest["started"] = 995
    assert not runtime.tick()
    assert len(writes(bridge, "create_deck")) == 2
    assert bridge.pending_clone is not None


@pytest.mark.parametrize("known_failure", [True, False])
def test_legacy_name_setter_failure_recovers_but_unknown_create_stays_uncertain(flow, known_failure):
    runtime, bridge, _ = flow
    bridge.fail_create = True
    runtime.tick()
    path = runtime._quest_deck_journal_path
    journal = json.loads(path.read_text())
    journal.pop("attempt_id")
    journal["submission_error"] = (
        "no method set_Name/1 on Wizards.Mtga.Decks.Client_DeckSummary"
        if known_failure
        else "timeout after CreateDeck"
    )
    path.write_text(json.dumps(journal))
    bridge.fail_create = False
    runtime.tick()
    assert len(writes(bridge, "create_deck")) == (2 if known_failure else 1)
    if known_failure:
        archived = list(path.parent.glob(path.stem + "-rejected-*.json"))
        assert len(archived) == 1
        assert json.loads(archived[0].read_text()) == journal


def test_save_failure_or_wrong_cache_never_queues(flow):
    runtime, bridge, _ = flow
    runtime.tick()
    bridge.publish_clone()
    runtime.tick()
    bridge.publish_cache()
    bridge.acknowledge(success=False)
    for _ in range(3):
        assert not runtime.tick()
    assert runtime._auto_quest["target"] is None
    assert len(writes(bridge, "create_deck")) == len(writes(bridge, "update_deck")) == 1
    bridge.acknowledge()
    bridge.decks[-1]["sideboard"] = [{"grp_id": 100, "count": 1}]
    assert not runtime.tick()


def test_update_exception_after_submission_reconciles_without_replay(flow):
    runtime, bridge, _ = flow
    runtime.tick()
    bridge.publish_clone()
    original_command = bridge.draft_command

    def timeout(action, **kwargs):
        response = original_command(action, **kwargs)
        if action == "update_deck":
            raise TimeoutError("response lost after submission")
        return response

    bridge.draft_command = timeout
    assert not runtime.tick()
    assert not runtime.tick()
    bridge.publish_cache()
    bridge.acknowledge()
    assert runtime.tick()
    assert len(writes(bridge, "create_deck")) == len(writes(bridge, "update_deck")) == 1


@pytest.mark.parametrize("gate", ["cancel", "autoplay", "stale", "deadline"])
def test_cancellation_during_read_blocks_writes(flow, gate):
    runtime, bridge, snapshot = flow

    def stop():
        if gate == "cancel":
            runtime._auto_quest = None
        elif gate == "autoplay":
            runtime.ap = "AP:OFF"
        elif gate == "stale":
            snapshot.observed_at = -1000  # Older than the 30-minute freshness window.
        else:
            runtime._auto_quest["deadline"] = 999

    bridge.after_read = stop
    assert not runtime.tick()
    assert not writes(bridge, "create_deck") and not writes(bridge, "update_deck")


def test_same_named_existing_deck_is_never_adopted(flow):
    runtime, bridge, _ = flow
    runtime.tick()
    bridge.decks[0]["name"] = bridge.pending_clone["name"]
    assert not runtime.tick()
    assert not writes(bridge, "update_deck")
    bridge.publish_clone()
    runtime.tick()
    assert writes(bridge, "update_deck")[0]["deck_id"] == bridge.decks[-1]["deck_id"]


def test_brawl_only_source_never_cloned(flow):
    runtime, bridge, _ = flow
    bridge.decks[0]["format"] = "HistoricBrawl"
    assert not runtime.tick()
    assert not writes(bridge, "create_deck")


def test_idle_accepts_fresh_home_quests_fetched_before_grind_was_enabled(flow):
    runtime, bridge, snapshot = flow
    snapshot.observed_at = 980  # Home response precedes enable at 990.
    assert not runtime.tick()
    assert len(writes(bridge, "create_deck")) == 1
    bridge.publish_clone()
    runtime.tick()
    bridge.publish_cache()
    bridge.acknowledge()
    assert runtime.tick()  # The per-write freshness gate must also accept it.


def test_idle_rejects_pre_match_quest_data_even_if_session_started_later(flow, monkeypatch):
    from arenamcp import server

    runtime, bridge, snapshot = flow
    snapshot.observed_at = 980
    monkeypatch.setattr(
        server, "get_completed_match_for_navigation", lambda: {"match_id": "old", "completed_at": 985}
    )
    assert not runtime.tick()
    assert not writes(bridge, "create_deck")
    assert "stale" in runtime.ui.status.call_args.args[1]


@pytest.mark.parametrize("observed", [-1000, 1001])
def test_idle_still_rejects_expired_or_future_home_data(flow, observed):
    runtime, bridge, snapshot = flow
    snapshot.observed_at = observed
    assert not runtime.tick()
    assert not writes(bridge, "create_deck")


def test_unknown_bridge_reports_missing_new_deck_queue_contract(flow):
    runtime, bridge, _ = flow
    bridge.client_runtime = "unsupported-runtime"
    runtime.tick()
    bridge.publish_clone()
    runtime.tick()
    bridge.publish_cache()
    bridge.acknowledge()
    assert not runtime.tick()
    assert runtime._auto_quest["verified_decks"]
    assert runtime._auto_quest["target"] is None
    assert "cannot queue a new constructed deck by ID" in runtime.ui.status.call_args.args[1]


def test_cancel_after_clone_prevents_population(flow):
    runtime, bridge, _ = flow
    runtime.tick()
    bridge.publish_clone()
    runtime.ap = "AP:OFF"
    assert not runtime.tick()
    assert not writes(bridge, "update_deck")


def test_native_mac_can_prepare_saved_new_deck(flow):
    runtime, bridge, _ = flow
    bridge.client_runtime = "il2cpp-macos"
    runtime.tick()
    bridge.publish_clone()
    runtime.tick()
    bridge.publish_cache()
    bridge.acknowledge()
    assert runtime.tick()
    assert runtime._auto_quest["target"]["deck_id"] == bridge.decks[-1]["deck_id"]


def test_queue_unknown_result_never_repeats_and_requires_exact_id(monkeypatch):
    from tests.test_grind_idle import TARGET, navigator

    nav = navigator()
    nav.set_enabled(True)
    assert nav.start_idle_grind(TARGET, {})
    bridge = Mock()
    bridge.queue_match.return_value = False  # Could have timed out after submission.
    nav._step_bridge(nav._generation, nav._abort, bridge)
    nav._step_bridge(nav._generation, nav._abort, bridge)
    bridge.queue_match.assert_called_once()
    nav._queue_selection = lambda: {**TARGET, "deck_id": "different-id", "recorded_at": nav._queue_clicked_at}
    nav._step_bridge(nav._generation, nav._abort, bridge)
    assert nav.paused_reason
    assert not nav._queue_confirmed


@pytest.mark.parametrize("gate", ["cancel", "deadline", "autoplay"])
def test_worker_rechecks_session_gate_after_slow_read(flow, gate):
    from tests.test_grind_idle import TARGET, navigator

    runtime, _, _ = flow
    run = runtime._auto_quest
    run["valid_until"] = 2000
    nav = navigator()
    nav.set_enabled(True)
    assert nav.start_idle_grind(TARGET, {})
    nav._action_allowed = lambda: runtime._quest_navigation_allowed(run)

    def slow_state():
        if gate == "cancel":
            runtime._auto_quest = None
        elif gate == "deadline":
            run["deadline"] = 999
        else:
            runtime.ap = "AP:OFF"
        return {}

    nav._get_game_state = slow_state
    bridge = Mock()
    nav._step_bridge(nav._generation, nav._abort, bridge)
    bridge.queue_match.assert_not_called()
