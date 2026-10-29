from pathlib import Path

import pytest

from arenamcp.auto_quest import QuestSnapshot, build_quest_deck, objective, plan, read_snapshot
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


@pytest.fixture
def red_pool():
    cards = {
        gid: {
            "name": f"Red Spell {gid}",
            "type_line": "Creature — Goblin",
            "colors": ["R"],
            "mana_cost": "{1}{R}",
            "cmc": 2,
            "legalities": {"standard": "Legal"},
        }
        for gid in range(1, 10)
    }
    cards[99] = {
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "colors": [],
        "mana_cost": "",
        "free_basic": True,
        "legalities": {"standard": "Legal"},
    }
    return {gid: 4 for gid in range(1, 10)}, cards


def test_builds_owned_color_quest_deck_without_crafting(red_pool):
    collection, cards = red_pool
    collection[99] = 24
    result = build_quest_deck("Cast {quantity} red spells.", collection, cards)
    assert result is not None
    assert result.spell_count == 36
    assert sum(count for _gid, count in result.main) == 60
    assert result.main[-1] == (99, 24)
    assert result.colors == {"R"}
    assert (result.event_id, result.format, result.legality_format) == ("Play", "Standard", "standard")


def test_build_quest_deck_uses_existing_basic_lands_when_collection_omits_them(red_pool):
    collection, cards = red_pool
    result = build_quest_deck("Cast {quantity} red spells.", collection, cards, fallback_lands=[(99, 20)])
    assert result is not None
    assert result.main[-1] == (99, 24)
    # No fallback deck is needed when the metadata itself verifies a free basic.
    assert build_quest_deck("Cast {quantity} red spells.", collection, cards) == result


def test_native_basic_requires_unlocked_printing_but_not_24_inventory_copies(red_pool):
    collection, cards = red_pool
    cards[99]["basic_requires_unlock"] = True
    assert build_quest_deck("Cast red spells.", collection, cards) is None
    cards[100] = {**cards[99]}
    collection[100] = 1
    result = build_quest_deck("Cast red spells.", collection, cards)
    assert result is not None and result.main[-1] == (100, 24)


@pytest.mark.parametrize(
    "legality",
    [None, {}, {"historic": "legal"}, {"standard": "banned"}, {"standard": "restricted"}, {"standard": True}],
)
def test_builder_requires_positive_legality_for_every_card(red_pool, legality):
    collection, cards = red_pool
    for gid in (1, 99):
        old = cards[gid].get("legalities")
        cards[gid]["legalities"] = legality
        assert build_quest_deck("Cast {quantity} red spells.", collection, cards) is None
        cards[gid]["legalities"] = old


@pytest.mark.parametrize("count", [0, 3, -1, True, "4", 4.5, None])
def test_builder_needs_36_actual_owned_spells(red_pool, count):
    collection, cards = red_pool
    collection[1] = count
    assert build_quest_deck("Cast {quantity} red spells.", collection, cards) is None


def test_builder_understands_native_colors_and_raw_mana(red_pool):
    collection, cards = red_pool
    for gid in collection:
        cards[gid]["colors"] = "4"
        cards[gid].pop("mana_cost")
        cards[gid]["OldSchoolManaText"] = "o1oR"
        cards[gid]["cmc"] = 0  # Native MTGACard has no cmc; do not trust this default.
    assert build_quest_deck("Cast {quantity} red spells.", collection, cards) is not None
    cards[1]["OldSchoolManaText"] = "o1o(R/G)"
    assert build_quest_deck("Cast {quantity} red spells.", collection, cards) is None


@pytest.mark.parametrize(
    "update",
    [
        {"colors": ["R", "U"]},
        {"colors": "3,4"},
        {"colors": ["4", "bogus"]},
        {"colors": None},
        {"mana_cost": "{U}"},
        {"mana_cost": "{1}{C}{R}"},
        {"mana_cost": "{S}{R}"},
        {"mana_cost": "{X}{R}"},
        {"mana_cost": "{R/P}"},
        {"mana_cost": "{R/G}"},
        {"mana_cost": "{1}{R}unknown"},
        {"mana_cost": ""},
        {"mana_cost": None},
        {"mana_cost": "{8}{R}"},
        {"name": ""},
        {"name": "Front // Back"},
        {"type_line": ""},
        {"type_line": "2"},
        {"type_line": "Land"},
        {"is_token": True},
        {"is_primary_card": False},
        {"layout": "transform"},
        {"related_faces": [{"name": "Back"}]},
    ],
)
def test_builder_rejects_off_color_unpayable_or_unverified_spells(red_pool, update):
    collection, cards = red_pool
    cards[1].update(update)
    assert build_quest_deck("Cast {quantity} red spells.", collection, cards) is None


def test_builder_caps_copies_by_name_across_printings_and_id_aliases(red_pool):
    collection, cards = red_pool
    cards[2]["name"] = "  RED SPELL 1  "
    cards["1"] = dict(cards[1])
    collection["1"] = 4
    assert build_quest_deck("Cast {quantity} red spells.", collection, cards) is None
    cards[10] = {**cards[1], "name": "Extra Spell"}
    collection[10] = 4
    result = build_quest_deck("Cast {quantity} red spells.", collection, cards)
    assert result is not None
    assert sum(count for gid, count in result.main if gid in {1, 2}) == 4
    assert dict(result.main)[10] == 4


def test_builder_can_combine_owned_printings_without_exceeding_copy_limit(red_pool):
    collection, cards = red_pool
    collection[1] = 2
    collection[10] = 3
    cards[10] = dict(cards[1])
    result = build_quest_deck("Cast {quantity} red spells.", collection, cards)
    assert result is not None
    assert dict(result.main)[1] == 2
    assert dict(result.main)[10] == 2


@pytest.mark.parametrize(
    "land",
    [
        {},
        {"name": "Island", "type_line": "Basic Land — Island"},
        {"name": "Mountain Valley", "type_line": "Land"},
        {"name": "Mountain", "type_line": "Land"},
        {"name": "Snow-Covered Mountain", "type_line": "Basic Snow Land — Mountain"},
        {"name": "Wastes", "type_line": "Basic Land"},
        {"name": "Mountain", "type_line": "Basic Land — Mountain", "is_token": True},
    ],
)
def test_fallback_never_invents_or_repeats_unknown_wrong_or_nonbasic_lands(red_pool, land):
    collection, cards = red_pool
    cards[99] = {**land, "legalities": {"standard": "legal"}}
    assert (
        build_quest_deck(
            "Cast {quantity} red spells.", collection, cards, fallback_lands=[(99, 24), (999, 100)]
        )
        is None
    )


def test_or_color_quest_uses_one_supported_color_and_deterministic_cheap_curve(red_pool):
    collection, cards = red_pool
    cards[100] = {**cards[99], "name": "Forest", "type_line": "Basic Land — Forest"}
    for gid in range(11, 20):
        collection[gid] = 4
        cards[gid] = {**cards[1], "name": f"Green {gid}", "colors": ["G"], "mana_cost": "{G}"}
    result = build_quest_deck("Cast {quantity} red or green spells.", collection, cards)
    assert result is not None
    assert result.colors == {"G"}
    assert result.main[-1] == (100, 24)
    assert {gid for gid, _ in result.main[:-1]} == set(range(11, 20))
    assert (
        build_quest_deck(
            "Cast {quantity} red or green spells.",
            dict(reversed(list(collection.items()))),
            dict(reversed(list(cards.items()))),
        )
        == result
    )


def test_builder_orders_by_real_cost_not_missing_native_cmc(red_pool):
    collection, cards = red_pool
    cards[1]["mana_cost"] = "{4}{R}"
    cards[1]["cmc"] = 0
    cards[10] = {**cards[1], "name": "Cheaper", "mana_cost": "{R}", "cmc": 100}
    collection[10] = 4
    result = build_quest_deck("Cast {quantity} red spells.", collection, cards)
    assert result is not None
    assert 1 not in dict(result.main)
    assert result.main[0] == (10, 4)


@pytest.mark.parametrize(
    "text",
    [
        "Destroy {quantity} creatures.",
        "Win {quantity} games.",
        "Play {quantity} lands.",
        "Cast {quantity} creature spells.",
    ],
)
def test_builder_returns_none_for_unsupported_objectives(red_pool, text):
    assert build_quest_deck(text, *red_pool) is None


def test_plan_accepts_free_ordinary_basics_but_requires_ownership_of_other_lands(inputs):
    inputs["decks"]["a"].main.append((99, 24))
    inputs["cards"][99] = {
        "name": "Forest",
        "type_line": "Basic Land — Forest",
        "colors": [],
        "free_basic": True,
    }
    assert plan(snapshot(), **inputs).status == "ready"
    for name, types in [
        ("Snow-Covered Forest", "Basic Snow Land — Forest"),
        ("Forest Lookout", "Land"),
        ("Wastes", "Basic Land"),
    ]:
        inputs["cards"][99].update(name=name, type_line=types)
        assert plan(snapshot(), **inputs).status == "blocked"


def test_plan_aggregates_duplicate_sideboard_rows_for_ownership(inputs):
    inputs["collection"][3] = 1
    inputs["decks"]["a"].sideboard = [(3, 1), (3, 1)]
    assert plan(snapshot(), **inputs).status == "blocked"


def generated_plan_inputs(red_pool, inputs):
    collection, cards = red_pool
    spec = build_quest_deck("Cast {quantity} red spells.", collection, cards)
    assert spec is not None
    inputs.update(
        cards=cards,
        collection=collection,
        decks={"new": DeckRecord("new", "Quest red", format=spec.format, main=list(spec.main))},
        descriptions={"q_Text": "Cast {quantity} red spells."},
    )
    snap = snapshot()
    snap.pairs = {}
    return spec, snap


def test_generated_deck_is_explicitly_eligible_without_fabricated_snapshot_pairs(red_pool, inputs):
    spec, snap = generated_plan_inputs(red_pool, inputs)
    assert plan(snap, **inputs).status == "blocked"
    result = plan(snap, verified_decks={"new": spec}, **inputs)
    assert result.status == "ready"
    assert (result.deck_id, result.event_id) == ("new", "Play")
    assert snap.pairs == {}


def test_generated_deck_rechecks_native_basic_unlock(red_pool, inputs):
    spec, snap = generated_plan_inputs(red_pool, inputs)
    inputs["cards"][99]["basic_requires_unlock"] = True
    assert plan(snap, verified_decks={"new": spec}, **inputs).status == "blocked"
    inputs["collection"][99] = 1
    assert plan(snap, verified_decks={"new": spec}, **inputs).status == "ready"


@pytest.mark.parametrize(
    "change",
    [
        "main",
        "sideboard",
        "command_zone",
        "companion",
        "format",
        "snapshot",
        "unowned",
        "illegal",
        "unknown_basic",
        "spec_event",
        "spec_format",
        "spec_spells",
        "spec_colors",
    ],
)
def test_generated_eligibility_is_rechecked_against_saved_deck_and_current_metadata(red_pool, inputs, change):
    spec, snap = generated_plan_inputs(red_pool, inputs)
    deck = inputs["decks"]["new"]
    if change == "main":
        deck.main[0] = (1, 3)
    elif change == "sideboard":
        deck.sideboard = [(1, 1)]
    elif change == "command_zone":
        deck.command_zone = [1]
    elif change == "companion":
        deck.companions = [1]
    elif change == "format":
        deck.format = "HistoricBrawl"
    elif change == "snapshot":
        deck.snapshot = True
    elif change == "unowned":
        inputs["collection"][1] = 3
    elif change == "illegal":
        inputs["cards"][1]["legalities"] = {"standard": "banned"}
    elif change == "unknown_basic":
        del inputs["cards"][99]
    elif change == "spec_event":
        spec.event_id = "PremierDraft"
    elif change == "spec_format":
        spec.format = "Alchemy"
    elif change == "spec_spells":
        spec.spell_count = 12
    else:
        spec.colors = {"U"}
    # An old observed pair cannot override a now-invalid generated-deck check.
    snap.pairs["new"] = "Play"
    assert plan(snap, verified_decks={"new": spec}, **inputs).status == "blocked"


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
