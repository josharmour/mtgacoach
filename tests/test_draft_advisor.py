import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.card_db import CardInfo
from arenamcp.draft_advisor import DraftAdvisor, pool_summary
from arenamcp.draft_guidance import normalize_card


def card(grp_id, name, cost, text, type_line="Creature"):
    return {"grp_id": grp_id, "name": name, "mana_cost": cost, "oracle_text": text, "type_line": type_line}


def draft_pack():
    return {
        "event_name": "PickTwoDraft_FRA",
        "pack_number": 2,
        "pick_number": 3,
        "picks_per_pack": 2,
        "cards": [
            card(1, "Seer", "{1}{U}", "When this creature enters, scry 2."),
            card(2, "Early Body", "{1}{W}", "Vigilance"),
            card(3, "Expensive Body", "{4}{R}", "Menace"),
        ],
        "picked_cards": [
            card(4, "Saheeli", "{3}{W}{W}", "Whenever you scry or surveil, create a Thopter."),
            card(4, "Saheeli", "{3}{W}{W}", "Whenever you scry or surveil, create a Thopter."),
        ],
    }


def response():
    return {
        "picks": [
            {
                "grp_id": 1,
                "reason": "Seer triggers Saheeli to make a Thopter and fills our early curve.",
                "synergy_with": [4],
            },
            {
                "grp_id": 2,
                "reason": "Early Body gives our counters and tricks a creature to use.",
                "synergy_with": [],
            },
        ],
        "plan": "Build white-blue with cheap creatures and scry to fuel Saheeli's Thopters.",
        "needs": ["More two-mana creatures", "Removal"],
        "alternative": {"grp_id": 3, "reason": "Another five-drop adds top-end and an unsupported color."},
    }


def test_pick_two_explains_rules_interactions_and_pool_needs():
    backend = Mock()
    backend.complete.return_value = json.dumps(response())
    result = DraftAdvisor(backend).recommend(draft_pack(), {"evaluations": []})
    assert [pick["grp_id"] for pick in result["recommendations"]] == [1, 2]
    assert "triggers Saheeli" in result["spoken_advice"]
    assert "Still need: More two-mana creatures" in result["detailed_advice"]
    message = json.loads(backend.complete.call_args.args[1])
    assert message["picks_required"] == 2
    assert message["already_drafted_not_pickable"][0]["copies"] == 2
    assert message["already_drafted_not_pickable"][0]["oracle_text"].startswith("Whenever you scry")
    assert message["pool_summary"]["five_plus_mana"] == 2
    assert message["pool_summary"]["early_plays"] == 0
    assert result["alternative"]["name"] == "Expensive Body"


@pytest.mark.parametrize("failure", ["absent_card", "duplicate", "missing_reason", "wrong_count"])
def test_invalid_model_choices_keep_explained_heuristic_fallback(failure):
    payload = response()
    if failure == "absent_card":
        payload["picks"][0]["grp_id"] = 999
    elif failure == "duplicate":
        payload["picks"][1]["grp_id"] = 1
    elif failure == "missing_reason":
        payload["picks"][0]["reason"] = ""
    else:
        payload["picks"].pop()
    backend = Mock()
    backend.complete.return_value = json.dumps(payload)
    fallback = {"spoken_advice": "Take Early Body; your pool needs cheap creatures."}
    result = DraftAdvisor(backend).recommend(draft_pack(), fallback)
    assert result["spoken_advice"] == fallback["spoken_advice"]
    assert result["reasoning_source"] == "heuristic"


def test_theme_follows_actual_picks_and_resets_for_new_draft():
    backend = Mock()
    backend.complete.return_value = json.dumps(response())
    advisor = DraftAdvisor(backend)
    pack = draft_pack()
    advisor.recommend(pack, {})
    pack["pick_number"] = 4
    pack["picked_cards"].append(card(9, "User's Different Pick", "{B}", "Deathtouch"))
    advisor.recommend(pack, {})
    message = json.loads(backend.complete.call_args.args[1])
    assert message["previous_plan"] == response()["plan"]
    assert [picked["pool_grp_id"] for picked in message["already_drafted_not_pickable"]] == [4, 9]
    pack["event_name"] = "NewDraft"
    pack["picked_cards"] = []
    advisor.recommend(pack, {})
    message = json.loads(backend.complete.call_args.args[1])
    assert message["previous_plan"] == ""


def test_timeout_does_not_queue_more_calls_or_apply_old_advice():
    release = threading.Event()
    backend = Mock()

    def complete(*args, **kwargs):
        release.wait(2)
        return json.dumps(response())

    backend.complete.side_effect = complete
    advisor = DraftAdvisor(backend, timeout=0.01)
    fallback = {"spoken_advice": "Card-text fallback"}
    try:
        assert advisor.recommend(draft_pack(), fallback)["spoken_advice"] == "Card-text fallback"
        assert advisor.recommend(draft_pack(), fallback) == fallback
        assert backend.complete.call_count == 1
    finally:
        release.set()
        advisor._executor.shutdown(wait=True)


def test_curve_uses_mana_cost_when_enrichment_lacks_mana_value():
    expensive = card(1, "Dragon", "{3}{R}{R}", "Flying")
    early = card(2, "Scout", "{1}{W/U}", "Vigilance")
    assert normalize_card(expensive).cmc == 5
    assert normalize_card(early).cmc == 2
    assert pool_summary([expensive, early])["early_plays"] == 1


def test_new_arena_cards_are_evaluated_from_unified_database(monkeypatch):
    from arenamcp import server

    database = Mock()
    cards = {
        106303: CardInfo(
            name="Artifist Acumen",
            oracle_text="Creatures you control gain first strike. Draw a card.",
            type_line="Sorcery",
            mana_cost="{R}",
            colors=["R"],
        ),
        106327: CardInfo(name="Early Creature", type_line="Creature", mana_cost="{1}{R}", colors=["R"]),
    }
    database.get_card_by_arena_id.side_effect = cards.get
    monkeypatch.setattr(server, "_get_card_db", lambda: database)
    monkeypatch.setattr(server, "_get_draft_stats", lambda: None)
    monkeypatch.setattr(server, "_get_mtgadb", lambda: None)
    monkeypatch.setattr(server.draft_state, "is_active", True)
    monkeypatch.setattr(server.draft_state, "is_sealed", False)
    monkeypatch.setattr(server.draft_state, "cards_in_pack", [106303, 106327])
    monkeypatch.setattr(server.draft_state, "picked_cards", [106327, 106327])
    monkeypatch.setattr(server.draft_state, "set_code", "FRA")
    monkeypatch.setitem(
        sys.modules, "arenamcp.synergy", SimpleNamespace(ensure_synergy_graph=lambda source: None)
    )
    monkeypatch.setattr(server, "_get_scryfall", lambda: Mock())
    result = server.evaluate_draft_pack_for_standalone()
    assert len(result["evaluations"]) == 2
    assert all(evaluation["score"] > 0 for evaluation in result["evaluations"])
    assert all(evaluation["reason"] for evaluation in result["evaluations"])
    assert "Take" in result["spoken_advice"]


def test_fresh_draft_pack_survives_a_retained_previous_match(monkeypatch):
    from arenamcp import server
    from arenamcp.draftstate import DraftState

    draft = DraftState(
        event_name="PickTwoDraft_FRA",
        set_code="FRA",
        is_active=True,
        pack_number=1,
        pick_number=2,
        cards_in_pack=[1, 2],
        picked_cards=[4, 4],
        picks_per_pack=2,
    )
    stale_game = Mock()
    stale_game.get_published_snapshot.return_value = {
        "match_id": "finished-match",
        "turn_info": {"turn_number": 8},
        "players": [{"seat_id": 1}],
    }
    monkeypatch.setattr(server, "game_state", stale_game)
    monkeypatch.setattr(server, "draft_state", draft)
    monkeypatch.setattr(server, "watcher", object())
    monkeypatch.setattr(
        server,
        "_get_draft_stats",
        lambda: Mock(get_color_pair_stats=lambda *args: [], get_draft_rating=lambda *args: None),
    )
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", lambda: SimpleNamespace(connected=False))
    monkeypatch.setattr(
        server, "enrich_with_oracle_text", lambda grp_id: card(grp_id, f"Card {grp_id}", "{1}{U}", "Flying")
    )
    result = server.get_draft_pack()
    assert result["is_active"] is True
    assert result["picks_per_pack"] == 2
    assert len(result["cards"]) == 2
    assert len(result["picked_cards"]) == 2
    assert draft.cards_in_pack == [1, 2]


def test_new_match_event_still_deactivates_draft(monkeypatch):
    from arenamcp import server
    from arenamcp.draftstate import DraftState
    from arenamcp.gamestate import GameState

    draft = DraftState(is_active=True, cards_in_pack=[1, 2], picked_cards=[4, 4])
    monkeypatch.setattr(server, "game_state", GameState())
    monkeypatch.setattr(server, "draft_state", draft)
    server._handle_match_created({"matchId": "new-match", "systemSeatId": 1})
    assert draft.is_active is False
    assert draft.cards_in_pack == []
    assert draft.picked_cards == [4, 4]


def test_prepared_spell_rules_and_cost_are_included_in_reasoning():
    pack = draft_pack()
    pack["cards"][0]["related_faces"] = [
        {"name": "Peer Review", "mana_cost": "{2}{W/U}", "oracle_text": "Create a Cadet. Surveil 1."}
    ]
    backend = Mock()
    backend.complete.return_value = json.dumps(response())
    DraftAdvisor(backend).recommend(pack, {})
    message = json.loads(backend.complete.call_args.args[1])
    assert message["pack_choices"][0]["related_faces"] == pack["cards"][0]["related_faces"]


def test_pack_choices_come_first_and_pool_ids_cannot_pass_for_pickable_ones():
    """2026-10-06 P1p8-9: the model picked Geist of Saint Thalia from its own pool twice."""
    from arenamcp.draft_advisor import DraftAdvisor

    captured = {}

    class Backend:
        def complete(self, system, message, max_tokens=None, **kwargs):
            captured["system"], captured["message"] = system, json.loads(message)
            return json.dumps(
                {"picks": [{"grp_id": 7, "reason": "Best pick here.", "synergy_with": []}], "plan": "x"}
            )

    pack = {
        "cards": [{"grp_id": 7, "name": "Pack Card", "type_line": "Creature", "oracle_text": ""}],
        "picked_cards": [{"grp_id": 3, "name": "Pool Card", "type_line": "Creature", "oracle_text": ""}],
        "pack_number": 1,
        "pick_number": 8,
    }
    result = DraftAdvisor(Backend(), timeout=5).recommend(pack, {"evaluations": []})
    message = captured["message"]
    keys = list(message)
    assert keys.index("pack_choices") < keys.index("already_drafted_not_pickable")
    assert message["pickable_grp_ids"] == [7]
    assert "grp_id" not in message["already_drafted_not_pickable"][0]
    assert message["already_drafted_not_pickable"][0]["pool_grp_id"] == 3
    assert "pickable_grp_ids" in captured["system"]
    assert result["recommendations"][0]["grp_id"] == 7
