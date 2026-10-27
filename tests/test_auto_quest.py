from pathlib import Path

import pytest

from arenamcp.auto_quest import QuestSnapshot, objective, plan, read_snapshot
from arenamcp.deck_library import DeckRecord


@pytest.fixture(autouse=True)
def no_live_account_reads(monkeypatch):
    monkeypatch.setattr("arenamcp.standalone_auto_quest.read_snapshot", lambda path: QuestSnapshot())
    monkeypatch.setattr("arenamcp.standalone_auto_quest.quest_descriptions", lambda: {})


@pytest.fixture
def inputs():
    return dict(
        now=1000,
        deadline=2000,
        goal="quests",
        decks={"a": DeckRecord("a", "Green", main=[(1, 20)]), "b": DeckRecord("b", "Blue", main=[(2, 20)])},
        collection={1: 20, 2: 20},
        cards={1: {"type_line": "Creature", "colors": ["G"]}, 2: {"type_line": "Creature", "colors": ["U"]}},
        descriptions={"q_Text": "Cast {quantity} white or green spells."},
    )


def snapshot():
    return QuestSnapshot(
        [{"locKey": "q", "goal": 20, "endingProgress": 4}], 999, pairs={"a": "Play", "b": "Ladder"}
    )


def test_ranks_owned_decks_for_actual_objective(inputs):
    result = plan(snapshot(), **inputs)
    assert result.status == "ready"
    assert result.deck_id == "a"
    assert "4/20" in result.detail


@pytest.mark.parametrize("change", ["unowned", "paid", "unknown_card", "snapshot", "no_pair"])
def test_ineligible_decks_never_queue(inputs, change):
    snap = snapshot()
    if change == "unowned":
        inputs["collection"][1] = 0
    elif change == "paid":
        snap.pairs["a"] = "PremierDraft"
    elif change == "unknown_card":
        inputs["cards"] = {}
    elif change == "snapshot":
        inputs["decks"]["a"].snapshot = True
    else:
        snap.pairs = {}
    result = plan(snap, **inputs)
    assert result.status == "blocked"
    assert result.coverage_gaps


def test_unknown_stale_future_and_pre_match_state_wait(inputs):
    for stamp in (0, 1001):
        snap = snapshot()
        snap.observed_at = stamp
        assert plan(snap, after=900, **inputs).status == "waiting"
    assert plan(snapshot(), after=1000, **inputs).status == "waiting"
    assert plan(QuestSnapshot(), **inputs).status == "waiting"


def test_completion_deadline_and_unsupported_objectives(inputs):
    snap = snapshot()
    snap.quests = []
    assert plan(snap, **inputs).status == "complete"
    inputs["deadline"] = 1000
    assert plan(snapshot(), **inputs).status == "complete"
    inputs["deadline"] = 2000
    inputs["descriptions"] = {}
    assert plan(snapshot(), **inputs).status == "blocked"


def test_no_assumed_progress_except_explicit_new_quest(inputs):
    snap = snapshot()
    del snap.quests[0]["endingProgress"]
    assert plan(snap, **inputs).status == "blocked"
    snap.quests[0]["isNewQuest"] = True
    assert plan(snap, **inputs).status == "ready"


def test_wins_goal_requires_fresh_server_reset(inputs):
    snap = snapshot()
    snap.quests = []
    inputs["goal"] = "daily4"
    assert plan(snap, **inputs).status == "waiting"
    snap.rewards = {"_dailyRewardSequenceId": 4, "_dailyRewardResetTimestamp": "1970-01-01T01:00:00Z"}
    snap.rewards_at = 999
    assert plan(snap, **inputs).status == "complete"
    snap.rewards["_dailyRewardSequenceId"] = 3
    assert plan(snap, **inputs).status == "ready"
    snap.rewards["_dailyRewardResetTimestamp"] = "1970-01-01T00:01:00Z"
    assert plan(snap, **inputs).status == "waiting"


def test_real_log_schema_empty_quests():
    snap = read_snapshot(Path(__file__).parent / "fixtures/player_log_idle_after_match_20261019.log")
    assert snap.quests == []
    assert snap.observed_at > 0
    # Fixture deliberately truncates the large reward JSON: don't invent it.
    assert snap.rewards == {}


def test_color_or_counts_multicolor_once():
    from arenamcp.auto_quest import quest_weight

    assert (
        quest_weight(
            "Cast {quantity} white or green spells.", [{"type_line": "Creature", "colors": ["W", "G"]}]
        )
        == 1
    )
    assert objective("Win {quantity} games with a white-green deck.")[0] == "unknown"


def test_session_does_not_persist_or_restore_quest_autoplay():
    from tests.test_standalone_auto_queue import Runtime

    runtime = Runtime()
    runtime.set_auto_quest(True, "quests", 30)
    assert runtime._auto_quest["goal"] == "quests"
    runtime.settings.set.assert_called_with("auto_queue_enabled", False)
    assert runtime._auto_queue_export() is None
    runtime.set_auto_quest(False)
    assert not runtime._auto_queue_enabled


def test_expired_session_stops_before_navigation():
    from tests.test_standalone_auto_queue import Runtime

    runtime = Runtime()
    runtime.set_auto_quest(True)
    runtime._auto_quest["deadline"] = 0
    assert not runtime._prepare_auto_quest({})
    assert not runtime._auto_queue_enabled


def test_bridge_queues_selected_deck_only_after_home():
    from unittest.mock import Mock

    from tests.test_auto_queue import navigator

    nav, *_ = navigator()
    nav._get_game_state = lambda: {}
    nav.set_enabled(True)
    nav.note_match_end(
        {
            "match_id": "q",
            "event_id": "Play",
            "deck_name": "Green",
            "deck_id": "owned-id",
            "quest_mode": True,
        },
        confirmed_match_end=True,
    )
    bridge = Mock()
    bridge.return_to_home.return_value = True
    bridge.queue_match.return_value = True
    nav._step_bridge(nav._generation, nav._abort, bridge)
    bridge.queue_match.assert_not_called()
    nav._step_bridge(nav._generation, nav._abort, bridge)
    bridge.queue_match.assert_called_once_with(event="Play", deck_id="owned-id")


def test_paid_bridge_target_is_blocked():
    from unittest.mock import Mock

    from tests.test_auto_queue import navigator

    nav, *_ = navigator()
    nav._get_game_state = lambda: {}
    nav.set_enabled(True)
    nav.note_match_end(
        {
            "match_id": "q",
            "event_id": "PremierDraft",
            "deck_name": "Green",
            "deck_id": "owned-id",
            "quest_mode": True,
        },
        confirmed_match_end=True,
    )
    nav._stage = "home"
    bridge = Mock()
    nav._step_bridge(nav._generation, nav._abort, bridge)
    bridge.queue_match.assert_not_called()
    assert nav.paused_reason


def test_pipe_carries_goal_and_window():
    from unittest.mock import Mock

    from arenamcp.pipe_adapter import PipeAdapter

    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._coach = Mock()
    adapter._dispatch(
        {"cmd": "set_auto_quest", "enabled": True, "goal": "weekly15", "minutes": 120, "deadline": 5000}
    )
    adapter._coach.set_auto_quest.assert_called_once_with(True, "weekly15", 120, 5000)


def test_stale_postmatch_data_cannot_start_navigation(monkeypatch):
    import time
    from unittest.mock import Mock

    from tests.test_standalone_auto_queue import Runtime

    from arenamcp import server

    runtime = Runtime()
    runtime.set_auto_quest(True)
    runtime._auto_quest.update(observed="finished", home="finished")
    monkeypatch.setattr(
        server, "_completed_match_for_navigation", {"match_id": "finished", "completed_at": time.time()}
    )
    monkeypatch.setattr("arenamcp.standalone_auto_quest.read_snapshot", lambda path: snapshot())
    monkeypatch.setattr("arenamcp.standalone_auto_quest.quest_descriptions", lambda: {})
    runtime._deck_library_bridge = Mock(return_value=Mock())
    runtime.collect_decks = Mock()
    assert not runtime._prepare_auto_quest({})
    runtime.collect_decks.assert_not_called()
    assert runtime._auto_quest["target"] is None


@pytest.mark.parametrize(
    "daily,weekly,expected", [(10, 15, "complete"), (10, 12, "ready"), (4, 15, "ready"), (4, 12, "ready")]
)
def test_mastery_automatically_pursues_either_remaining_xp_source(inputs, daily, weekly, expected):
    snap = snapshot()
    snap.quests = []
    snap.rewards_at = 999
    snap.rewards = {
        "_dailyRewardSequenceId": daily,
        "_weeklyRewardSequenceId": weekly,
        "_dailyRewardResetTimestamp": "1970-01-01T01:00:00Z",
        "_weeklyRewardResetTimestamp": "1970-01-01T01:00:00Z",
    }
    inputs["goal"] = "mastery"
    result = plan(snap, **inputs)
    assert result.status == expected
    if expected == "complete":
        assert "next reset" in result.detail
    else:
        assert f"daily: {max(0, 10 - daily)}" in result.detail
        assert f"weekly: {max(0, 15 - weekly)}" in result.detail


def test_mastery_prioritizes_quests_before_checking_win_counters(inputs):
    inputs["goal"] = "mastery"
    result = plan(snapshot(), **inputs)
    assert result.status == "ready"
    assert result.deck_id == "a"
    assert "Completing quests" in result.detail
    snap = snapshot()
    snap.quests = []
    assert plan(snap, **inputs).status == "waiting"


def test_default_grind_and_manual_queue_override_emit_toggle_state():
    from tests.test_standalone_auto_queue import Runtime

    runtime = Runtime()
    runtime.set_auto_quest(True)
    assert runtime._auto_quest["goal"] == "mastery"
    runtime.ui.status.assert_any_call("GRIND", "ON")
    runtime.set_auto_queue(True)
    assert runtime._auto_quest is None
    runtime.ui.status.assert_any_call("GRIND", "OFF")


def test_arena_log_timestamps_are_utc(tmp_path):
    from datetime import datetime, timezone

    path = tmp_path / "Player.log"
    path.write_text('[UnityCrossThreadLogger]10/27/2026 5:31:20 AM\n<== QuestGetQuests(id)\n{"quests":[]}\n')
    assert (
        read_snapshot(path).observed_at == datetime(2026, 10, 27, 5, 31, 20, tzinfo=timezone.utc).timestamp()
    )


def test_exhausted_xp_stops_on_enable_without_match_or_autoplay(monkeypatch):
    import time
    from datetime import datetime, timezone

    from tests.test_standalone_auto_queue import Runtime

    now = time.time()
    reset = datetime.fromtimestamp(now + 3600, timezone.utc).isoformat()
    snap = QuestSnapshot(
        [],
        now - 1,
        {
            "_dailyRewardSequenceId": 15,
            "_weeklyRewardSequenceId": 15,
            "_dailyRewardResetTimestamp": reset,
            "_weeklyRewardResetTimestamp": reset,
        },
        now - 1,
    )
    monkeypatch.setattr("arenamcp.standalone_auto_quest.read_snapshot", lambda path: snap)
    runtime = Runtime()
    runtime.ap_status = "AP:OFF"
    runtime.set_auto_quest(True)
    assert runtime._auto_quest is None
    assert not runtime._auto_queue_enabled
    runtime.ui.status.assert_any_call("GRIND", "OFF")
    assert "mastery track may still be unfinished" in runtime.ui.status.call_args.args[1]


@pytest.mark.parametrize("is_new", [True, False])
def test_home_refresh_omits_zero_progress_even_without_new_quest_flag(tmp_path, inputs, is_new):
    import json
    from datetime import datetime, timezone

    # bug_20261027_004156: the fresh 07:41:43 response omitted both
    # progress fields and isNewQuest after the user revisited Home.
    quest = {"questId": "zero-progress", "goal": 20, "locKey": "q", "canSwap": True}
    if is_new:
        quest["isNewQuest"] = True
    path = tmp_path / "Player.log"
    path.write_text(
        "[UnityCrossThreadLogger]10/27/2026 7:41:43 AM\n<== QuestGetQuests(id)\n"
        + json.dumps({"quests": [quest]})
        + "\n"
    )
    snap = read_snapshot(path)
    snap.pairs = {"a": "Play"}
    inputs.update(now=datetime(2026, 10, 27, 7, 41, 44, tzinfo=timezone.utc).timestamp())
    inputs["deadline"] = inputs["now"] + 3600
    result = plan(snap, **inputs)
    assert result.status == "ready"
    assert "0/20" in result.detail


@pytest.mark.parametrize(
    "fields",
    [
        {"endingProgress": None},
        {"endingProgress": "0"},
        {"endingProgress": -1},
        {"endingProgress": False},
        {"startingProgress": 5},
    ],
)
def test_log_reader_does_not_turn_invalid_or_partial_progress_into_zero(tmp_path, inputs, fields):
    import json

    quest = {"questId": "q", "goal": 20, "locKey": "q", **fields}
    path = tmp_path / "Player.log"
    path.write_text("<== QuestGetQuests(id)\n" + json.dumps({"quests": [quest]}) + "\n")
    snap = read_snapshot(path)
    snap.observed_at = 999
    assert plan(snap, **inputs).status == "blocked"
