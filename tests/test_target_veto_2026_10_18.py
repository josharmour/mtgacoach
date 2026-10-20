"""Correct target picks refused as "no safe automatic choice", 2026-10-18.

standalone.log (ms timestamps) and the SelectTargetsReq payloads from
Player.log (log_archive/new_build_2026-10-18, current Player.log):

* 20:49:07.093 Spellbook Vendor's "Create a Sorcerer Role token attached to
  target creature you control" (ability 169024, source 613, AllowCancel_No):
  candidates 544 Spellbook Vendor (6/4, highlight ReplaceRole: it already
  wore our Sorcerer Role) and 595 Veteran Survivor (2/1, Tepid). The model
  chose tgt:544 without target_controllers; the gate logged "Declining
  unclassified target tgt:544 without explicit controller intent" and went
  MANUAL REQUIRED at 20:49:09.359. The backstop re-forced the window, the
  same choice was re-planned and refused again at 20:49:11.501 (a second
  Oops incident), the Oops emote went out at 20:49:19 and the user put the
  Role on Veteran Survivor (tgt:595) at 20:49:32. At 20:48:38 the same pick
  had gone through (the model then named the controller) and only replaced
  the Role Spellbook Vendor already wore.
* 15:06:25 the same trigger, three of our creatures, all Tepid: tgt:515
  (Skyward Spider) refused, MANUAL REQUIRED at 15:06:27.
* 20:23:47 The Grand Evolution, "Distribute seven +1/+1 counters among any
  number of target creatures you control" (166732): our 6/6 Vaultborn
  Tyrant (931) only; refused, MANUAL REQUIRED at 20:23:51.
* 19:21:21 Badgermole Cub, "When this creature enters, earthbend 1"
  (192752): our two Forests (445, 458); refused, MANUAL REQUIRED 19:21:27.
* 19:28:12 and 19:34:14-19:36:12 Utopia Sprawl ("Enchant Forest", 2330):
  every candidate one of our Forests, all Hot; refused and the cast
  cancelled seven times.
* 15:02:47 Sheltered by Ghosts ("Enchant creature you control", 1886): our
  Skyward Spider (343, Hot); refused (its enter trigger exiles an opponent's
  permanent, so the card read as mixed) and the cast cancelled.
* 14:52:14 Smell Fear, "Target creature you control fights up to one target
  creature you don't control" (141766): slot 1 our Rishkar (466), slot 2
  their Hero of Iroas (444) or Eriette (472), all Tepid; the pick
  [tgt:466, tgt:472] was refused (the request changed during planning).

Across both Player.logs (56 distinct target requests) Arena's highlight
was opp:Hot/own:Cold for every harmful effect, own:Hot/opp:Cold for every
buff, and Tepid for exactly the effects the text rules could not classify.
"""

from __future__ import annotations

import pytest

import arenamcp.action_planner as action_planner
from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import build_pending_decision
from arenamcp.target_effects import (
    effect_mentions_benefit,
    enchant_effect_text,
    target_effect_is_harmful,
    target_highlight,
)

ME, OPP = 1, 2

# Arena card DB rules text (Raw_CardDatabase, 2026-10-18).
ABILITIES = {
    169024: "Create a Sorcerer Role token attached to target creature you control.",
    166732: "Distribute seven +1/+1 counters among any number of target creatures you control.",
    192752: "When CARDNAME enters, earthbend 1.",
    2330: "Enchant Forest",
    1886: "Enchant creature you control",
    141766: "Target creature you control fights up to one target creature you don't control.",
}
SPELLBOOK_VENDOR = (
    "Vigilance\nAt the beginning of combat on your turn, you may pay {o1}. When you do, create a "
    "Sorcerer Role token attached to target creature you control."
)
GRAND_EVOLUTION = (
    "Mill ten cards. Put up to two creature cards from among the milled cards onto the battlefield.\n"
    "Distribute seven +1/+1 counters among any number of target creatures you control.\n"
    'Until end of turn, creatures you control gain "{o1}: This creature fights target creature you '
    "don't control.\" Exile this Saga, then return it to the battlefield <i>(front face up)</i>."
)
BADGERMOLE_CUB = (
    "When this creature enters, earthbend 1.\nWhenever you tap a creature for mana, add an additional {oG}."
)
UTOPIA_SPRAWL = (
    "Enchant Forest\nAs this Aura enters, choose a color.\nWhenever enchanted Forest is tapped for mana, "
    "its controller adds an additional one mana of the chosen color."
)
SHELTERED_BY_GHOSTS = (
    "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an opponent "
    "controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has lifelink "
    "and ward {o2}."
)
SMELL_FEAR = "Proliferate.\nTarget creature you control fights up to one target creature you don't control."


@pytest.fixture(autouse=True)
def _arena_ability_text(monkeypatch):
    monkeypatch.setattr(action_planner, "_ability_rules_text", lambda aid: ABILITIES.get(int(aid), ""))


def creature(iid, name, seat, power=2, toughness=2, type_line="Creature"):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def forest(iid, seat=ME):
    return creature(iid, "Forest", seat, None, None, "Basic Land — Forest")


def slots_of(*slots):
    """[(ability, [(iid, highlight), ...], min, max), ...] as Player.log records the targets."""
    return [
        {
            "targetIdx": index,
            "targets": [
                {
                    "targetInstanceId": iid,
                    "legalAction": "SelectAction_Select",
                    "highlight": f"HighlightType_{highlight}",
                }
                for iid, highlight in candidates
            ],
            "minTargets": minimum,
            "maxTargets": maximum,
            "targetingAbilityGrpId": ability,
            "targetingPlayer": ME,
        }
        for index, (ability, candidates, minimum, maximum) in enumerate(slots, start=1)
    ]


def poll_for(log_slots, *, gsid, msg, can_cancel, bridge_highlights=True):
    """The mac bridge's shape; the Windows plugin sends no highlight (bridge_highlights=False)."""
    selections, flat = [], []
    for selection in log_slots:
        entries = []
        for target in selection["targets"]:
            entry = {
                "targetInstanceId": target["targetInstanceId"],
                "targetIdx": selection["targetIdx"],
                "grpId": 0,
                "legalAction": "Select",
            }
            if bridge_highlights:
                entry["highlight"] = target["highlight"].removeprefix("HighlightType_")
            entries.append(entry)
        flat.extend(entries)
        selections.append(
            {
                "targetIdx": selection["targetIdx"],
                "minTargets": selection["minTargets"],
                "maxTargets": selection["maxTargets"],
                "selectedTargets": 0,
                "targets": entries,
            }
        )
    return {
        "has_pending": True,
        "request_type": "SelectTargets",
        "request_class": "SelectTargetsRequest",
        "game_state_id": gsid,
        "msg_id": msg,
        "can_cancel": can_cancel,
        "target_selections": selections,
        "target_candidates": flat,
    }


def state_for(log_slots, *, source, battlefield, stack, turn=13):
    return {
        "match_id": "2026-10-18",
        "local_seat_id": ME,
        "turn": {"turn_number": turn, "active_player": ME, "priority_player": ME, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": ME, "is_local": True, "life_total": 20},
            {"seat_id": OPP, "is_local": False, "life_total": 8},
        ],
        "battlefield": battlefield,
        "stack": stack,
        "hand": [],
        "graveyard": [],
        "exile": [],
        "command": [],
        "_bridge_request_payload": {
            "sourceId": source,
            "targetSelections": [
                {"targetIdx": s["targetIdx"], "targetingAbilityGrpId": s["targetingAbilityGrpId"]}
                for s in log_slots
            ],
        },
        "decision_context": {
            "type": "target_selection",
            "source_id": source,
            "targets": log_slots,
            "raw": {"targets": log_slots, "sourceId": source},
        },
    }


class _Backend:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.reply


def planner(reply):
    p = ActionPlanner.__new__(ActionPlanner)
    p._timeout = 1.0
    p._backend = _Backend(reply)
    return p


def pick(*ids, reason="pick"):
    quoted = ", ".join(f'"{oid}"' for oid in ids)
    return f'{{"option_ids": [{quoted}], "reasoning": "{reason}"}}'


# --- 20:49:07 Spellbook Vendor -----------------------------------------------------

VENDOR_SLOTS = slots_of((169024, [(544, "ReplaceRole"), (595, "Tepid")], 1, 1))


def vendor_state():
    return state_for(
        VENDOR_SLOTS,
        source=613,
        battlefield=[
            creature(544, "Spellbook Vendor", ME, 6, 4),
            creature(595, "Veteran Survivor", ME, 2, 1),
            creature(560, "Sorcerer Role", ME, None, None, "Enchantment Token — Aura Role"),
            creature(700, "Emrakul, the World Anew", OPP, 12, 12),
        ],
        stack=[
            {
                "instance_id": 613,
                "name": "Spellbook Vendor",
                "oracle_text": ABILITIES[169024],
                "parent_instance_id": 544,
                "controller_seat_id": ME,
                "source_card": {"oracle_text": SPELLBOOK_VENDOR},
            }
        ],
    )


@pytest.mark.parametrize("bridge_highlights", [True, False], ids=["mac-bridge", "log-only"])
def test_role_goes_to_the_creature_without_one_like_the_user_chose(bridge_highlights):
    decision = build_pending_decision(
        poll_for(VENDOR_SLOTS, gsid=333, msg=462, can_cancel=False, bridge_highlights=bridge_highlights)
    )
    p = planner(pick("tgt:544", reason="The Role is a helpful +1/+1 buff for my biggest attacker"))
    assert p.plan_decision_options(decision, vendor_state()) == ["tgt:595"]
    assert "Veteran Survivor" in p.get_decision_reasoning(["tgt:595"])


def test_the_model_is_told_which_creature_already_wears_the_role():
    decision = build_pending_decision(poll_for(VENDOR_SLOTS, gsid=333, msg=462, can_cancel=False))
    p = planner(pick("tgt:595"))
    assert p.plan_decision_options(decision, vendor_state()) == ["tgt:595"]
    vendor_line = next(line for line in p._backend.prompts[0].splitlines() if line.startswith("- tgt:544"))
    assert "already wears this Role" in vendor_line


def test_a_lone_role_wearer_still_gets_the_fresh_role():
    slots = slots_of((169024, [(544, "ReplaceRole")], 1, 1))
    state = vendor_state()
    state["decision_context"]["targets"] = state["decision_context"]["raw"]["targets"] = slots
    decision = build_pending_decision(poll_for(slots, gsid=333, msg=462, can_cancel=False))
    assert planner(pick("tgt:544")).plan_decision_options(decision, state) == ["tgt:544"]


def test_15_06_role_pick_among_our_creatures_is_submitted():
    slots = slots_of((169024, [(503, "Tepid"), (515, "Tepid"), (532, "Tepid")], 1, 1))
    state = state_for(
        slots,
        source=544,
        turn=19,
        battlefield=[
            creature(503, "Optimistic Scavenger", ME, 3, 2),
            creature(515, "Skyward Spider", ME, 2, 2),
            creature(532, "Spellbook Vendor", ME, 2, 2),
        ],
        stack=[
            {
                "instance_id": 544,
                "name": "Spellbook Vendor",
                "oracle_text": ABILITIES[169024],
                "parent_instance_id": 532,
                "controller_seat_id": ME,
            }
        ],
    )
    decision = build_pending_decision(poll_for(slots, gsid=564, msg=762, can_cancel=False))
    p = planner(pick("tgt:515", reason="The Role grants Skyward Spider evasion"))
    assert p.plan_decision_options(decision, state) == ["tgt:515"]


# --- 20:23:47 The Grand Evolution, 19:21:21 Badgermole Cub ------------------------------


def test_grand_evolution_counters_go_on_our_only_creature():
    slots = slots_of((166732, [(931, "Tepid")], 0, 7))
    state = state_for(
        slots,
        source=935,
        battlefield=[creature(931, "Vaultborn Tyrant", ME, 6, 6)],
        stack=[
            {
                "instance_id": 935,
                "name": "The Grand Evolution",
                "oracle_text": ABILITIES[166732],
                "parent_instance_id": 864,
                "controller_seat_id": ME,
                "source_card": {"oracle_text": GRAND_EVOLUTION},
            }
        ],
    )
    decision = build_pending_decision(poll_for(slots, gsid=361, msg=503, can_cancel=False))
    assert planner(pick("tgt:931")).plan_decision_options(decision, state) == ["tgt:931"]


def test_badgermole_cub_earthbends_our_forest():
    slots = slots_of((192752, [(445, "Tepid"), (458, "Tepid")], 1, 1))
    state = state_for(
        slots,
        source=462,
        turn=4,
        battlefield=[forest(445), forest(458), creature(459, "Badgermole Cub", ME, 2, 2)],
        stack=[
            {
                "instance_id": 462,
                "name": "Badgermole Cub",
                "oracle_text": ABILITIES[192752],
                "parent_instance_id": 459,
                "controller_seat_id": ME,
                "source_card": {"oracle_text": BADGERMOLE_CUB},
            }
        ],
    )
    decision = build_pending_decision(poll_for(slots, gsid=86, msg=119, can_cancel=False))
    assert planner(pick("tgt:445")).plan_decision_options(decision, state) == ["tgt:445"]


# --- Aura casts: 19:28:12 Utopia Sprawl, 15:02:47 Sheltered by Ghosts -------------------


def aura_state(slots, source, name, oracle, battlefield):
    return state_for(
        slots,
        source=source,
        battlefield=battlefield,
        stack=[{"instance_id": source, "name": name, "oracle_text": oracle, "controller_seat_id": ME}],
    )


@pytest.mark.parametrize("bridge_highlights", [True, False], ids=["mac-bridge", "log-only"])
def test_utopia_sprawl_enchants_our_forest(bridge_highlights):
    ids = (458, 477, 571, 688, 904, 918, 929, 969)
    slots = slots_of((2330, [(iid, "Hot") for iid in ids], 1, 1))
    state = aura_state(slots, 970, "Utopia Sprawl", UTOPIA_SPRAWL, [forest(iid) for iid in ids])
    decision = build_pending_decision(
        poll_for(slots, gsid=474, msg=643, can_cancel=True, bridge_highlights=bridge_highlights)
    )
    assert planner(pick("tgt:969")).plan_decision_options(decision, state) == ["tgt:969"]


def test_sheltered_by_ghosts_enchants_our_creature():
    slots = slots_of((1886, [(343, "Tepid")], 1, 1))  # live: Hot; Tepid proves the text alone suffices
    state = aura_state(
        slots,
        373,
        "Sheltered by Ghosts",
        SHELTERED_BY_GHOSTS,
        [
            creature(343, "Skyward Spider", ME, 7, 7),
            creature(900, "Authority of the Consuls", OPP, None, None),
        ],
    )
    decision = build_pending_decision(poll_for(slots, gsid=167, msg=226, can_cancel=True))
    assert planner(pick("tgt:343")).plan_decision_options(decision, state) == ["tgt:343"]


# --- 14:52:14 Smell Fear: one slot each side ------------------------------------------


def test_smell_fear_fights_their_creature_with_ours():
    slots = slots_of(
        (141766, [(466, "Tepid")], 1, 1),
        (141766, [(444, "Tepid"), (472, "Tepid")], 0, 1),
    )
    state = state_for(
        slots,
        source=501,
        battlefield=[
            creature(466, "Rishkar, Peema Renegade", ME, 6, 6),
            creature(444, "Hero of Iroas", OPP, 6, 5),
            creature(472, "Eriette of the Charmed Apple", OPP, 2, 4),
        ],
        stack=[
            {"instance_id": 501, "name": "Smell Fear", "oracle_text": SMELL_FEAR, "controller_seat_id": ME}
        ],
    )
    decision = build_pending_decision(poll_for(slots, gsid=241, msg=319, can_cancel=True))
    assert planner(pick("tgt:466", "tgt:472")).plan_decision_options(decision, state) == [
        "tgt:466",
        "tgt:472",
    ]


# --- what still declines, and the blind fallback ---------------------------------------


def test_a_cold_pick_without_a_reason_moves_to_arenas_good_target():
    # Veteran Survivor (20:50:00): our own graveyard cards Cold, theirs Hot.
    slots = slots_of((174311, [(564, "Cold"), (563, "Hot")], 0, 1))
    state = state_for(
        slots,
        source=623,
        battlefield=[],
        stack=[
            {
                "instance_id": 623,
                "name": "Veteran Survivor",
                "oracle_text": "exile up to one target card from a graveyard.",
                "controller_seat_id": ME,
            }
        ],
    )
    state["graveyard"] = [
        creature(564, "Feather of Flight", ME, None, None, "Enchantment — Aura"),
        creature(563, "Bitter Triumph", OPP, None, None, "Instant"),
    ]
    decision = build_pending_decision(poll_for(slots, gsid=358, msg=512, can_cancel=True))
    assert planner(pick("tgt:564")).plan_decision_options(decision, state) == ["tgt:563"]


def test_blind_fallback_puts_the_role_on_a_creature_without_one():
    decision = build_pending_decision(poll_for(VENDOR_SLOTS, gsid=333, msg=462, can_cancel=False))
    assert planner("no json").plan_decision_options(decision, vendor_state()) == ["tgt:595"]


def test_blind_fallback_for_an_unclassified_one_sided_slot():
    slots = slots_of((555, [(445, "Tepid"), (458, "Tepid")], 1, 1))
    ABILITIES[555] = "Target land you control becomes a 3/3 Elemental creature until end of turn."
    try:
        state = state_for(
            slots,
            source=462,
            battlefield=[forest(445), {**forest(458), "power": 0}],
            stack=[
                {
                    "instance_id": 462,
                    "name": "Animate",
                    "oracle_text": ABILITIES[555],
                    "controller_seat_id": ME,
                }
            ],
        )
        decision = build_pending_decision(poll_for(slots, gsid=86, msg=119, can_cancel=False))
        assert target_effect_is_harmful(ABILITIES[555]) is None
        assert planner("no json").plan_decision_options(decision, state) in (["tgt:445"], ["tgt:458"])
    finally:
        del ABILITIES[555]


def test_text_that_could_buff_their_permanent_still_declines():
    slots = slots_of((556, [(445, "Tepid"), (700, "Tepid")], 1, 1))
    ABILITIES[556] = "Target permanent phases out."
    try:
        state = state_for(
            slots,
            source=462,
            battlefield=[forest(445), creature(700, "Grizzly Bears", OPP)],
            stack=[
                {"instance_id": 462, "name": "Odd", "oracle_text": ABILITIES[556], "controller_seat_id": ME}
            ],
        )
        decision = build_pending_decision(poll_for(slots, gsid=86, msg=119, can_cancel=False))
        p = planner(pick("tgt:700"))
        assert p.plan_decision_options(decision, state) == [DECLINE_DECISION]
        assert p.get_last_decision_trace()["ambiguous_target_pick"] == ["tgt:700"]
    finally:
        del ABILITIES[556]


# --- the text rules -------------------------------------------------------------------


def test_role_counters_earthbend_and_aura_texts_classify():
    assert target_effect_is_harmful(ABILITIES[169024]) is False
    assert (
        target_effect_is_harmful("Create a Monster Role token attached to up to one target creature.")
        is False
    )
    assert target_effect_is_harmful("Create a Cursed Role token attached to target creature.") is True
    assert target_effect_is_harmful(ABILITIES[166732]) is False
    assert target_effect_is_harmful(ABILITIES[192752]) is False
    assert target_effect_is_harmful(enchant_effect_text("Enchant Forest", UTOPIA_SPRAWL)) is False
    assert (
        target_effect_is_harmful(enchant_effect_text("Enchant creature you control", SHELTERED_BY_GHOSTS))
        is False
    )
    assert target_effect_is_harmful(SHELTERED_BY_GHOSTS) is None  # the whole card stays mixed
    pacifism = "Enchant creature\nEnchanted creature can't attack or block."
    assert target_effect_is_harmful(enchant_effect_text("Enchant creature", pacifism)) is True
    assert enchant_effect_text(ABILITIES[169024], SPELLBOOK_VENDOR) == ""


def test_benefit_wording_and_highlight_names():
    assert effect_mentions_benefit("Target player draws two cards.")
    assert effect_mentions_benefit("Untap target permanent.")
    assert not effect_mentions_benefit("Target player mills three cards.")
    assert not effect_mentions_benefit("Choose two target permanents.")
    assert target_highlight("HighlightType_ReplaceRole") == "ReplaceRole"
    assert target_highlight("Hot") == target_highlight(3) == "Hot"
    assert target_highlight("HighlightType_None") == target_highlight(None) == ""
