"""Regressions from the 2026-11-25 audit: token removal, Auras hexproof/timing, and Unranked strip."""

from __future__ import annotations

import json

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.cast_notes import trick_timing_note
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.match_history import MatchHistory, MatchRecord
from arenamcp.stack_protection import review_hexproof_pass_claim

ME, THEM = 1, 2

SEAM_RIP_ORACLE = (
    "When this enchantment enters, exile target nonland permanent an opponent controls "
    "with mana value 2 or less until this enchantment leaves the battlefield."
)
SHELTERED_ORACLE = (
    "Enchant creature you control\n"
    "When this Aura enters, exile target nonland permanent an opponent controls until this Aura leaves the battlefield.\n"
    "Enchanted creature gets +1/+0 and has lifelink and ward {2}."
)
RESCUE_ORACLE = (
    "Flash\nEnchant creature you control\n"
    "As long as this Aura entered this turn, enchanted creature has hexproof.\n"
    "Enchanted creature gets +1/+1."
)


def _seam_rip_hand(iid: int = 301) -> dict:
    return {
        "instance_id": iid,
        "name": "Seam Rip",
        "controller_seat_id": ME,
        "type_line": "Enchantment",
        "mana_cost": "{W}",
        "oracle_text": SEAM_RIP_ORACLE,
    }


def _sheltered_hand(iid: int = 302) -> dict:
    return {
        "instance_id": iid,
        "name": "Sheltered by Ghosts",
        "controller_seat_id": ME,
        "type_line": "Enchantment — Aura",
        "mana_cost": "{1}{W}",
        "oracle_text": SHELTERED_ORACLE,
    }


def _rescue_hand(iid: int = 364) -> dict:
    return {
        "instance_id": iid,
        "name": "Shardmage's Rescue",
        "controller_seat_id": ME,
        "type_line": "Enchantment — Aura",
        "mana_cost": "{W}",
        "oracle_text": RESCUE_ORACLE,
    }


def _cast_menu(card: dict) -> PendingDecision:
    return PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                f"Cast {card['name']}",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": card["instance_id"]},
            ),
            DecisionOption("pass", "Pass"),
        ),
        can_pass=True,
    )


def _base_state(
    *,
    hand: list[dict],
    opp_bf: list[dict],
    our_bf: list[dict] | None = None,
    our_life: int = 20,
    phase: str = "Phase_Main1",
    stack: list[dict] | None = None,
) -> dict:
    return {
        "local_seat_id": ME,
        "turn": {"turn_number": 10, "active_player": ME, "phase": phase, "step": ""},
        "players": [
            {"seat_id": ME, "is_local": True, "life_total": our_life},
            {"seat_id": THEM, "is_local": False, "life_total": 20},
        ],
        "battlefield": [*(our_bf or []), *opp_bf],
        "hand": hand,
        "stack": stack or [],
    }


# --- 1. Disposable-token removal safeguard -------------------------------------------


def test_holds_seam_rip_when_only_small_disposable_tokens_are_legal():
    """2026-11-25 Kayss29 T10 & Arthurian UFO T17: don't burn Seam Rip on 1/1 Kithkin/Illusion tokens."""
    planner = ActionPlanner.__new__(ActionPlanner)
    seam_rip = _seam_rip_hand(301)
    ajani = {
        "instance_id": 401,
        "name": "Ajani, Outland Chaperone",
        "controller_seat_id": THEM,
        "type_line": "Legendary Planeswalker — Ajani",
        "mana_cost": "{1}{W}{W}",
        "card_types": ["CardType_Planeswalker"],
        "oracle_text": "[+1]: Create a 1/1 green and white Kithkin creature token.",
    }
    kithkin = {
        "instance_id": 402,
        "name": "Kithkin",
        "controller_seat_id": THEM,
        "type_line": "Token Creature — Kithkin",
        "card_types": ["CardType_Creature"],
        "object_kind": "TOKEN",
        "power": 1,
        "toughness": 1,
        "oracle_text": "",
    }
    kayss_state = _base_state(hand=[seam_rip], opp_bf=[ajani, kithkin])
    filtered, _held = planner._hold_main_phase_burn(_cast_menu(seam_rip), kayss_state)
    assert [o.option_id for o in filtered.options] == ["pass"]
    assert "Kithkin" in planner._last_decision_reasoning
    assert any("Kithkin" in n for n in planner._held_notes)

    jace = {
        "instance_id": 403,
        "name": "Jace, the Mind Sculptor",
        "controller_seat_id": THEM,
        "type_line": "Legendary Planeswalker — Jace",
        "mana_cost": "{2}{U}{U}",
        "card_types": ["CardType_Planeswalker"],
    }
    illusion = {
        "instance_id": 404,
        "name": "Illusion",
        "controller_seat_id": THEM,
        "type_line": "Token Creature — Illusion",
        "card_types": ["CardType_Creature"],
        "is_token": True,
        "power": 1,
        "toughness": 1,
        "oracle_text": "",
    }
    treasure = {
        "instance_id": 405,
        "name": "Treasure",
        "controller_seat_id": THEM,
        "type_line": "Token Artifact — Treasure",
        "card_types": ["CardType_Artifact"],
        "is_token": True,
        "oracle_text": "{T}, Sacrifice this artifact: Add one mana of any color.",
    }
    ufo_state = _base_state(hand=[seam_rip], opp_bf=[jace, illusion, treasure])
    filtered_ufo, _ = planner._hold_main_phase_burn(_cast_menu(seam_rip), ufo_state)
    assert [o.option_id for o in filtered_ufo.options] == ["pass"]


def test_disposable_token_controls_do_not_hold_seam_rip_or_auras():
    """Opposite controls: real MV<=2 targets, 4/4/flying tokens, own attackers, Eerie, low life, or Auras."""
    planner = ActionPlanner.__new__(ActionPlanner)
    seam_rip = _seam_rip_hand(301)
    kithkin = {
        "instance_id": 402,
        "name": "Kithkin",
        "controller_seat_id": THEM,
        "type_line": "Token Creature — Kithkin",
        "card_types": ["CardType_Creature"],
        "object_kind": "TOKEN",
        "power": 1,
        "toughness": 1,
        "oracle_text": "",
    }

    # 1) Real non-token MV<=2 permanent exists alongside token.
    hired_claw = {
        "instance_id": 410,
        "name": "Hired Claw",
        "controller_seat_id": THEM,
        "type_line": "Creature — Lizard Mercenary",
        "mana_cost": "{R}",
        "card_types": ["CardType_Creature"],
        "object_kind": "CARD",
        "power": 1,
        "toughness": 2,
    }
    # 2) Large or flying token.
    angel_token = {
        "instance_id": 411,
        "name": "Angel",
        "controller_seat_id": THEM,
        "type_line": "Token Creature — Angel",
        "card_types": ["CardType_Creature"],
        "is_token": True,
        "power": 4,
        "toughness": 4,
        "keywords": ["flying"],
    }
    for opp_cards in ([kithkin, hired_claw], [angel_token]):
        kept, reasons = planner._hold_main_phase_burn(
            _cast_menu(seam_rip), _base_state(hand=[seam_rip], opp_bf=opp_cards)
        )
        assert [o.option_id for o in kept.options] == ["idx:0", "pass"]
        assert reasons == []

    # 3) Untapped attacker of ours (removing chump blocker helps attack) or Eerie creature.
    veteran = {
        "instance_id": 201,
        "name": "Veteran Survivor",
        "controller_seat_id": ME,
        "type_line": "Creature — Human Survivor",
        "card_types": ["CardType_Creature"],
        "power": 2,
        "toughness": 1,
        "is_tapped": False,
        "summoning_sickness": False,
    }
    scavenger = {
        "instance_id": 202,
        "name": "Optimistic Scavenger",
        "controller_seat_id": ME,
        "type_line": "Creature — Human Scout",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 1,
        "is_tapped": True,
        "oracle_text": "Eerie — Whenever an enchantment you control enters, put a +1/+1 counter on target creature.",
    }
    for our_card in (veteran, scavenger):
        kept, _ = planner._hold_main_phase_burn(
            _cast_menu(seam_rip), _base_state(hand=[seam_rip], opp_bf=[kithkin], our_bf=[our_card])
        )
        assert [o.option_id for o in kept.options] == ["idx:0", "pass"]

    # 4) Low life vs board power.
    kept_low, _ = planner._hold_main_phase_burn(
        _cast_menu(seam_rip), _base_state(hand=[seam_rip], opp_bf=[kithkin], our_life=5)
    )
    assert [o.option_id for o in kept_low.options] == ["idx:0", "pass"]

    # 5) Removal Aura (Sheltered by Ghosts) is never held by the non-aura token guard.
    sheltered = _sheltered_hand(302)
    kept_aura, _ = planner._hold_main_phase_burn(
        _cast_menu(sheltered), _base_state(hand=[sheltered], opp_bf=[kithkin])
    )
    assert [o.option_id for o in kept_aura.options] == ["idx:0", "pass"]


# --- 2A. Stack hexproof pass recheck --------------------------------------------------


def test_review_hexproof_pass_claim_reasks_on_retroactive_myth():
    """2026-11-24 Arthurian UFO T21: passing because Get Lost is 'already on the stack' triggers CR 608.2b recheck."""
    rescue = _rescue_hand(364)
    scavenger = {
        "instance_id": 449,
        "name": "Optimistic Scavenger",
        "controller_seat_id": ME,
        "type_line": "Creature — Human Scout",
    }
    state = _base_state(
        hand=[rescue],
        our_bf=[scavenger],
        opp_bf=[],
        stack=[{"instance_id": 500, "name": "Get Lost", "controller_seat_id": THEM}],
    )
    menu = _cast_menu(rescue)
    asked: list[str] = []

    def fake_ask(prompt, **_kwargs):
        asked.append(prompt)
        return json.dumps({"option_ids": ["idx:0"], "reasoning": "Cast Rescue to fizzle Get Lost."})

    trace: dict = {}
    revised, picked = review_hexproof_pass_claim(
        menu,
        state,
        {
            "option_ids": ["pass"],
            "reasoning": "Get Lost is already on the stack targeting Scavenger, so Rescue's hexproof can't retroactively protect it.",
        },
        ["pass"],
        fake_ask,
        lambda raw: (json.loads(raw), json.loads(raw)["option_ids"]),
        "PROMPT",
        trace,
    )
    assert len(asked) == 1
    assert "CR 608.2b" in asked[0]
    assert picked == ["idx:0"]
    assert trace["stack_hexproof_check"]["revised_ids"] == ["idx:0"]


def test_review_hexproof_pass_claim_skips_when_nowhere_to_run_or_counterspell():
    """Opposite controls: Nowhere to Run disables hexproof; counterspell reasoning is valid."""
    rescue = _rescue_hand(364)
    menu = _cast_menu(rescue)
    nowhere = {
        "instance_id": 600,
        "name": "Nowhere to Run",
        "controller_seat_id": THEM,
        "type_line": "Enchantment",
        "oracle_text": "Flash\nCreatures your opponents control can't have hexproof or ward.",
    }
    state_nowhere = _base_state(
        hand=[rescue],
        opp_bf=[nowhere],
        stack=[{"instance_id": 500, "name": "Get Lost", "controller_seat_id": THEM}],
    )
    asked = False

    def fail_ask(*_args, **_kwargs):
        nonlocal asked
        asked = True
        raise AssertionError("should not re-ask")

    _d, picked = review_hexproof_pass_claim(
        menu,
        state_nowhere,
        {"reasoning": "Rescue's hexproof can't protect it while Nowhere to Run is out."},
        ["pass"],
        fail_ask,
        lambda x: x,
        "PROMPT",
        {},
    )
    assert not asked and picked == ["pass"]

    state_counter = _base_state(
        hand=[rescue],
        opp_bf=[],
        stack=[{"instance_id": 501, "name": "Spell Snare", "controller_seat_id": THEM}],
    )
    _d2, picked2 = review_hexproof_pass_claim(
        menu,
        state_counter,
        {
            "reasoning": "Spell Snare is countering our Saga, so Rescue's hexproof can't protect the spell on the stack."
        },
        ["pass"],
        fail_ask,
        lambda x: x,
        "PROMPT",
        {},
    )
    assert not asked and picked2 == ["pass"]


# --- 2B. Anti-hexproof & Main 2 flash hexproof timing notes ---------------------------


def test_trick_timing_note_warns_on_nowhere_to_run_and_main2_flash_hexproof():
    """2026-11-24 Robdogg T5 (Nowhere to Run) & Shadoh T8 (Main 2 empty-stack flash Rescue)."""
    rescue = _rescue_hand(364)
    nowhere = {
        "instance_id": 600,
        "name": "Nowhere to Run",
        "controller_seat_id": THEM,
        "type_line": "Enchantment",
        "oracle_text": "Creatures your opponents control can't have hexproof or ward.",
    }
    note_nowhere = trick_timing_note(
        _base_state(hand=[rescue], opp_bf=[nowhere], phase="Phase_Ending"),
        rescue,
    )
    assert "HEXPROOF DISABLED" in note_nowhere and "Nowhere to Run" in note_nowhere

    note_main2 = trick_timing_note(
        _base_state(hand=[rescue], opp_bf=[], phase="Phase_Main2", stack=[]),
        rescue,
    )
    assert "MAIN 2" in note_main2

    # Controls: Main 2 with opposing stack spell does NOT warn Main 2; no Nowhere to Run does NOT warn disabled.
    note_stack = trick_timing_note(
        _base_state(
            hand=[rescue],
            opp_bf=[],
            phase="Phase_Main2",
            stack=[{"instance_id": 700, "name": "Go for the Throat", "controller_seat_id": THEM}],
        ),
        rescue,
    )
    assert "MAIN 2" not in note_stack
    assert "HEXPROOF DISABLED" not in note_stack


# --- 3. Unranked strip & history detail -----------------------------------------------


def test_rank_strip_surfaces_unranked_wins_separately_from_ranked_losses(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    import arenamcp.match_history as mh
    from arenamcp.desktop.compact_coach import CompactCoachPanel

    path = tmp_path / "history.json"
    monkeypatch.setattr(mh, "HISTORY_FILE", path)
    monkeypatch.setenv("MTGA_LOG_PATH", str(tmp_path / "missing.log"))
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")

    h = MatchHistory()
    for i in range(10):
        h.add_record(
            MatchRecord(
                match_id=f"r{i}",
                format_name="Ladder",
                result="loss",
                deck_name="Mono-White Auras",
                opponent_name=f"RankedOpp{i}",
                timestamp="2026-11-25T06:44:13.100730+00:00",
            )
        )
    for i in range(8):
        h.add_record(
            MatchRecord(
                match_id=f"u{i}",
                format_name="Brawl Historic",
                result="win",
                deck_name="Michelangelo, Weirdness to 11",
                opponent_name=f"BrawlOpp{i}",
                turns=12,
                timestamp="2026-11-25T18:18:18.613008+00:00",
            )
        )

    _ = QApplication.instance() or QApplication([])
    panel = CompactCoachPanel()
    try:
        panel.resize(420, 720)
        panel.show()
        strip = panel.rank_strip
        before = strip.size()
        strip.refresh()
        assert strip.size() == before
        assert "L L L L L L L L L L" in strip.buttons["Constructed"].text()
        assert "Unranked" in strip.unranked_button.text()
        assert "W W W W W W W W" in strip.unranked_button.text()

        strip.unranked_button.click()
        assert panel.detail_pager.title.text() == "Recent Unranked · newest first"
        assert "Michelangelo, Weirdness to 11" in panel.detail_pager._html
        assert "BrawlOpp7" in panel.detail_pager._html

        strip.buttons["Constructed"].click()
        assert panel.detail_pager.title.text() == "Recent Constructed · newest first (ranked)"
        assert "Mono-White Auras" in panel.detail_pager._html
    finally:
        panel.close()
        panel.deleteLater()
