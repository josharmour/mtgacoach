"""The losing-attack guard keeps an attack that kills a planeswalker whatever they block (#127).

2026-10-19 13:01:35 (match 3e545401, T15): after Shock killed Ajani's
Pridemate, the plan sent Slickshot Show-Off 3/2 (flying) and Giant Cindermaw
4/3 (trample) at Ajani Resolute (loyalty 1). The opponent's only untapped
creature was Unflinching Hortimancer 7/6 with Angelic Destiny (flying, first
strike). It can block one attacker; the other kills Ajani. The guard priced
face damage only ("while it is blocked the rest of the attack gets only 3 more
damage through") and held both: "Not attacking." The planner called
``losing_attackers`` without the attack recipients, and so does the bridge's
submission filter. The board is the match's Player.log at the
DeclareAttackers request (gameStateId 341).
"""

from __future__ import annotations

import copy
import logging

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_strategy import _walker_target_id, losing_attackers

US, THEM = 2, 1
SLICKSHOT_TEXT = (
    "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.\n"
    "Plot {1}{R} (You may pay {1}{R} and exile this card from your hand. Cast it as a sorcery on a later "
    "turn without paying its mana cost. Plot only as a sorcery.)"
)
CINDERMAW_TEXT = (
    "Trample (This creature can deal excess combat damage to the player or planeswalker it's attacking.)\n"
    "Players can't gain life."
)
HORTIMANCER_TEXT = (
    "Ward {1} (Whenever this creature becomes the target of a spell or ability an opponent controls, "
    "counter it unless that player pays {1}.)\n"
    "Whenever you gain life, put a +1/+1 counter on this creature."
)
AJANI_TEXT = (
    "Whenever you gain life, put a loyalty counter on Ajani.\n[0]: You gain 1 life.\n"
    "[−4]: Create a 2/2 white Cat Soldier creature token named Ajani's Pridemate with \"Whenever you gain "
    'life, put a +1/+1 counter on this token."\n[−10]: You get an emblem with "Creatures you control get '
    '+2/+2."'
)
DESTINY_TEXT = (
    "Enchant creature\nEnchanted creature gets +4/+4, has flying and first strike, and is an Angel in "
    "addition to its other types.\nWhen enchanted creature dies, return this card to its owner's hand."
)
CONSULS_TEXT = (
    "Creatures your opponents control enter tapped.\n"
    "Whenever a creature an opponent controls enters, you gain 1 life."
)
AJANI_ID, SLICKSHOT_ID, CINDERMAW_ID, HORTIMANCER_ID = 341, 347, 360, 353


def creature(iid, name, power, toughness, seat, text="", *, type_line="Creature", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": text,
        "is_tapped": False,
        **extra,
    }


def permanent(iid, name, seat, type_line, card_types, text="", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": card_types,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": text,
        "is_tapped": False,
        **extra,
    }


def land(iid, name, seat, *, tapped=False):
    color = "R" if name == "Mountain" else "W"
    return permanent(
        iid,
        name,
        seat,
        f"Basic Land — {name}",
        ["CardType_Land"],
        f"({{T}}: Add {{{color}}}.)",
        is_tapped=tapped,
    )


def hortimancer(iid=HORTIMANCER_ID):
    return creature(
        iid,
        "Unflinching Hortimancer",
        7,
        6,
        THEM,
        HORTIMANCER_TEXT,
        type_line="Creature — Human Cleric Angel",
        keywords=["flying", "first strike", "ward"],
        counters={"CounterType_P1P1": 1},
    )


def recipients():
    return [
        {"type": "DamageRecType_Player", "playerSystemSeatId": THEM},
        {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": AJANI_ID},
    ]


def turn_15_board(*, ajani_loyalty=1, theirs=None) -> dict:
    """13:01:26, gsid 341: Shock resolved on the Pridemate (Slickshot 3/2 until end of turn)."""
    theirs = [hortimancer()] if theirs is None else theirs
    attackers = [
        {"attackerInstanceId": identity, "legalDamageRecipients": recipients()}
        for identity in (SLICKSHOT_ID, CINDERMAW_ID)
    ]
    return {
        "turn": {
            "turn_number": 15,
            "active_player": US,
            "priority_player": US,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "local_seat_id": US,
        "opponent_seat_id": THEM,
        "players": [
            {"seat_id": THEM, "life_total": 13, "is_local": False, "mana_pool": {}},
            {"seat_id": US, "life_total": 20, "is_local": True, "mana_pool": {}},
        ],
        "battlefield": [
            creature(
                SLICKSHOT_ID,
                "Slickshot Show-Off",
                3,
                2,
                US,
                SLICKSHOT_TEXT,
                type_line="Creature — Bird Wizard",
                keywords=["flying", "haste"],
            ),
            creature(
                CINDERMAW_ID,
                "Giant Cindermaw",
                4,
                3,
                US,
                CINDERMAW_TEXT,
                type_line="Creature — Dinosaur Beast",
                keywords=["trample"],
            ),
            land(281, "Mountain", US, tapped=True),
            *(land(iid, "Mountain", US) for iid in (293, 305, 333)),
            *theirs,
            permanent(
                AJANI_ID,
                "Ajani Resolute",
                THEM,
                "Legendary Planeswalker — Ajani",
                ["CardType_Planeswalker"],
                AJANI_TEXT,
                loyalty=ajani_loyalty,
                counters={"CounterType_Loyalty": ajani_loyalty},
            ),
            permanent(
                366,
                "Angelic Destiny",
                THEM,
                "Enchantment — Aura",
                ["CardType_Enchantment"],
                DESTINY_TEXT,
                attached_to=HORTIMANCER_ID,
            ),
            permanent(
                302, "Authority of the Consuls", THEM, "Enchantment", ["CardType_Enchantment"], CONSULS_TEXT
            ),
            *(land(iid, "Plains", THEM, tapped=True) for iid in (286, 299, 315, 327)),
            land(340, "Plains", THEM),
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"opponent_hand_count": 0},
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": ["Slickshot Show-Off", "Giant Cindermaw"],
            "legal_attacker_ids": [SLICKSHOT_ID, CINDERMAW_ID],
            "raw_attackers": attackers,
        },
    }


AT_AJANI = {SLICKSHOT_ID: "Ajani Resolute [341]", CINDERMAW_ID: "Ajani Resolute [341]"}
MODEL_PLAN = (
    "Kill Ajani's token engine; worst case trades Cindermaw for the walker. Remove Ajani to stop the token "
    "engine while racing with burn-backed attackers."
)


def test_both_attacks_at_ajani_are_kept_one_connects_whatever_hortimancer_blocks(caplog):
    with caplog.at_level(logging.INFO, logger="arenamcp.combat_strategy"):
        losing = losing_attackers(
            turn_15_board(), [SLICKSHOT_ID, CINDERMAW_ID], strategy_text=MODEL_PLAN, attacker_targets=AT_AJANI
        )
    assert losing == {}
    assert "Losing-attack guard: keeping Slickshot Show-Off" in caplog.text
    assert "Ajani Resolute (loyalty 1)" in caplog.text
    assert "it dies whatever they block" in caplog.text


def test_the_same_attack_at_their_face_is_still_held():
    # Face damage is what the guard priced live: Hortimancer eats either attacker for nothing.
    board = turn_15_board()
    live = losing_attackers(board, [SLICKSHOT_ID, CINDERMAW_ID], strategy_text=MODEL_PLAN)
    assert set(live) == {SLICKSHOT_ID, CINDERMAW_ID}
    # The live log's words (13:01:35), reproduced by this board without the recipients.
    assert live[CINDERMAW_ID] == (
        "Unflinching Hortimancer 7/6 can block Giant Cindermaw 4/3, kill it and survive; nothing gets "
        "through, it has no attack or death payoff, and no affordable trick could change that combat; "
        "while it is blocked the rest of the attack gets only 3 more damage through"
    )
    assert live[SLICKSHOT_ID].startswith(
        "Unflinching Hortimancer 7/6 can block Slickshot Show-Off 3/2, kill it"
    )
    face = {SLICKSHOT_ID: "Opponent", CINDERMAW_ID: "Opponent"}
    assert set(
        losing_attackers(board, [SLICKSHOT_ID, CINDERMAW_ID], strategy_text=MODEL_PLAN, attacker_targets=face)
    ) == {SLICKSHOT_ID, CINDERMAW_ID}


def test_a_lone_attacker_at_ajani_is_still_held():
    # One attacker, one blocker that eats it: Ajani survives, the creature dies for nothing.
    losing = losing_attackers(
        turn_15_board(), [CINDERMAW_ID], attacker_targets={CINDERMAW_ID: "Ajani Resolute [341]"}
    )
    assert set(losing) == {CINDERMAW_ID}


def test_a_walker_their_blocks_can_save_keeps_the_old_verdict():
    # Loyalty 5: Hortimancer on Cindermaw leaves 3 (Slickshot) on Ajani, who survives.
    losing = losing_attackers(
        turn_15_board(ajani_loyalty=5), [SLICKSHOT_ID, CINDERMAW_ID], attacker_targets=AT_AJANI
    )
    assert set(losing) == {SLICKSHOT_ID, CINDERMAW_ID}
    # A second first-striking flier blocks the other attacker: nothing reaches Ajani at 1.
    two = turn_15_board(theirs=[hortimancer(), hortimancer(354)])
    assert set(losing_attackers(two, [SLICKSHOT_ID, CINDERMAW_ID], attacker_targets=AT_AJANI)) == {
        SLICKSHOT_ID,
        CINDERMAW_ID,
    }


def test_a_split_attack_keeps_only_the_walker_side():
    # Cindermaw at Ajani alone: Hortimancer can eat it, so Ajani's attack only works with Slickshot too.
    board = turn_15_board()
    split = {SLICKSHOT_ID: "Ajani Resolute [341]", CINDERMAW_ID: "Opponent"}
    assert set(losing_attackers(board, [SLICKSHOT_ID, CINDERMAW_ID], attacker_targets=split)) == {
        SLICKSHOT_ID,
        CINDERMAW_ID,
    }


def test_recipients_resolve_from_labels_bridge_entries_and_ids():
    board = turn_15_board()
    cards = {card["instance_id"]: card for card in board["battlefield"]}
    for target in (
        "Ajani Resolute [341]",
        "Ajani Resolute",
        "ajani resolute",
        AJANI_ID,
        {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": AJANI_ID},
    ):
        assert _walker_target_id(target, cards, THEM) == AJANI_ID, target
    for target in (
        "Opponent",
        "",
        None,
        {"type": "DamageRecType_Player", "playerSystemSeatId": THEM},
        "Giant Cindermaw [360]",  # not a planeswalker
        HORTIMANCER_ID,
    ):
        assert _walker_target_id(target, cards, THEM) is None, target
    # Our own planeswalker is never theirs to lose.
    cards[AJANI_ID] = {**cards[AJANI_ID], "controller_seat_id": US, "owner_seat_id": US}
    assert _walker_target_id("Ajani Resolute [341]", cards, THEM) is None


def planned_attack(targets: dict[str, str] | None = None, target_names=None) -> ActionPlan:
    return ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Slickshot Show-Off", "Giant Cindermaw"],
                attacker_instance_ids=[SLICKSHOT_ID, CINDERMAW_ID],
                attacker_targets=dict(targets or {}),
                target_names=list(target_names or []),
                reasoning="Remove Ajani to stop the token engine while racing with burn-backed attackers.",
            )
        ],
        overall_strategy="Kill Ajani's token engine; worst case trades Cindermaw for the walker.",
    )


def test_planner_keeps_the_ajani_attack(caplog):
    state = turn_15_board()
    plan = planned_attack(
        {"Slickshot Show-Off": "Ajani Resolute [341]", "Giant Cindermaw": "Ajani Resolute [341]"}
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])
    assert "Losing-attack guard" not in caplog.text
    action = plan.actions[0]
    assert action.attacker_names == ["Slickshot Show-Off", "Giant Cindermaw"]
    assert action.attacker_targets == {
        "Slickshot Show-Off": "Ajani Resolute [341]",
        "Giant Cindermaw": "Ajani Resolute [341]",
    }
    assert plan.fallback_reason != "planner_losing_attack" and not action.guard_hold


def test_planner_reads_one_shared_target_name():
    state = turn_15_board()
    plan = planned_attack(target_names=["Ajani Resolute"])
    ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_names == ["Slickshot Show-Off", "Giant Cindermaw"]


def test_planner_still_holds_the_face_attack():
    state = turn_15_board()
    plan = planned_attack({"Slickshot Show-Off": "Opponent", "Giant Cindermaw": "Opponent"})
    ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_names == [] and plan.actions[0].guard_hold is True


def test_the_crackback_guard_lets_the_ajani_attack_stand():
    # Their crackback is Hortimancer's 7 into our 20: no reason to hold anything back.
    state = turn_15_board()
    plan = planned_attack(
        {"Slickshot Show-Off": "Ajani Resolute [341]", "Giant Cindermaw": "Ajani Resolute [341]"}
    )
    ActionPlanner.__new__(ActionPlanner)._check_attack_crackback(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_names == ["Slickshot Show-Off", "Giant Cindermaw"]
    assert plan.fallback_reason != "planner_crackback_guard"


def test_bridge_submission_keeps_the_attackers_aimed_at_ajani():
    from arenamcp.autopilot_bridge import _BridgeSubmitMixin

    state = turn_15_board()
    pending = {"attackers": copy.deepcopy(state["decision_context"]["raw_attackers"])}
    at_ajani = {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": AJANI_ID}
    entries = [
        {"attackerInstanceId": SLICKSHOT_ID, "damageRecipient": at_ajani},
        {"attackerInstanceId": CINDERMAW_ID, "damageRecipient": at_ajani},
    ]
    action = planned_attack(
        {"Slickshot Show-Off": "Ajani Resolute [341]", "Giant Cindermaw": "Ajani Resolute [341]"}
    )
    submitted, kept = _BridgeSubmitMixin()._prepare_attack_submission(
        action.actions[0], ["Slickshot Show-Off", "Giant Cindermaw"], entries, state, pending
    )
    assert [entry["attackerInstanceId"] for entry in kept] == [SLICKSHOT_ID, CINDERMAW_ID]
    assert submitted.attacker_targets == {
        "Slickshot Show-Off": "Ajani Resolute [341]",
        "Giant Cindermaw": "Ajani Resolute [341]",
    }
    # The same entries at their face are still filtered.
    face = {"type": "DamageRecType_Player", "playerSystemSeatId": THEM}
    _submitted, kept = _BridgeSubmitMixin()._prepare_attack_submission(
        action.actions[0],
        ["Slickshot Show-Off", "Giant Cindermaw"],
        [{**entry, "damageRecipient": face} for entry in entries],
        state,
        pending,
    )
    assert kept == []
