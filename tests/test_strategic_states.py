"""The real-state fixtures in tests/strategic_states.py stay faithful to their sources.

Each bug-report fixture is checked against an independent reference: the
report's own game_state, copied to tests/fixtures/ through an explicit
whitelist (never 'settings'). The fixture must reproduce it on every field the
strategic layer reads (board_assessment, board_model, combat_*, mulligan_policy,
game_plan, concede). A field the snapshot leaves empty (None, False, '', [],
{}) may be absent from the fixture: that layer reads every field with .get().
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests import strategic_states as fx

FIXTURES = Path(__file__).parent / "fixtures"
BUG_FIXTURES = {
    "bug_20261006_135027": fx.BUG_135027,
    "bug_20261006_174855": fx.BUG_174855,
    "bug_20261006_180436": fx.BUG_180436,
}
STRATEGIC_CARD_FIELDS = (
    "instance_id", "grp_id", "name", "modified_name", "owner_seat_id", "controller_seat_id", "type_line",
    "card_types", "subtypes", "colors", "modified_colors", "color_production", "mana_cost", "cmc",
    "oracle_text", "power", "toughness", "modified_power", "modified_toughness", "printed_power",
    "printed_toughness", "keywords", "is_tapped", "turn_entered_battlefield", "is_attacking", "is_blocking",
    "attack_target_id", "attached_to_id", "object_kind", "is_token", "is_phased_out", "counters", "damage",
    "loyalty", "parent_instance_id", "summoning_sickness", "cant_be_blocked", "rarity",
)  # fmt: skip
# Recorded by the bridge but read by nothing in the strategic layer (a printing id).
UNREAD_CARD_FIELDS = {"base_grp_id"}
TURN_FIELDS = ("turn_number", "active_player", "priority_player", "phase", "step")
PLAYER_FIELDS = ("seat_id", "life_total", "lands_played", "is_local", "mulligan_count")
STATE_FIELDS = (
    "match_id",
    "event_id",
    "format_name",
    "local_seat_id",
    "opponent_seat_id",
    "zones",
    "deck_cards",
)
ZONES = ("battlefield", "hand", "graveyard", "exile", "stack", "command")


def _collapse(text: str | None) -> str:
    """Arena's duplicated oracle variants (plain, <nobr>, <i>) collapsed to one line each."""
    lines: list[str] = []
    for line in (text or "").split("\n"):
        line = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", line)).strip()
        if line and line not in lines:
            lines.append(line)
    return "\n".join(lines)


def _value(card: dict, field: str):
    value = card.get(field)
    if field == "oracle_text":
        value = _collapse(value)
    if value is None or value is False or value in ("", [], {}):
        return None
    return value


def _reference(report: str) -> dict:
    return json.loads((FIXTURES / f"{report}_game_state.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("report", sorted(BUG_FIXTURES))
def test_bug_fixture_reproduces_the_reports_game_state(report):
    reference, fixture = _reference(report), BUG_FIXTURES[report]

    for field in STATE_FIELDS:
        assert fixture[field] == reference[field], field
    assert {f: fixture["turn"][f] for f in TURN_FIELDS} == {f: reference["turn"][f] for f in TURN_FIELDS}
    assert [{f: p.get(f) for f in PLAYER_FIELDS} for p in fixture["players"]] == [
        {f: p.get(f) for f in PLAYER_FIELDS} for p in reference["players"]
    ]
    for zone in ZONES:
        ours, recorded = fixture.get(zone) or [], reference.get(zone) or []
        assert [c["instance_id"] for c in ours] == [c["instance_id"] for c in recorded], zone
        for mine, theirs in zip(ours, recorded, strict=True):
            where = (zone, theirs["instance_id"], theirs["name"])
            # A card field the bridge starts publishing needs a decision: read or not.
            assert set(theirs) <= set(STRATEGIC_CARD_FIELDS) | UNREAD_CARD_FIELDS, where
            assert set(mine) <= set(STRATEGIC_CARD_FIELDS), where
            diffs = {
                field: (_value(mine, field), _value(theirs, field))
                for field in STRATEGIC_CARD_FIELDS
                if _value(mine, field) != _value(theirs, field)
            }
            assert not diffs, (where, diffs)


def test_bug_fixtures_keep_the_bridge_names():
    for fixture in BUG_FIXTURES.values():
        assert (fixture["turn"]["phase"], fixture["turn"]["step"]) == ("Main1", "None")
        assert all(
            not kind.startswith("CardType_") for c in fixture["battlefield"] for kind in c["card_types"]
        )


def test_reference_copies_hold_no_secrets_or_names():
    for path in sorted(FIXTURES.glob("bug_*_game_state.json")):
        text = path.read_text(encoding="utf-8")
        for needle in ("sk-", "license", "api_key", "settings", "opponent_name", "install_id"):
            assert needle not in text, (path.name, needle)


def test_new_fixtures_are_json_safe():
    for name in (
        "G1_T14_ON_STACK", "G1_T14_MODE_STATE", "G3_T10_BLOCKS", "BUG_135027", "BUG_174855", "BUG_180436",
    ):  # fmt: skip
        state = getattr(fx, name)
        assert json.loads(json.dumps(state)) == state, name


def test_phase_helpers_convert_both_ways():
    mac = fx.mac_phase(fx.G1_T14)
    assert (mac["turn"]["phase"], mac["turn"]["step"]) == ("Main1", "None")
    assert fx.log_phase(mac) == fx.G1_T14
    assert fx.G1_T14["turn"]["phase"] == "Phase_Main1"  # the source is untouched

    blocks = fx.mac_phase(fx.G3_T10_BLOCKS)
    assert (blocks["turn"]["phase"], blocks["turn"]["step"]) == ("Combat", "DeclareBlock")
    assert fx.log_phase(blocks) == fx.G3_T10_BLOCKS

    log = fx.log_phase(fx.BUG_180436)
    assert (log["turn"]["phase"], log["turn"]["step"]) == ("Phase_Main1", "")
    assert fx.mac_phase(log) == fx.BUG_180436
    assert fx.mac_phase(fx.BUG_180436) == fx.BUG_180436


def _our_tapped(state: dict) -> set[int]:
    return {c["instance_id"] for c in state["battlefield"] if c["controller_seat_id"] == 1 and c["is_tapped"]}


def test_after_cast_pays_like_arena():
    on_stack = fx.G1_T14_ON_STACK
    assert [(c["instance_id"], c["name"]) for c in on_stack["stack"]] == [(344, "Archive Arbiter")]
    assert 200 not in {c["instance_id"] for c in on_stack["hand"]}
    # Arena tapped these six for the {6} (Player.log gameStateId 342 ManaPaid).
    assert _our_tapped(on_stack) - _our_tapped(fx.G1_T14) == {289, 264, 233, 213, 201, 285}
    assert _our_tapped(on_stack) == _our_tapped(fx.G1_T14_MODE_STATE)
    assert "stack" not in fx.G1_T14 and 200 in {c["instance_id"] for c in fx.G1_T14["hand"]}


def test_after_cast_pays_coloured_pips_first():
    # Tetsuko {1}{U}: the Island pays U although both Forests come first.
    cast = fx.after_cast(fx.G1_T8, 247)
    assert _our_tapped(cast) == {201, 233}
    assert cast["stack"][0]["instance_id"] == 247
    with pytest.raises(ValueError):
        fx.after_cast(fx.G1_T8, 200)  # Archive Arbiter {6} with three lands


def test_mode_menu_is_the_recorded_decision():
    decision = fx.modal_decision(fx.G1_T14_MODE_MENU, request_id=fx.G1_T14_MODE_REQUEST_ID)
    assert decision.request_id == (344, 455)
    assert decision.request_type == "CastingTimeOptions"
    assert [(o.option_id, o.label) for o in decision.options] == [
        ("idx:0", "Mode 1: Destroy target noncreature, nonland permanent."),
        ("idx:1", "Mode 2: You gain 4 life."),
    ]
    assert {o.meta["sourceId"] for o in decision.options} == {344}
    assert decision.selection_is_valid(fx.G1_T14_MODE_CHOSEN)
    assert decision.selection_is_valid(["idx:1"])
    assert not decision.selection_is_valid(["idx:0", "idx:1"])


def test_mode_state_links_the_trigger_to_arbiter():
    (trigger,) = fx.G1_T14_MODE_STATE["stack"]
    assert (trigger["instance_id"], trigger["object_kind"], trigger["parent_instance_id"]) == (
        344,
        "ABILITY",
        337,
    )
    assert trigger["grp_id"] == fx.G1_T14_MODE_MENU[0][2]["abilityGrpId"]
    arbiter = next(c for c in fx.G1_T14_MODE_STATE["battlefield"] if c["instance_id"] == 337)
    assert (arbiter["name"], arbiter["turn_entered_battlefield"], arbiter["is_tapped"]) == (
        "Archive Arbiter",
        14,
        False,
    )
    assert [c["name"] for c in fx.G1_T14_MODE_STATE["hand"]] == ["Island", "Island", "Countersculpt"]


def test_trick_board_matches_the_log():
    state = fx.G3_T10_BLOCKS
    attackers = {c["instance_id"]: c for c in state["battlefield"] if c.get("is_attacking")}
    assert sorted(attackers) == state["decision_context"]["attacker_ids"] == [282, 287, 297]
    assert {(c["name"], c["power"], c["toughness"]) for c in attackers.values()} == {
        ("Pia, Determined Rebuilder", 2, 2),
        ("Thopter", 2, 2),
        ("Chandra's Emberling", 4, 4),
    }
    their_lands = {
        c["instance_id"]: c["is_tapped"]
        for c in state["battlefield"]
        if c["controller_seat_id"] == 2 and "Land" in c["type_line"]
    }
    assert their_lands == {307: False, 296: False, 281: False, 269: True, 262: True}
    assert state["zones"]["opponent_hand_count"] == 3
    blockers = {
        c["instance_id"]
        for c in state["battlefield"]
        if c["controller_seat_id"] == 1 and "Creature" in c["type_line"]
    }
    assert blockers == set(state["decision_context"]["legal_blocker_ids"]) >= set(fx.G3_T10_BLOCKS_CHOSEN)
