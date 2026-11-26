"""Regressions from the 2026-11-24/25 loss streak audit and Recent Constructed UI."""

from __future__ import annotations

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.card_facts import client as cf_client
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_actions_available
from arenamcp.match_history import MatchRecord
from arenamcp.play_safety import affordable_x, exact_mana_total, filter_play_options

ME, THEM = 2, 1

HYDRA_TEXT = (
    "Vigilance\nTrample\nHaste\nThis creature enters with X +1/+1 counters on it.\n"
    "When this creature dies, create a number of tapped Treasure tokens equal to its power."
)


def _hydra(instance_id: int = 353) -> dict:
    return {
        "instance_id": instance_id,
        "grp_id": 90513,
        "name": "Goldvein Hydra",
        "controller_seat_id": ME,
        "owner_seat_id": ME,
        "type_line": "Creature — Hydra",
        "mana_cost": "{X}{G}",
        "power": 0,
        "toughness": 0,
        "oracle_text": HYDRA_TEXT,
    }


def _forest(instance_id: int, tapped: bool = False) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Forest",
        "controller_seat_id": ME,
        "type_line": "Basic Land — Forest",
        "is_tapped": tapped,
        "oracle_text": "({T}: Add {G}.)",
    }


def _birds(instance_id: int = 447, tapped: bool = False) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Birds of Paradise",
        "controller_seat_id": ME,
        "type_line": "Artifact Creature — Bird",
        "is_tapped": tapped,
        "summoning_sickness": False,
        "oracle_text": "Flying\n{T}: Add one mana of any color.",
    }


def _cavern(instance_id: int = 582, tapped: bool = False) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Cavern of Souls",
        "controller_seat_id": ME,
        "type_line": "Land",
        "is_tapped": tapped,
        "oracle_text": (
            "As this land enters, choose a creature type.\n"
            "{T}: Add {C}.\n"
            "{T}: Add one mana of any color. Spend this mana only to cast a creature spell of the chosen type, "
            "and that spell can't be countered."
        ),
    }


def _shang_chi(instance_id: int = 668, tapped: bool = False) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Shang-Chi, Master of Kung Fu",
        "controller_seat_id": ME,
        "type_line": "Legendary Creature — Human Warrior",
        "mana_cost": "{1}{G}",
        "is_tapped": tapped,
        "summoning_sickness": False,
        "oracle_text": (
            "You may activate abilities of creatures you control as though those creatures had haste.\n"
            "{oT}: Add two mana of any one color. Spend this mana only to activate abilities of creatures."
        ),
    }


def _x_menu(count: int = 10) -> PendingDecision:
    return PendingDecision(
        request_id=(287, 410),
        request_type="CastingTimeOptions",
        options=tuple(
            DecisionOption(
                f"idx:{n}",
                f"X = {n}",
                meta={"choiceKind": "numeric_input", "numericValue": n, "sourceId": 623},
            )
            for n in range(count)
        ),
        can_cancel=True,
    )


def _dillegent_t11_state(*extra: dict) -> dict:
    return {
        "local_seat_id": ME,
        "turn": {"turn_number": 11, "active_player": ME},
        "players": [
            {"seat_id": THEM, "is_local": False, "life_total": 8},
            {"seat_id": ME, "is_local": True, "life_total": 20},
        ],
        "battlefield": [
            _forest(446),
            _forest(469),
            _birds(447),
            _cavern(582),
            _shang_chi(668),
            *extra,
        ],
        "hand": [],
        "stack": [_hydra(623)],
    }


def test_activate_only_mana_does_not_blind_x_spell_budget(monkeypatch):
    """2026-11-25 Dillegent T11: Shang-Chi's activate-only mana cannot cast Goldvein Hydra."""
    monkeypatch.setattr(cf_client, "facts_for", lambda cards, **kwargs: {})
    state = _dillegent_t11_state()

    assert exact_mana_total(state) == 4
    assert affordable_x(state, _hydra(623)) == 3
    kept = filter_play_options(_x_menu(10), state)
    assert [o.meta["numericValue"] for o in kept.options] == [1, 2, 3]


def test_activate_only_mana_with_card_facts_does_not_blind_x_spell_budget(monkeypatch):
    """Same Dillegent T11 board when CardFacts has an authenticated record for Shang-Chi."""
    sc = _shang_chi(668)
    sc_hash = cf_client.hash_for_card(sc)
    sc_rec = {
        "schema_version": 1,
        "name": "Shang-Chi, Master of Kung Fu",
        "mana_cost": "{1}{G}",
        "type_line": "Legendary Creature — Human Warrior",
        "rules_hash": sc_hash,
        "modes": [
            {"name": "cast", "cost": {"mana": "{1}{G}"}, "cost_text": "{1}{G}", "stays_on_battlefield": True},
            {"name": "activate", "cost": {"tap": True}, "cost_text": "{T}", "stays_on_battlefield": True},
        ],
        "effects": [
            {
                "trigger": "activated",
                "effect": {"effect_type": "AddMana", "mana": "any color", "amount": 2},
            }
        ],
        "mana": [{"mana": "any", "condition": "Spend this mana only to activate abilities of creatures."}],
        "confidence": "high",
        "notes": "",
    }
    monkeypatch.setattr(cf_client, "facts_for", lambda cards, **kwargs: {sc_hash: sc_rec})
    state = _dillegent_t11_state()

    assert exact_mana_total(state) == 4
    assert affordable_x(state, _hydra(623)) == 3
    kept = filter_play_options(_x_menu(10), state)
    assert [o.meta["numericValue"] for o in kept.options] == [1, 2, 3]


def test_controls_that_must_keep_exact_mana_unknown(monkeypatch):
    """Opposite controls: Castle Garenbrig (can cast creatures), Sol Ring, and Utopia Sprawl stay None."""
    monkeypatch.setattr(cf_client, "facts_for", lambda cards, **kwargs: {})

    castle = {
        "instance_id": 700,
        "name": "Castle Garenbrig",
        "controller_seat_id": ME,
        "type_line": "Land",
        "is_tapped": False,
        "oracle_text": (
            "{T}: Add {G}.\n"
            "{2}{G}{G}, {T}: Add six {G}. Spend this mana only to cast creature spells "
            "or activate abilities of creatures."
        ),
    }
    sol_ring = {
        "instance_id": 701,
        "name": "Sol Ring",
        "controller_seat_id": ME,
        "type_line": "Artifact",
        "is_tapped": False,
        "oracle_text": "{T}: Add {C}{C}.",
    }
    sprawl = {
        "instance_id": 702,
        "name": "Utopia Sprawl",
        "controller_seat_id": ME,
        "type_line": "Enchantment — Aura",
        "oracle_text": (
            "Whenever enchanted Forest is tapped for mana, its controller adds an additional one mana "
            "of the chosen color."
        ),
    }

    for card in (castle, sol_ring, sprawl):
        state = _dillegent_t11_state(card)
        assert exact_mana_total(state) is None, card["name"]
        assert affordable_x(state, _hydra(623)) is None, card["name"]


def test_align_numeric_x_choice_repairs_off_by_one_reasoning():
    """2026-11-25 Dillegent T11/T17: reasoning states X=3 while model picked idx:4 (X=4)."""
    menu = _x_menu(8)
    trace: dict = {}
    chosen = ActionPlanner._align_numeric_x_choice(
        menu,
        {"reasoning": "Only 4 mana is reachable, so X=3 is the largest payable value."},
        ["idx:4"],
        trace,
    )
    assert chosen == ["idx:3"]
    assert trace["x_value_repaired"] == {"from": 4, "to": 3}

    # Controls: matching X, multiple X values, inequality, or withheld X must not rewrite.
    for reasoning, picked in (
        ("With 5 mana I can cast at X=4.", ["idx:4"]),
        ("Either X=3 or X=4 works here.", ["idx:4"]),
        ("Goldvein Hydra at X<=2 is weak, so cast bigger.", ["idx:4"]),
        ("X=99 is not offered in this menu.", ["idx:4"]),
    ):
        t: dict = {}
        assert ActionPlanner._align_numeric_x_choice(menu, {"reasoning": reasoning}, picked, t) == picked
        assert "x_value_repaired" not in t


def test_mdfc_land_and_spell_labels_and_priority_classification():
    """2026-11-25 dvniel22 T8: ActionType_PlayMdfc must label the MDFC land face clearly."""
    names = {
        90824: "Bridgeworks Battle",
        90825: "Tanglespan Bridgeworks",
        75553: "Forest",
    }
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": 100,
        "msg_id": 10,
        "can_pass": True,
        "actions": [
            {"actionType": "Play", "grpId": 75553, "instanceId": 300},
            {
                "actionType": "CastMdfc",
                "grpId": 90824,
                "instanceId": 247,
                "hasAutoTap": True,
                "manaCost": [{"color": '[ "Generic" ]', "count": 2}, {"color": '[ "Green" ]', "count": 1}],
            },
            {"actionType": "PlayMdfc", "grpId": 90825, "instanceId": 247},
            {"actionType": "Pass"},
        ],
    }
    decision = build_pending_decision(poll, resolve_name=lambda gid: names.get(gid, ""))
    assert decision is not None
    assert decision.find("idx:0").label == "Play land: Forest"
    assert decision.find("idx:1").label == "Cast Bridgeworks Battle"
    assert decision.find("idx:1").payable is True
    assert (
        decision.find("idx:2").label == "Play land: Tanglespan Bridgeworks (MDFC land face — not the spell)"
    )

    gs = GameState()
    gs.local_seat_id = 1
    _handle_actions_available(
        gs,
        {"actionsAvailableReq": {"actions": [{"actionType": "ActionType_PlayMdfc", "grpId": 90825}]}},
    )
    assert gs.pending_decision == "Priority"


def test_recent_history_detail_formats_local_time_and_metadata(monkeypatch):
    PySide6 = pytest.importorskip("PySide6")
    assert PySide6 is not None
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.compact_coach import CompactCoachPanel, _history_row_html
    from arenamcp.desktop.performance_tab import _fmt_ts

    _ = QApplication.instance() or QApplication([])
    rec = MatchRecord(
        match_id="62c1d526",
        timestamp="2026-11-25T14:42:16.298448+00:00",
        result="loss",
        opponent_name="Dillegent",
        format_name="Brawl Ladder",
        turns=20,
        deck_name="The Notary Hobbits Digital",
    )
    rendered = _history_row_html(rec)
    assert "LOSS" in rendered
    assert "Dillegent" in rendered
    assert "The Notary Hobbits Digital" in rendered
    assert "Brawl Ladder" in rendered
    assert "20t" in rendered
    assert _fmt_ts(rec.timestamp) in rendered
    assert "2026-11-25T14:42:16.298448+00:00" not in rendered

    panel = CompactCoachPanel()
    try:
        panel.rank_strip.recent["Constructed"] = [rec]
        panel._show_rank_history("Constructed")
        assert panel.detail_pager.title.text() == "Recent Constructed · newest first (ranked)"
        html = panel.detail_pager._html
        assert _fmt_ts(rec.timestamp) in html
        assert "2026-11-25T14:42:16" not in html
        assert "The Notary Hobbits Digital" in html

        # Control: empty list keeps empty fallback
        panel.rank_strip.recent["Limited"] = []
        panel._show_rank_history("Limited")
        assert panel.detail_pager.title.text() == "Recent Limited · newest first (ranked)"
        assert panel.detail_pager._html == "No recent ranked matches recorded."
    finally:
        panel.close()
        panel.deleteLater()
