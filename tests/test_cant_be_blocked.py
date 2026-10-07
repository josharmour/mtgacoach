"""Static "can't be blocked" grants reach the combat math (bug_20261006_184540).

Sealed FRA, match ef9761c6: Tetsuko Umezawa, Fugitive ("Creatures you control
with power or toughness 1 or less can't be blocked.") made Rank Rat 1/1,
Theoretical Necromancer 4/1, Yuriko 1/1 and Tetsuko 1/3 unblockable. Arena's
own Qualification annotation (QualificationType 32, CantBeBlocked, affector
Tetsuko) listed exactly those creatures, but every deterministic layer treated
them as blockable:

* T20 prompt: "If Thalia, the Survivor 3/4 blocks Rank Rat 1/1: BAD" and
  "Computed optimal attack: attack with nobody";
* T24 autopilot: "Losing-attack guard: not attacking with Yuriko ... Cadet 2/2
  can block Yuriko, Hope from the Shadows 1/1, kill it and survive";
* T26 coach: suggested only Yuriko and Tetsuko; the 4/1 Necromancer stayed home.

The boards below are the declare-attackers snapshots rebuilt by replaying
Player.log through arenamcp's LogParser + GameState.
"""

from __future__ import annotations

from copy import deepcopy

import arenamcp.server as server_module
from arenamcp.coach import CoachEngine
from arenamcp.combat_keywords import annotate_cant_be_blocked, can_be_blocked_by, own_text_unblockable
from arenamcp.combat_solver import optimal_attacks, optimal_blocks
from arenamcp.combat_strategy import combat_choice, losing_attackers

TETSUKO = "Creatures you control with power or toughness 1 or less can't be blocked."
DIVINER = "This creature enters prepared.\nWhenever you scry or surveil, this creature gets +1/+1 until end of turn."
NECROMANCER = (
    "{o3oB}, Exile this card from your graveyard: Return another target creature card from your "
    "graveyard to your hand."
)
YURIKO = (
    "Flash\nWhen Yuriko enters, choose one — \n•Target creature gets -X/-0 until end of turn, where X is "
    "the number of cards in your graveyard. \n•Surveil 2."
)
RESCUE_GIRL = "Flying\n{oT}: Return another target permanent you control to its owner's hand. Activate only during your turn."
RURIC = (
    "Flying\nProwess\nWhenever Ruric Thar becomes the target of a spell or ability an opponent controls, "
    "draw a card."
)
THALIA = "Lifelink\nNoncreature spells your opponents cast cost {o1} more to cast."
LAND = "({T}: Add {U}.)"

# (instance, name, controller, power, toughness, type line, rules text, tapped, entered)
T26_BOARD = [
    (203, "Island", 1, None, None, "Basic Land — Island", LAND, True, 2),
    (210, "Island", 1, None, None, "Basic Land — Island", LAND, True, 4),
    (
        284,
        "Rank Rat",
        1,
        1,
        1,
        "Creature — Zombie Rat",
        "When this creature enters, each opponent discards a card.",
        False,
        10,
    ),
    (305, "Theoretical Necromancer", 1, 4, 1, "Creature — Vampire Warlock", NECROMANCER, False, 12),
    (315, "Thalia, the Survivor", 2, 3, 4, "Legendary Creature — Human Soldier", THALIA, False, 13),
    (328, "Cadet", 1, 2, 2, "Token Creature — Wizard Soldier", "", False, 14),
    (
        332,
        "Rescue Girl, First Responder",
        2,
        1,
        3,
        "Legendary Creature — Human Cleric",
        RESCUE_GIRL,
        True,
        15,
    ),
    (363, "Tetsuko Umezawa, Fugitive", 1, 1, 3, "Legendary Creature — Human Rogue", TETSUKO, False, 18),
    (380, "Yuriko, Hope from the Shadows", 1, 1, 1, "Legendary Creature — Human Ninja", YURIKO, False, 20),
    (444, "Cadet", 2, 2, 2, "Token Creature — Wizard Soldier", "", False, 23),
    (445, "Cadet", 2, 2, 2, "Token Creature — Wizard Soldier", "", False, 23),
    (446, "Cadet", 2, 2, 2, "Token Creature — Wizard Soldier", "", False, 23),
    # Two surveils this turn made Diviner 2/2: no longer covered by Tetsuko.
    (469, "Diviner of Victory", 1, 2, 2, "Creature — Dwarf Wizard", DIVINER, False, 24),
    (474, "Ruric Thar, Biomagus", 2, 4, 6, "Legendary Creature — Ogre Crab Wizard", RURIC, False, 25),
    (
        494,
        "Geist of Saint Thalia",
        1,
        1,
        2,
        "Legendary Creature — Spirit Cleric",
        "Flying\nNoncreature spells you cast cost {o1} less to cast.",
        False,
        26,
    ),
]
T26_ATTACKERS = [284, 305, 328, 363, 380, 469]
UNBLOCKABLE_T26 = {284, 305, 363, 380, 494}  # Arena's Qualification affectedIds at T26


def _state(board, *, turn: int, our_life: int, opp_life: int, attackers: list[int]) -> dict:
    battlefield = [
        {
            "instance_id": iid,
            "name": name,
            "controller_seat_id": ctrl,
            "owner_seat_id": ctrl,
            "power": power,
            "toughness": toughness,
            "type_line": type_line,
            "card_types": ["CardType_Creature"] if "Creature" in type_line else ["CardType_Land"],
            "subtypes": type_line.partition("— ")[2].split(),
            "object_kind": "TOKEN" if type_line.startswith("Token") else "CARD",
            "oracle_text": text,
            "keywords": ["flying"] if text.startswith("Flying") else [],
            "is_tapped": tapped,
            "turn_entered_battlefield": entered,
        }
        for iid, name, ctrl, power, toughness, type_line, text, tapped, entered in board
    ]
    return {
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": turn,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Combat",
            "step": "DeclareAttack",
        },
        "players": [
            {"seat_id": 1, "life_total": our_life, "is_local": True, "lands_played": 1},
            {"seat_id": 2, "life_total": opp_life, "is_local": False, "lands_played": 1},
        ],
        "battlefield": battlefield,
        "hand": [],
        "graveyard": [],
        "pending_decision": "Declare Attackers",
        "decision_context": {
            "type": "declare_attackers",
            "legal_attacker_ids": attackers,
            "raw_attackers": [
                {
                    "attackerInstanceId": identity,
                    "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
                }
                for identity in attackers
            ],
        },
    }


def _t26() -> dict:
    return _state(T26_BOARD, turn=26, our_life=4, opp_life=17, attackers=T26_ATTACKERS)


def _t24() -> dict:
    """T24: Diviner just cast (1/1, sick), Yuriko not yet attacked; every land tapped."""
    board = [
        row
        for row in T26_BOARD
        if row[0] not in (474, 494)  # Ruric Thar and Geist arrived on T25/T26
    ]
    board = [(*row[:3], 1, 1, *row[5:]) if row[0] == 469 else row for row in board]
    board.append(
        (
            350,
            "Campus Crier",
            2,
            3,
            1,
            "Creature — Human Advisor",
            "{o1}, Exile this card from your graveyard: Empower Jace 2.",
            False,
            17,
        )
    )
    return _state(board, turn=24, our_life=5, opp_life=18, attackers=[284, 305, 328, 363, 380])


def _by_id(state: dict) -> dict[int, dict]:
    return {card["instance_id"]: card for card in state["battlefield"]}


def test_tetsuko_marks_exactly_what_arena_qualified():
    state = _t26()
    marked = annotate_cant_be_blocked(state["battlefield"])
    # 2/2 Diviner and Cadets fail "power or toughness 1 or less"; the
    # opponent's creatures are not ours to grant.
    assert set(marked) == UNBLOCKABLE_T26
    assert marked[284] == "Tetsuko Umezawa, Fugitive"
    assert "cant_be_blocked" not in _by_id(state)[469]


def test_mark_follows_current_power_and_toughness():
    state = _t26()
    annotate_cant_be_blocked(state["battlefield"])
    diviner = _by_id(state)[469]
    diviner["power"], diviner["toughness"] = 1, 1  # end of turn: back to 1/1
    annotate_cant_be_blocked(state["battlefield"])
    assert diviner["cant_be_blocked"]
    diviner["power"], diviner["toughness"] = 3, 3
    annotate_cant_be_blocked(state["battlefield"])
    assert "cant_be_blocked" not in diviner


def test_losing_attack_guard_keeps_the_unblockable_yuriko():
    # 18:42:47: "Losing-attack guard: not attacking with Yuriko, Hope from the
    # Shadows [380] (planned target: Opponent): Cadet 2/2 can block Yuriko ..."
    assert losing_attackers(_t24(), [380, 363]) == {}


def test_attack_search_sends_unblockable_damage_instead_of_holding():
    # Before the fix the recipient-aware search answered "hold all attackers".
    choice = combat_choice(_t26())
    assert choice is not None
    attackers = set(choice.assignments)
    assert attackers and attackers <= UNBLOCKABLE_T26
    assert 305 in attackers  # the 4/1 Necromancer is 4 damage nothing can block
    assert choice.player_damage == sum(_by_id(_t26())[i]["power"] for i in attackers)
    assert choice.crackback < 4  # and it still survives the crack-back at 4 life


def test_coach_prompt_shows_unblockable_attackers_not_impossible_blocks():
    context = CoachEngine.__new__(CoachEngine)._format_game_context(_t26(), for_planner=True)
    assert (
        "Unblockable (Tetsuko Umezawa, Fugitive): Rank Rat 1, Theoretical Necromancer 4, "
        "Tetsuko Umezawa, Fugitive 1, Yuriko, Hope from the Shadows 1 = 7 damage no blocker can stop"
    ) in context
    assert "  Rank Rat 1/1 [UNBLOCKABLE]" in context
    for name in ("Rank Rat", "Theoretical Necromancer", "Tetsuko Umezawa, Fugitive", "Yuriko"):
        assert f"blocks {name}" not in context
    # The 2/2 Diviner is not covered, so its block lines remain.
    assert "If Thalia, the Survivor 3/4 blocks Diviner of Victory 2/2: BAD" in context
    assert "vs 5blk" in context  # 7 unblockable damage is not lethal at 17


def test_unblockable_damage_alone_can_be_lethal():
    state = _t26()
    state["players"][1]["life_total"] = 7
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    assert "Atk: 6cr/11pwr vs LETHAL" in context


def test_snapshot_marks_unblockable_creatures(monkeypatch):
    state = _t26()
    snapshot = {
        "turn_info": state["turn"],
        "players": state["players"],
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "zones": {"battlefield": deepcopy(state["battlefield"])},
    }
    rows = {card["instance_id"]: card for card in state["battlefield"]}

    class _Snapshot:
        def ensure_local_seat_id(self):
            return None

        def get_published_snapshot(self, deep_copy=False):
            return snapshot

    monkeypatch.setattr(server_module, "watcher", object())
    monkeypatch.setattr(server_module, "game_state", _Snapshot())
    monkeypatch.setattr(server_module, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server_module, "_get_bridge_overlay", lambda **_kwargs: {})
    monkeypatch.setattr(server_module, "_serialize_snapshot_obj", lambda obj: dict(rows[obj["instance_id"]]))
    result = server_module.get_game_state()
    marked = {card["instance_id"] for card in result["battlefield"] if card.get("cant_be_blocked")}
    assert marked == UNBLOCKABLE_T26


# --- the rules text itself ------------------------------------------------------


def _creature(identity: int, power: int, toughness: int, text: str = "", **extra) -> dict:
    return {
        "instance_id": identity,
        "name": extra.pop("name", f"Creature {identity}"),
        "controller_seat_id": extra.pop("controller", 1),
        "power": power,
        "toughness": toughness,
        "type_line": extra.pop("type_line", "Creature — Human"),
        "oracle_text": text,
        **extra,
    }


def test_plain_cant_be_blocked_by_name_or_this_creature():
    assert own_text_unblockable(_creature(1, 1, 1, "Slither Blade can't be blocked.", name="Slither Blade"))
    assert own_text_unblockable(_creature(2, 2, 1, "This creature can't be blocked."))
    blade = _creature(3, 1, 1, "This creature can't be blocked.")
    plan = optimal_blocks([blade], [_creature(4, 5, 5, controller=2)], 20)
    assert plan.damage_through == 1 and not plan.assignments


def test_conditional_text_is_not_treated_as_unblockable():
    for text in (
        "As long as you control a Jace planeswalker, this creature gets +1/+1 and can't be blocked.",
        "{o1oU}: This creature can't be blocked this turn.",
        "This creature can't be blocked as long as it's attacking alone.",
        "This creature can't be blocked except by three or more creatures.",
    ):
        attacker = _creature(1, 2, 2, text)
        assert not own_text_unblockable(attacker), text
        assert annotate_cant_be_blocked([attacker]) == {}, text
        assert can_be_blocked_by(attacker, _creature(2, 2, 2, controller=2)), text


def test_checkable_per_blocker_restrictions():
    big_blockers = _creature(1, 3, 3, "This creature can't be blocked by creatures with power 2 or greater.")
    assert not can_be_blocked_by(big_blockers, _creature(2, 3, 4, controller=2))
    assert can_be_blocked_by(big_blockers, _creature(3, 1, 3, controller=2))

    wight = _creature(4, 2, 1, "Mist Wight can't be blocked except by Spirits.", name="Mist Wight")
    spirit = _creature(5, 1, 2, controller=2, type_line="Legendary Creature — Spirit Cleric")
    assert can_be_blocked_by(wight, spirit)
    assert not can_be_blocked_by(
        wight, _creature(6, 3, 4, controller=2, type_line="Creature — Human Soldier")
    )

    flyer_proof = _creature(7, 2, 2, "This creature can't be blocked by creatures with flying.")
    assert not can_be_blocked_by(flyer_proof, _creature(8, 1, 3, "Flying", controller=2))
    assert can_be_blocked_by(flyer_proof, _creature(9, 1, 3, controller=2))


def test_grants_follow_controller_other_and_attachments():
    ours = _creature(10, 1, 1)
    theirs_tetsuko = _creature(11, 1, 3, TETSUKO, controller=2, name="Tetsuko Umezawa, Fugitive")
    assert annotate_cant_be_blocked([ours, theirs_tetsuko]) == {11: "Tetsuko Umezawa, Fugitive"}

    lord = _creature(12, 2, 2, "Other creatures you control can't be blocked.", name="Lord")
    friend = _creature(13, 5, 5)
    assert annotate_cant_be_blocked([lord, friend]) == {13: "Lord"}

    aura = {
        "instance_id": 14,
        "name": "Aqueous Form",
        "controller_seat_id": 1,
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1 and can't be blocked.",
        "attached_to_id": 13,
    }
    assert annotate_cant_be_blocked([friend, aura]) == {13: "Aqueous Form"}
    aura["attached_to_id"] = 99
    assert annotate_cant_be_blocked([friend, aura]) == {}
    assert "cant_be_blocked" not in friend  # stale mark removed


def test_solver_cache_sees_the_mark():
    rat = _creature(20, 1, 1, name="Rank Rat")
    thalia = _creature(21, 3, 4, controller=2, name="Thalia, the Survivor")
    blocked = optimal_attacks([rat], [thalia], 17, 4, [], [])
    assert blocked.damage_through == 0
    rat["cant_be_blocked"] = "Tetsuko Umezawa, Fugitive"
    unblocked = optimal_attacks([rat], [thalia], 17, 4, [], [])
    assert unblocked.damage_through == 1 and unblocked.attacker_ids == [20]
