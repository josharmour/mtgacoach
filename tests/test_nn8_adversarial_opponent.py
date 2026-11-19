"""NN8 tests: format-agnostic public adversarial opponent posture, pass signals, and race-aware constraints."""

from __future__ import annotations

import concurrent.futures
import json
from copy import deepcopy

from arenamcp import opponent_tricks as ot
from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import assess
from arenamcp.decisions import build_pending_decision
from arenamcp.game_plan import GamePlanManager
from arenamcp.match_context import (
    prepare_match_context,
    reset_seen_rules,
    shared_match_system_prompt,
    split_deck_reference,
)
from arenamcp.opponent_read import OpponentRead


class _CaptureBackend:
    def __init__(self, reply: str = '{"option_ids": ["pass"], "reasoning": "pass"}') -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, system_prompt: str, user_message: str, max_tokens: int = 4096, **kwargs) -> str:
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_message,
                "class": kwargs.get("call_class"),
                "max_tokens": max_tokens,
            }
        )
        return self.reply


def _basic_land(iid: int, name: str, seat: int, *, tapped: bool = False, entered: int = 1) -> dict:
    colors = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
    color = colors[name]
    return {
        "instance_id": iid,
        "grp_id": 90000 + iid,
        "name": name,
        "type_line": f"Basic Land — {name}",
        "card_types": ["CardType_Land"],
        "oracle_text": f"({{T}}: Add {{{color}}}.)",
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "object_kind": "CARD",
        "is_tapped": tapped,
        "turn_entered_battlefield": entered,
    }


def _creature(
    iid: int,
    name: str,
    seat: int,
    power: int,
    toughness: int,
    *,
    tapped: bool = False,
    entered: int = 1,
    mana_cost: str = "{1}{G}",
    oracle_text: str = "",
    keywords: list[str] | None = None,
    grp_id: int | None = None,
) -> dict:
    return {
        "instance_id": iid,
        "grp_id": grp_id or (80000 + iid),
        "name": name,
        "type_line": "Creature — Bear",
        "card_types": ["CardType_Creature"],
        "mana_cost": mana_cost,
        "cmc": 2,
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle_text,
        "keywords": list(keywords or []),
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "object_kind": "CARD",
        "is_tapped": tapped,
        "turn_entered_battlefield": entered,
    }


def _constructed_state(
    *,
    match_id: str = "nn8-constructed",
    game_number: int = 1,
    turn: int = 5,
    active: int = 1,
    phase: str = "Phase_Main1",
    step: str = "",
    our_life: int = 20,
    opp_life: int = 12,
    opp_hand: int = 3,
    opp_tapped: bool = False,
    our_creatures: list[dict] | None = None,
    opp_creatures: list[dict] | None = None,
    opp_lands: list[dict] | None = None,
    extra_opp_permanents: list[dict] | None = None,
    graveyard: list[dict] | None = None,
    stack: list[dict] | None = None,
    seq: int | None = None,
) -> dict:
    ours = (
        our_creatures
        if our_creatures is not None
        else [
            _creature(101, "Alpha Vanguard", 1, 3, 3, entered=1, grp_id=101),
            _creature(102, "Beta Vanguard", 1, 3, 3, entered=3, grp_id=102),
            _creature(103, "Gamma Vanguard", 1, 2, 2, entered=3, grp_id=103),
        ]
    )
    lands = (
        opp_lands
        if opp_lands is not None
        else [
            _basic_land(201, "Plains", 2, tapped=opp_tapped, entered=1),
            _basic_land(202, "Plains", 2, tapped=opp_tapped, entered=2),
            _basic_land(203, "Island", 2, tapped=opp_tapped, entered=3),
            _basic_land(204, "Island", 2, tapped=opp_tapped, entered=4),
        ]
    )
    our_lands = [
        _basic_land(301, "Forest", 1, tapped=False, entered=1),
        _basic_land(302, "Forest", 1, tapped=False, entered=3),
        _basic_land(303, "Forest", 1, tapped=False, entered=5),
    ]
    hand_card = _creature(401, "Redundant Bear", 1, 2, 2, mana_cost="{1}{G}", grp_id=104)
    state: dict = {
        "match_id": match_id,
        "game_number": game_number,
        "event_id": "Constructed_Ladder",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": our_life, "mana_pool": {}},
            {
                "seat_id": 2,
                "is_local": False,
                "life_total": opp_life,
                "hand_size": opp_hand,
                "mana_pool": {},
            },
        ],
        "deck_cards": [101, 102, 103, 104, 90301],
        "hand": [hand_card],
        "battlefield": [*ours, *(opp_creatures or []), *lands, *our_lands, *(extra_opp_permanents or [])],
        "graveyard": list(graveyard or []),
        "exile": [],
        "stack": list(stack or []),
        "zones": {"library_count": 40, "opponent_hand_count": opp_hand},
    }
    if seq is not None:
        state["commentary_source_sequence"] = seq
    return state


def _actions_decision(card_id: int = 401, grp_id: int = 104, name: str = "Redundant Bear"):
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": grp_id, "instanceId": card_id, "hasAutoTap": True}
            ],
        },
        resolve_name=lambda _gid: name,
    )


def _wire_catalog(monkeypatch) -> None:
    catalog = {
        101: {
            "name": "Alpha Vanguard",
            "type_line": "Creature — Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "power": "3",
            "toughness": "3",
            "oracle_text": "",
        },
        102: {
            "name": "Beta Vanguard",
            "type_line": "Creature — Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "power": "3",
            "toughness": "3",
            "oracle_text": "",
        },
        103: {
            "name": "Gamma Vanguard",
            "type_line": "Creature — Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "power": "2",
            "toughness": "2",
            "oracle_text": "",
        },
        104: {
            "name": "Redundant Bear",
            "type_line": "Creature — Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "power": "2",
            "toughness": "2",
            "oracle_text": "",
        },
        90301: {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "mana_cost": "",
            "cmc": 0,
            "oracle_text": "({T}: Add {G}.)",
        },
    }
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, _epoch: catalog.get(gid, {}))
    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", lambda *args, **kwargs: {})


def test_sweeper_posture_and_ahead_constraint_with_pass_signal(monkeypatch):
    reset_seen_rules()
    _wire_catalog(monkeypatch)
    t4 = _constructed_state(turn=4, active=2, phase="Phase_Ending", opp_hand=3, seq=10)
    t5 = _constructed_state(turn=5, active=1, phase="Phase_Main1", opp_hand=3, seq=20)

    reader = OpponentRead()
    assert reader.observe_posture(t4) == ("passed turn 4 with 4 open mana (2W/2U)",)
    posture = reader.opponent_posture(t5)
    assert posture.known is True
    assert posture.tapped_out is False
    assert posture.open_mana == 4
    assert posture.color_summary == "2W/2U"
    assert posture.sweeper_risk == "high"
    assert posture.interaction_risk == "high"
    assert "4 open mana (2W/2U)" in posture.evidence_text
    assert "3 cards in hand" in posture.evidence_text
    assert "passed turn 4" in posture.evidence_text

    # GamePlanManager.strategy_block surfaces compact 2-line race-aware sweeper constraint.
    mgr = GamePlanManager(None)
    mgr.observe(t4)
    mgr.observe(t5)
    block = mgr.strategy_block(t5)
    posture_lines = [
        line for line in block.splitlines() if "OPP POSTURE:" in line or "OPP CONSTRAINT:" in line
    ]
    assert len(posture_lines) == 2
    assert "sweeper risk high" in posture_lines[0]
    assert "4 open mana (2W/2U)" in posture_lines[0]
    assert "3 cards in hand" in posture_lines[0]
    assert "passed turn 4" in posture_lines[0]
    assert "without overcommitting redundant threats into a sweeper" in posture_lines[1]

    # ActionPlanner typed decision AND plan_actions user prompts also surface the constraint cleanly.
    typed_be = _CaptureBackend()
    planner = ActionPlanner(typed_be, timeout=5.0, land_drop_first=False)
    planner.plan_decision_options(_actions_decision(), deepcopy(t4))
    planner.plan_decision_options(_actions_decision(), deepcopy(t5))
    typed_user = typed_be.calls[-1]["user"]
    assert "OPP POSTURE: sweeper risk high" in typed_user
    assert "passed turn 4 with 4 open mana (2W/2U)" in typed_user
    assert "without overcommitting redundant threats into a sweeper" in typed_user

    plan_be = _CaptureBackend('{"actions": [{"pick": 1}]}')
    plan_planner = ActionPlanner(plan_be, timeout=5.0, land_drop_first=False)
    plan_planner.plan_actions(deepcopy(t4), "opponent_turn", ["Pass"])
    plan_planner.plan_actions(deepcopy(t5), "new_turn", ["Cast Redundant Bear [OK]", "Pass"])
    plan_user = plan_be.calls[-1]["user"]
    assert "OPP POSTURE: sweeper risk high" in plan_user
    assert "without overcommitting redundant threats into a sweeper" in plan_user
    reset_seen_rules()


def test_opposite_control_behind_on_clock_never_advises_holding_back(monkeypatch):
    reset_seen_rules()
    _wire_catalog(monkeypatch)
    # Same 4 open W/U mana + 3 cards + pass signal, but opponent threatens lethal quickly (we are behind/survival).
    opp_monsters = [
        _creature(250, "Doom Titan", 2, 7, 7, entered=2, mana_cost="{4}{B}{B}"),
        _creature(251, "Sky Terror", 2, 6, 6, entered=3, mana_cost="{3}{U}{U}", keywords=["flying"]),
    ]
    our_small = [
        _creature(101, "Alpha Vanguard", 1, 1, 1, entered=1, grp_id=101),
        _creature(102, "Beta Vanguard", 1, 1, 1, entered=3, grp_id=102),
    ]
    t4 = _constructed_state(
        turn=4,
        active=2,
        phase="Phase_Ending",
        our_life=8,
        opp_life=20,
        opp_hand=3,
        our_creatures=our_small,
        opp_creatures=opp_monsters,
        seq=10,
    )
    t5 = _constructed_state(
        turn=5,
        active=1,
        phase="Phase_Main1",
        our_life=8,
        opp_life=20,
        opp_hand=3,
        our_creatures=our_small,
        opp_creatures=opp_monsters,
        seq=20,
    )
    assessment = assess(t5)
    assert assessment is not None
    assert assessment.survival_mode or assessment.race == "behind"

    mgr = GamePlanManager(None)
    mgr.observe(t4)
    mgr.observe(t5)
    block = mgr.strategy_block(t5)
    posture_lines = [
        line for line in block.splitlines() if "OPP POSTURE:" in line or "OPP CONSTRAINT:" in line
    ]
    assert len(posture_lines) <= 1
    assert not any("OPP CONSTRAINT:" in line for line in posture_lines)
    for line in posture_lines:
        lower = line.lower()
        assert "overcommit" not in lower
        assert "hold" not in lower
        assert "sweeper" not in lower
        assert "deploy" in lower or "stabilize" in lower

    backend = _CaptureBackend()
    planner = ActionPlanner(backend, timeout=5.0, land_drop_first=False)
    planner.plan_decision_options(_actions_decision(), deepcopy(t4))
    planner.plan_decision_options(_actions_decision(), deepcopy(t5))
    user = backend.calls[-1]["user"]
    assert "OPP CONSTRAINT:" not in user
    assert "without overcommitting" not in user
    reset_seen_rules()


def test_opposite_control_tapped_out_or_empty_handed_emits_zero_caution(monkeypatch):
    reset_seen_rules()
    _wire_catalog(monkeypatch)
    tapped = _constructed_state(turn=5, active=1, opp_tapped=True, opp_hand=3)
    empty_hand = _constructed_state(turn=5, active=1, opp_tapped=False, opp_hand=0)

    for label, state in (("tapped_out", tapped), ("empty_hand", empty_hand)):
        posture = OpponentRead().opponent_posture(state)
        assert posture.known is True, label
        assert posture.tapped_out is True, label
        assert posture.sweeper_risk == "none", label
        assert posture.interaction_risk == "none", label
        assert posture.constraint_lines(assess(state)) == (), label

        mgr = GamePlanManager(None)
        block = mgr.strategy_block(state)
        assert "OPP POSTURE:" not in block, label
        assert "OPP CONSTRAINT:" not in block, label

        backend = _CaptureBackend()
        planner = ActionPlanner(backend, timeout=5.0, land_drop_first=False)
        planner.plan_decision_options(_actions_decision(), deepcopy(state))
        user = backend.calls[-1]["user"]
        assert "OPP POSTURE:" not in user, label
        assert "OPP CONSTRAINT:" not in user, label
    reset_seen_rules()


def test_privacy_by_construction_ignores_hidden_hand_and_library_canaries(monkeypatch):
    reset_seen_rules()
    _wire_catalog(monkeypatch)
    state = _constructed_state(turn=5, active=1, opp_tapped=False, opp_hand=0)
    # Inject synthetic private/hidden keys containing canary card names and objects.
    hidden_zone_canaries = [
        "SECRET_HAND_WRATH_CANARY",
        "PRIVATE_ZONES_HAND_CANARY",
        "HIDDEN_LIBRARY_COUNTER_CANARY",
    ]
    all_canaries = [
        *hidden_zone_canaries,
        "STRAY_OPP_IN_HAND_CANARY",
        "REVEALED_UNTRUSTED_CANARY",
        "PRIVATE_EXILE_CANARY",
        "FACEDOWN_TRAP_CANARY",
    ]
    state["opponent_hand"] = [
        {
            "name": "SECRET_HAND_WRATH_CANARY",
            "type_line": "Sorcery",
            "oracle_text": "Destroy all creatures.",
            "owner_seat_id": 2,
        }
    ]
    state["zones"]["opponent_hand"] = [
        {
            "name": "PRIVATE_ZONES_HAND_CANARY",
            "type_line": "Instant",
            "oracle_text": "Counter target spell.",
            "owner_seat_id": 2,
        }
    ]
    state["opponent_library"] = [
        {
            "name": "HIDDEN_LIBRARY_COUNTER_CANARY",
            "type_line": "Instant",
            "oracle_text": "Counter target spell.",
            "owner_seat_id": 2,
        }
    ]
    state["zones"]["opponent_library"] = state["opponent_library"]
    state["hand"].append(
        {
            "instance_id": 999,
            "grp_id": 99999,
            "name": "STRAY_OPP_IN_HAND_CANARY",
            "type_line": "Sorcery",
            "oracle_text": "Destroy all creatures.",
            "owner_seat_id": 2,
        }
    )
    state["revealed_cards"] = [
        {
            "instance_id": 998,
            "grp_id": 99998,
            "name": "REVEALED_UNTRUSTED_CANARY",
            "zone": "hand",
            "owner_seat_id": 2,
        }
    ]
    state["exile"] = [
        {
            "instance_id": 997,
            "grp_id": 99997,
            "name": "PRIVATE_EXILE_CANARY",
            "type_line": "Sorcery",
            "oracle_text": "Destroy all creatures.",
            "owner_seat_id": 2,
            "visibility": "Visibility_Private",
        }
    ]
    state["battlefield"].append(
        {
            "instance_id": 996,
            "grp_id": 3,
            "name": "FACEDOWN_TRAP_CANARY",
            "type_line": "Creature",
            "oracle_text": "Destroy all creatures.",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "face_down": True,
        }
    )

    # Even with synthetic opponent_hand objects, public opponent_hand_count=0 rules -> tapped_out=True.
    posture_zero = OpponentRead().opponent_posture(state)
    assert posture_zero.opp_hand == 0
    assert posture_zero.tapped_out is True
    assert posture_zero.sweeper_risk == "none"

    # Now set public count=3 and confirm none of the hidden card names or public-sighting falsehoods leak.
    state["zones"]["opponent_hand_count"] = 3
    state["players"][1]["hand_size"] = 3
    reader = OpponentRead()
    posture = reader.opponent_posture(state)
    assert posture.opp_hand == 3
    assert "public sweeper sighting" not in posture.public_signals
    assert "public counterspell sighting" not in posture.public_signals

    mgr = GamePlanManager(None)
    block = mgr.strategy_block(state)
    backend = _CaptureBackend()
    planner = ActionPlanner(backend, timeout=5.0, land_drop_first=False)
    planner.plan_decision_options(_actions_decision(), deepcopy(state))
    user = backend.calls[-1]["user"]

    posture_note = planner._adversarial_posture_note(state)
    opp_prompt_lines = "\n".join(
        line for line in user.splitlines() if "OPP POSTURE:" in line or "OPP CONSTRAINT:" in line
    )
    for text in (
        json.dumps(posture.as_payload()),
        reader.plan_block(state),
        block,
        posture_note,
        opp_prompt_lines,
    ):
        for canary in all_canaries:
            assert canary not in text
    for canary in hidden_zone_canaries:
        assert canary not in user
    reset_seen_rules()


def test_nn1_kv_cache_prefix_invariance_across_posture_transitions(monkeypatch):
    reset_seen_rules()
    _wire_catalog(monkeypatch)
    backend = _CaptureBackend()
    planner = ActionPlanner(backend, timeout=15.0, land_drop_first=False)
    planner._deck_strategy_fn = lambda: "Curve out with bears and pressure life total."

    s_tapped = _constructed_state(turn=3, active=1, opp_tapped=True, opp_hand=4, seq=1)
    s_opp_pass = _constructed_state(
        turn=4, active=2, phase="Phase_Ending", opp_tapped=False, opp_hand=3, seq=2
    )
    s_open_sweeper = _constructed_state(turn=5, active=1, opp_tapped=False, opp_hand=3, seq=3)
    s_empty_hand = _constructed_state(turn=6, active=1, opp_tapped=False, opp_hand=0, seq=4)

    decision = _actions_decision()
    for s in (s_tapped, s_opp_pass, s_open_sweeper, s_empty_hand):
        planner._llm_decision_options(decision, deepcopy(s))

    systems = [c["system"] for c in backend.calls]
    assert len(set(systems)) == 1
    assert systems[0] == shared_match_system_prompt(s_tapped)
    assert ActionPlanner._decision_system_prompt() == ActionPlanner._decision_system_prompt()

    users = [c["user"] for c in backend.calls]
    assert "OPP POSTURE: sweeper risk high" not in users[0]
    assert "OPP POSTURE: sweeper risk high" in users[2]
    assert "passed turn 4 with 4 open mana (2W/2U)" in users[2]
    assert "OPP POSTURE:" not in users[3]

    # Leading KV-cache prefix through DECISION OUTPUT FORMAT stays byte-identical across all 4 calls.
    prefixes = [u[: u.index("PENDING DECISION")] for u in users]
    assert len(set(prefixes)) == 1
    prepared = prepare_match_context(deepcopy(s_tapped), compact_decision=True, source_ids_complete=True)
    head, rules = split_deck_reference(prepared["deck_reference_stable"])
    assert prefixes[0].startswith(head)
    assert rules in prefixes[0]
    reset_seen_rules()


def test_limited_trick_table_and_brawl_mana_rock_integration():
    # 1) Limited WITH TrickTable: integrates elevated p_any and zero-p_any.
    pump = ot.TrickCard(grp_id=701, name="Test Trick", mana_cost="{1}{G}", mv=2, pips=("G",), kinds=("pump",))
    table = ot.TrickTable(
        set_code="FRA",
        prior={"RG": 1.0},
        cards={701: pump},
        copies={"RG": {701: 4.0}},
    )
    limited_state = _constructed_state(
        turn=7,
        active=1,
        opp_hand=4,
        opp_lands=[
            _basic_land(201, "Forest", 2, tapped=False, entered=1),
            _basic_land(202, "Mountain", 2, tapped=False, entered=3),
        ],
    )
    limited_state["event_id"] = "PremierDraft_FRA_20260929"
    limited_state["deck_cards"] = [101] * 40
    posture_limited = OpponentRead().opponent_posture(limited_state, table=table)
    assert posture_limited.sweeper_risk == "none"
    assert posture_limited.interaction_risk == "high"
    assert any(sig.startswith("limited trick risk") for sig in posture_limited.public_signals)

    # With only 1 Mountain open and no 1-mana trick in table, p_any == 0.0 -> clears interaction_risk.
    one_red = deepcopy(limited_state)
    one_red["battlefield"] = [c for c in one_red["battlefield"] if c.get("instance_id") != 201]
    posture_clear = OpponentRead().opponent_posture(one_red, table=table)
    assert posture_clear.interaction_risk == "none"

    # 2) Brawl WITHOUT TrickTable: counts Sol Ring (2C) + Arcane Signet (any) + Plains + Swamp = 5 mana.
    brawl_state = _constructed_state(
        turn=5,
        active=1,
        our_life=30,
        opp_life=18,
        opp_hand=3,
        opp_lands=[
            _basic_land(201, "Plains", 2, tapped=False, entered=1),
            _basic_land(202, "Swamp", 2, tapped=False, entered=3),
        ],
        extra_opp_permanents=[
            {
                "instance_id": 210,
                "grp_id": 91001,
                "name": "Sol Ring",
                "type_line": "Artifact",
                "oracle_text": "{T}: Add {C}{C}.",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "object_kind": "CARD",
                "is_tapped": False,
            },
            {
                "instance_id": 211,
                "grp_id": 91002,
                "name": "Arcane Signet",
                "type_line": "Artifact",
                "oracle_text": "{T}: Add one mana of any color in your commander's color identity.",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "object_kind": "CARD",
                "is_tapped": False,
            },
        ],
    )
    brawl_state["event_id"] = "Brawl_Standard"
    brawl_state["deck_cards"] = [101] * 100
    brawl_posture = OpponentRead().opponent_posture(brawl_state)
    assert brawl_posture.open_mana == 5
    assert "W" in brawl_posture.open_colors and "B" in brawl_posture.open_colors
    assert brawl_posture.sweeper_risk == "high"
    lines = brawl_posture.constraint_lines(assess(brawl_state))
    assert len(lines) == 2 and "without overcommitting redundant threats into a sweeper" in lines[1]


def test_tempo_pass_signal_gates_sorcery_plays_sequence_isolation_and_concurrency():
    # 1) Sorcery-speed creature played on turn 4 suppresses pass-with-mana signal on turn 4.
    reader = OpponentRead()
    t4_creature = _creature(290, "Turn4 Ogre", 2, 4, 4, entered=4)
    s4_play = _constructed_state(
        turn=4, active=2, phase="Phase_Ending", opp_hand=2, opp_creatures=[t4_creature], seq=10
    )
    assert reader.observe_posture(s4_play) == ()

    # 2) Summoning-sick mana creature does not inflate open mana; Heartwood Crafter restricted mana ignored.
    sick_elf = _creature(
        291,
        "Llanowar Elves",
        2,
        1,
        1,
        entered=5,
        oracle_text="{T}: Add {G}.",
    )
    restricted_dork = _creature(
        292,
        "Restricted Maker",
        2,
        1,
        1,
        entered=1,
        oracle_text="{T}: Add {W}. Spend this mana only to cast a planeswalker spell.",
    )
    s5_sick = _constructed_state(
        turn=5,
        active=1,
        opp_hand=2,
        opp_lands=[_basic_land(201, "Plains", 2, tapped=False, entered=1)],
        opp_creatures=[sick_elf, restricted_dork],
        seq=15,
    )
    assert ot.opponent_open_mana(s5_sick)["open_mana"] == 1

    # 3) Sequence gating & game/match isolation.
    seq_reader = OpponentRead()
    s4_clean = _constructed_state(turn=4, active=2, phase="Phase_Ending", opp_hand=3, seq=20)
    s5_after = _constructed_state(turn=5, active=1, phase="Phase_Main1", opp_hand=3, seq=30)
    assert seq_reader.observe_posture(s4_clean) == ("passed turn 4 with 4 open mana (2W/2U)",)
    assert seq_reader.observe_posture(s5_after) == ("passed turn 4 with 4 open mana (2W/2U)",)
    # Late worker reading an earlier sequence does not see seq=20 pass signal.
    s4_early = _constructed_state(turn=4, active=2, phase="Phase_Main1", opp_hand=3, seq=12)
    assert seq_reader.observe_posture(s4_early) == ()
    # Different game_number or match_id stays isolated.
    assert seq_reader.observe_posture({**s5_after, "game_number": 2}) == ()
    assert seq_reader.observe_posture({**s5_after, "match_id": "other-match"}) == ()

    # 4) Concurrent thread safety under OpponentRead._lock.
    concurrent_reader = OpponentRead()

    def worker(idx: int) -> str:
        st = _constructed_state(
            match_id=f"conc-{idx % 3}",
            turn=4 if idx % 2 == 0 else 5,
            active=2 if idx % 2 == 0 else 1,
            phase="Phase_Ending" if idx % 2 == 0 else "Phase_Main1",
            opp_hand=3,
            seq=idx + 1,
        )
        return concurrent_reader.opponent_posture(st).sweeper_risk

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(30)))
    assert all(r == "high" for r in results)


def test_turn_transition_pass_inference_moderate_sweeper_and_reform_prompt(monkeypatch):
    reset_seen_rules()
    _wire_catalog(monkeypatch)
    # 1) Transition directly from opponent's Turn 4 Main1 -> our Turn 5 Main1 records pass signal.
    t4_main1 = _constructed_state(turn=4, active=2, phase="Phase_Main1", opp_hand=2, seq=1)
    t5_main1 = _constructed_state(turn=5, active=1, phase="Phase_Main1", opp_hand=2, seq=2)
    reader = OpponentRead()
    assert reader.observe_posture(t4_main1) == ()
    assert reader.observe_posture(t5_main1) == ("passed turn 4 with 4 open mana (2W/2U)",)

    # 2) Moderate sweeper risk when opp_hand == 2 with 4 W/B/R open mana and no prior pass/control evidence,
    #    reading public hand_size from players[] when zones.opponent_hand_count is omitted.
    mod_state = _constructed_state(turn=5, active=1, opp_hand=2)
    mod_state["zones"].pop("opponent_hand_count", None)
    posture_mod = OpponentRead().opponent_posture(mod_state)
    assert posture_mod.opp_hand == 2
    assert posture_mod.sweeper_risk == "moderate"

    # 3) GamePlanManager._reform includes posture constraints in volatile user_parts after plan instructions
    #    while keeping shared_match_system_prompt byte-identical.
    plan_json = json.dumps(
        {
            "role": "aggressor",
            "win_conditions": ["Alpha Vanguard combat damage"],
            "path": "Attack with current board while holding spare creatures",
            "threat": "board sweeper",
            "develop_next": "pressure with existing board",
            "turns": [
                {"turn": "T", "land": "", "cast": [], "attack": "Alpha Vanguard", "hold": "Redundant Bear"}
            ],
            "switch_if": ["opponent taps out"],
        }
    )
    gp_be = _CaptureBackend(plan_json)
    mgr = GamePlanManager(gp_be)
    mgr.observe(t4_main1)
    mgr.observe(t5_main1)
    plan = mgr.maybe_reform(deepcopy(t5_main1))
    assert plan is not None
    assert gp_be.calls[-1]["system"] == shared_match_system_prompt(t5_main1)
    user_msg = gp_be.calls[-1]["user"]
    assert "OPP POSTURE: sweeper risk high" in user_msg
    assert "OPP CONSTRAINT: ahead on board" in user_msg
    assert (
        user_msg.index("YOUR BOARD:") < user_msg.index("OPP POSTURE:") < user_msg.index("Form the GAME PLAN")
    )
    reset_seen_rules()


def test_nn8_findings7_10_behind_stalled_assessment_none_and_unknown_hand():
    from types import SimpleNamespace

    # 1. Stalled / behind without our_clock (our_clock=None, board_advantage < 0) suppresses sweeper OPP CONSTRAINT
    behind_no_clock = SimpleNamespace(
        race="behind",
        our_clock=None,
        their_clock=4,
        board_advantage=-3,
        our_creatures=2,
        their_creatures=3,
        lethal_now=False,
    )
    assert ot._is_behind_or_survival(behind_no_clock) is True

    stalled_behind = SimpleNamespace(
        race="stalled",
        our_clock=5,
        their_clock=5,
        board_advantage=-2,
        our_creatures=2,
        their_creatures=3,
        lethal_now=False,
    )
    assert ot._is_behind_or_survival(stalled_behind) is True

    posture = ot.AdversarialPosture(
        known=True,
        open_mana=4,
        color_summary="2W/2U",
        opp_hand=3,
        tapped_out=False,
        interaction_risk="moderate",
        sweeper_risk="high",
        our_creatures=4,
        evidence_text="4 open mana (2W/2U), 3 cards in hand",
    )
    for assm in (behind_no_clock, stalled_behind):
        lines = posture.constraint_lines(assm)
        assert not any("OPP CONSTRAINT:" in line for line in lines)
        assert len(lines) == 1 and "behind on race" in lines[0]
    assert posture.constraint_lines(None) == (f"  OPP POSTURE: sweeper risk high — {posture.evidence_text}.",)

    # 2. Unknown opp_hand (None) still surfaces visible high instant-speed risk
    unknown_hand_posture = ot.AdversarialPosture(
        known=True,
        open_mana=3,
        color_summary="2U/1W",
        opp_hand=None,
        tapped_out=False,
        interaction_risk="high",
        sweeper_risk="none",
        our_creatures=2,
        evidence_text="3 open mana (2U/1W), passed turn 4 with open mana",
    )
    u_lines = unknown_hand_posture.constraint_lines(None)
    assert len(u_lines) == 1
    assert "interaction risk high" in u_lines[0]


def test_nn8_finding8_fast_forwarded_sorcery_and_finalized_guard():
    opp_sorcery = {
        "instance_id": 701,
        "grp_id": 701,
        "name": "Divination",
        "type_line": "Sorcery",
        "card_types": ["CardType_Sorcery"],
        "owner_seat_id": 2,
        "controller_seat_id": 2,
    }

    # Case A: Opponent casts a sorcery in turn 4 and updates fast-forward directly to turn 5 Main1
    reader_a = OpponentRead()
    t4_main1 = _constructed_state(turn=4, active=2, phase="Phase_Main1", opp_hand=2, graveyard=[], seq=10)
    t5_main1_after_sorcery = _constructed_state(
        turn=5, active=1, phase="Phase_Main1", opp_hand=3, graveyard=[opp_sorcery], seq=11
    )
    assert reader_a.observe_posture(t4_main1) == ()
    assert reader_a.observe_posture(t5_main1_after_sorcery) == ()

    # Case B: Opponent genuinely passed turn 4 (finalized=True at seq=21); a discard in turn 5 does NOT erase pass
    reader_b = OpponentRead()
    t4_clean = _constructed_state(turn=4, active=2, phase="Phase_Main1", opp_hand=2, graveyard=[], seq=20)
    t5_clean = _constructed_state(turn=5, active=1, phase="Phase_Main1", opp_hand=2, graveyard=[], seq=21)
    t5_after_discard = _constructed_state(
        turn=5, active=1, phase="Phase_Main1", opp_hand=1, graveyard=[opp_sorcery], seq=22
    )
    assert reader_b.observe_posture(t4_clean) == ()
    assert reader_b.observe_posture(t5_clean) == ("passed turn 4 with 4 open mana (2W/2U)",)
    assert reader_b.observe_posture(t5_after_discard) == ("passed turn 4 with 4 open mana (2W/2U)",)


def test_nn8_finding11_limited_pass_signal_cap_and_verdict_gating(monkeypatch):
    reset_seen_rules()
    _wire_catalog(monkeypatch)
    t4_pass = _constructed_state(turn=4, active=2, phase="Phase_Ending", opp_hand=2, seq=1)
    t5_lim = _constructed_state(turn=5, active=1, phase="Phase_Main1", opp_hand=2, seq=2)
    t4_pass["event_id"] = "QuickDraft_FDN"
    t5_lim["event_id"] = "QuickDraft_FDN"

    reader = OpponentRead()
    reader.observe_posture(t4_pass)
    reader.observe_posture(t5_lim)

    low_trick = ot.TrickRisk(
        known=True,
        p_any=0.02,
        top=[("Possible Trick", "pump", 0.02)],
    )
    posture = reader.opponent_posture(t5_lim, trick=low_trick)
    # Pass signals + p_any < 0.10 without certain caps risk at "moderate" rather than "high"
    assert posture.interaction_risk == "moderate"

    # GamePlanManager.strategy_block suppresses OPP INTERACTION when a verdict is present & trick p_any < 0.10
    plan_json = json.dumps(
        {
            "role": "aggressor",
            "win_conditions": ["Alpha Vanguard"],
            "path": "Attack",
            "threat": "removal",
            "develop_next": "cast bear",
            "turns": [{"turn": "T", "land": "", "cast": [], "attack": "Alpha Vanguard", "hold": ""}],
            "switch_if": [],
        }
    )
    mgr = GamePlanManager(_CaptureBackend(plan_json))
    mgr.observe(t4_pass)
    mgr.observe(t5_lim)
    monkeypatch.setattr("arenamcp.game_plan.opponent_interaction", lambda _s, _svc: low_trick)
    monkeypatch.setattr(ot.TrickRisk, "verdict_line", lambda _self: "SAFE")
    block = mgr.strategy_block(deepcopy(t5_lim), with_lines=False)
    assert "OPP INTERACTION: SAFE" in block
    assert "OPP POSTURE: interaction risk" not in block
    reset_seen_rules()
