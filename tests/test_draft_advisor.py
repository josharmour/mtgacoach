import json
import logging
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


def test_plan_follows_actual_picks_and_resets_for_new_draft():
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


def _lane_pack(pool, plan, lane, pick_number=5, pack_number=1):
    pack = draft_pack()
    pack["pack_number"] = pack_number
    pack["pick_number"] = pick_number
    pack["picked_cards"] = pool
    payload = response()
    payload["plan"] = plan
    payload["lane"] = lane
    return pack, payload


WU_POOL = [
    card(11, "Tide Scholar", "{1}{U}", "Flying"),
    card(12, "Meadow Squire", "{W}", "Vigilance"),
    card(13, "Cloud Sentry", "{2}{W}{U}", "Flying"),
    card(14, "Hybrid Knight", "{1}{W/B}", "Lifelink"),  # hybrid: castable in WU
]
BR_CARDS = [
    card(21, "Ember Hound", "{1}{R}", "Haste"),
    card(22, "Bog Snare", "{1}{B}", "Destroy target creature.", "Instant"),
    card(23, "Blood Pact", "{B}{R}", "Menace"),
    card(24, "Cinder Imp", "{R}", "Flying"),
    card(25, "Grave Rat", "{B}", "Deathtouch"),
    card(26, "Rakdos Cutter", "{2}{B}{R}", "Trample"),
]


def test_lane_holds_when_the_model_rationalises_one_off_color_card(caplog):
    # 2026-10-10 FRA: lane=BG, RG, BG, UG, RG, UB, BG over eight picks while the
    # pool stayed the same handful of cards.
    backend = Mock()
    advisor = DraftAdvisor(backend)
    pack, payload = _lane_pack(WU_POOL, "Build white-blue fliers and tempo.", "WU", 6)
    backend.complete.return_value = json.dumps(payload)
    first = advisor.recommend(pack, {})
    assert first["lane"] == "WU" and "lane_rejected" not in first

    pack, payload = _lane_pack(
        WU_POOL + BR_CARDS[:1], "Pivot to black-red aggro around Ember Hound.", "BR", 7
    )
    backend.complete.return_value = json.dumps(payload)
    with caplog.at_level(logging.INFO, logger="arenamcp.draft_advisor"):
        second = advisor.recommend(pack, {})
    assert second["lane"] == "WU"
    assert second["lane_rejected"] == "BR"
    assert second["plan"] == "Build white-blue fliers and tempo."
    assert second["rejected_plan"].startswith("Pivot to black-red")
    assert "Plan: Build white-blue" in second["spoken_advice"]
    assert any(m.startswith("Draft lane held at WU: BR casts 2 vs 4 (needs +2)") for m in caplog.messages)
    # The next prompt continues from the held lane and plan, not the discarded pivot.
    backend.complete.return_value = json.dumps(payload)
    advisor.recommend(_lane_pack(WU_POOL + BR_CARDS[:1], "x", "BR", 8)[0], {})
    message = json.loads(backend.complete.call_args.args[1])
    assert message["previous_lane"] == "WU"
    assert message["previous_plan"] == "Build white-blue fliers and tempo."


def test_lane_switches_once_the_pool_casts_clearly_more_in_the_new_pair(caplog):
    backend = Mock()
    advisor = DraftAdvisor(backend)
    pack, payload = _lane_pack(WU_POOL, "Build white-blue fliers and tempo.", "WU", 6)
    backend.complete.return_value = json.dumps(payload)
    advisor.recommend(pack, {})
    # WU casts 4 pool cards (hybrid included); BR must cast at least 6 to take over in pack 1.
    pool = WU_POOL + BR_CARDS[:5]  # BR casts 5 (+ the hybrid knight = 6)
    pack, payload = _lane_pack(pool, "Switch to black-red aggro: the pool is now mostly Rakdos.", "BR", 10)
    backend.complete.return_value = json.dumps(payload)
    with caplog.at_level(logging.INFO, logger="arenamcp.draft_advisor"):
        result = advisor.recommend(pack, {})
    assert result["lane"] == "BR" and "lane_rejected" not in result
    assert any(
        m.startswith("Draft lane switch: the pool casts 6 cards in BR vs 4 in WU") for m in caplog.messages
    )
    # One card short of the margin holds.
    advisor2 = DraftAdvisor(backend)
    pack, payload = _lane_pack(WU_POOL, "Build white-blue fliers and tempo.", "WU", 6)
    backend.complete.return_value = json.dumps(payload)
    advisor2.recommend(pack, {})
    pack, payload = _lane_pack(WU_POOL + BR_CARDS[:4], "Switch to black-red aggro.", "BR", 9)
    backend.complete.return_value = json.dumps(payload)
    assert advisor2.recommend(pack, {})["lane"] == "WU"
    # From pack 2 the same six-card edge is one short of the late margin.
    advisor3 = DraftAdvisor(backend)
    pack, payload = _lane_pack(WU_POOL, "Build white-blue fliers and tempo.", "WU", 6)
    backend.complete.return_value = json.dumps(payload)
    advisor3.recommend(pack, {})
    pack, payload = _lane_pack(WU_POOL + BR_CARDS[:5], "Switch to black-red aggro.", "BR", 2, pack_number=2)
    backend.complete.return_value = json.dumps(payload)
    assert advisor3.recommend(pack, {})["lane"] == "WU"


def test_opening_picks_declare_no_lane_and_a_lane_needs_pool_support(caplog):
    # 2026-10-10 15:41: lane=RG declared on P1p4 from one green card, held for
    # four picks of blue cards ("outside our red-green colors").
    backend = Mock()
    advisor = DraftAdvisor(backend)
    pack, payload = _lane_pack([BR_CARDS[0]], "Open toward Gruul ramp around Ember Hound.", "RG", 2)
    backend.complete.return_value = json.dumps(payload)
    with caplog.at_level(logging.INFO, logger="arenamcp.draft_advisor"):
        result = advisor.recommend(pack, {})
    assert result["lane"] == "" and result["lane_rejected"] == "RG"
    assert result["plan"].startswith("Open toward Gruul")  # the lean stays in the plan text
    assert any("picks 1-4 take the best card; no lane yet" in m for m in caplog.messages)
    # Pick 5: one red card does not support a red-green lane while the pool casts 3 white-blue cards.
    pack, payload = _lane_pack(WU_POOL[:3] + BR_CARDS[:1], "Gruul ramp.", "RG", 5)
    backend.complete.return_value = json.dumps(payload)
    result = advisor.recommend(pack, {})
    assert result["lane"] == "" and result["lane_rejected"] == "RG"
    # A single main color is a valid first commitment once two pool cards are mono-blue.
    drake = card(15, "Mist Drake", "{2}{U}", "Flying")
    pack, payload = _lane_pack(
        WU_POOL[:3] + [drake] + BR_CARDS[:1], "Blue-based tempo, second color open.", "U", 6
    )
    backend.complete.return_value = json.dumps(payload)
    assert advisor.recommend(pack, {})["lane"] == "U"
    # Settling the pair needs the pool to back it; WU casts 5 of 6, the best pair.
    pack, payload = _lane_pack(WU_POOL + [drake] + BR_CARDS[:1], "Azorius fliers.", "WU", 7)
    backend.complete.return_value = json.dumps(payload)
    assert advisor.recommend(pack, {})["lane"] == "WU"
    # A committed pair is not dropped for "open".
    pack, payload = _lane_pack(WU_POOL + [drake] + BR_CARDS[:1], "Staying open between several pairs.", "", 8)
    backend.complete.return_value = json.dumps(payload)
    result = advisor.recommend(pack, {})
    assert result["lane"] == "WU" and result["lane_rejected"] == ""


def test_a_splash_is_kept_only_with_fixing_in_the_pool(caplog):
    # 2026-10-10 P3p10: "in our red splash" from a plan's "possible light red splash" with no red source.
    backend = Mock()
    advisor = DraftAdvisor(backend)
    pack, payload = _lane_pack(
        WU_POOL, "Azorius fliers with a possible light red splash for Ember Hound.", "WU", 9
    )
    backend.complete.return_value = json.dumps(payload)
    with caplog.at_level(logging.INFO, logger="arenamcp.draft_advisor"):
        result = advisor.recommend(pack, {})
    assert result["lane"] == "WU" and result["splash"] == ""
    assert any(
        m.startswith("Draft splash dropped: no fixing in the pool for a R splash") for m in caplog.messages
    )
    rock = card(30, "Murmuring Volume", "{2}", "{T}: Add one mana of any color.", "Artifact")
    pack, payload = _lane_pack(
        WU_POOL + [rock], "Azorius fliers with a light red splash for Ember Hound.", "WU", 10
    )
    backend.complete.return_value = json.dumps(payload)
    assert advisor.recommend(pack, {})["splash"] == "R"


# --- the table's signal and value above replacement (2026-10-10 research) ---------------

from arenamcp.draft_advisor import lane_quality, validate_lane  # noqa: E402
from arenamcp.draft_autopick import color_signals, note_openness  # noqa: E402


def test_signals_accumulate_per_pack_with_direction_weighting():
    store = {}
    note_openness(store, 1, {"W": 0.0, "U": 1.0, "B": 0.2, "R": 0.0, "G": 0.0})
    note_openness(store, 1, {"W": 0.0, "U": 1.0, "B": 0.6, "R": 0.0, "G": 0.0})
    assert color_signals(store, 1) == {"W": 0.0, "U": 1.0, "B": 0.4, "R": 0.0, "G": 0.0}
    note_openness(store, 2, {"W": 1.0, "U": 0.0, "B": 0.0, "R": 0.0, "G": 0.0})
    # Pack 2 comes from the other neighbour: pack 1's blue counts half there.
    assert color_signals(store, 2) == {"W": 1.0, "U": 1.0, "B": 0.4, "R": 0.0, "G": 0.0}
    # Pack 3 passes the same way as pack 1: blue is back in full, white at half.
    assert color_signals(store, 3)["U"] == 1.0 and color_signals(store, 3)["W"] == 0.25
    assert color_signals({}, 1) == {"W": 0.0, "U": 0.0, "B": 0.0, "R": 0.0, "G": 0.0}


def test_a_flowing_pair_can_be_declared_two_short_and_a_cut_pair_is_refused():
    pool = WU_POOL[:3] + BR_CARDS[:3]  # WU casts 3, BR casts 3 (+hybrid knight? no: knight is WU_POOL[3])
    blue_flowing = {"W": 0.1, "U": 1.0, "B": 0.2, "R": 0.3, "G": 0.0}
    # Equal support: WU is declared (within one of the best pair) with the signal noted.
    lane, why = validate_lane("", "WU", pool, pick_number=6, signals=blue_flowing)
    assert lane == "WU" and "declared" in why and "WU signal 0.55" in why
    # BR is within one card of WU but WU's colours are clearly flowing: stay open rather than fight the table.
    lane, why = validate_lane("", "BR", pool, pick_number=6, signals=blue_flowing)
    assert lane == "" and "is flowing" in why
    # Two short of the best pair is enough when the proposed pair is clearly flowing.
    drake = card(15, "Mist Drake", "{2}{U}", "Flying")
    pool = BR_CARDS[:4] + [WU_POOL[0], drake]  # BR 4, WU 2
    lane, why = validate_lane(
        "", "WU", pool, pick_number=7, signals={"W": 0.6, "U": 1.0, "B": 0.2, "R": 0.1, "G": 0.0}
    )
    assert lane == "WU" and "on the signal" in why
    lane, _why = validate_lane("", "WU", pool, pick_number=7)  # no signal: support decides
    assert lane == ""


def test_switch_margin_scales_with_signal_and_card_quality():
    def rated(c, rate):
        return {**c, "gih_wr": rate}

    held = [rated(c, 0.54) for c in WU_POOL]  # WU casts 4
    # One card short of the pack-1 margin holds; the same edge switches when BR is flowing.
    pool = held + [rated(c, 0.54) for c in BR_CARDS[:4]]  # BR casts 4 (+hybrid) = 5
    assert validate_lane("WU", "BR", pool, pick_number=10)[0] == "WU"
    lane, why = validate_lane(
        "WU", "BR", pool, pick_number=10, signals={"W": 0.1, "U": 0.2, "B": 0.9, "R": 1.0, "G": 0.0}
    )
    assert lane == "BR" and "BR is flowing" in why
    # Clearly stronger cards in the new pair also lower the bar (value above replacement)...
    pool = held + [rated(c, 0.58) for c in BR_CARDS[:4]]
    lane, why = validate_lane("WU", "BR", pool, pick_number=10)
    assert lane == "BR" and "BR cards stronger" in why
    # ...and clearly stronger held cards raise it: six vs four is not enough any more.
    pool = [rated(c, 0.60) for c in WU_POOL] + [rated(c, 0.54) for c in BR_CARDS[:5]]
    lane, why = validate_lane("WU", "BR", pool, pick_number=10)
    assert lane == "WU" and "WU cards stronger" in why and "needs +3" in why
    assert lane_quality(pool, "WU") > lane_quality(pool, "BR")
    assert (
        lane_quality([{"grp_id": 1, "name": "x", "mana_cost": "{U}", "type_line": "Creature"}], "U") is None
    )
