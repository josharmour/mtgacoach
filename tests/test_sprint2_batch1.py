"""Sprint 2 Batch 1 regression tests: S2-1..S2-4 (100% customer-deck-agnostic)."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from unittest.mock import MagicMock

from arenamcp.action_planner import ActionPlanner
from arenamcp.auto_queue import AutoQueueNavigator
from arenamcp.burn_timing import opponent_early_window_holds
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision
from arenamcp.deck_strategy import DeckPlaybook
from arenamcp.game_plan import GamePlanManager
from arenamcp.match_context import (
    prepare_match_context,
    reset_seen_rules,
    shared_match_system_prompt,
    split_deck_reference,
)
from arenamcp.play_safety import (
    dead_power_pump_activation,
    own_stack_reserved_option,
    pre_block_pump_hold,
)
from deck_fakes import playbook_for

LOCAL, OPP = 1, 2


class _CaptureBackend:
    def __init__(self, reply: str = '{"option_ids": ["pass"], "reasoning": "hold"}') -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, system_prompt, user_message, max_tokens=1024, **kwargs):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_message,
                "class": kwargs.get("call_class"),
                "max_tokens": max_tokens,
            }
        )
        return self.reply


def _player(seat: int, *, is_local: bool, life: int = 20, mana_pool: dict | None = None) -> dict:
    p = {"seat_id": seat, "is_local": is_local, "life_total": life}
    if mana_pool is not None:
        p["mana_pool"] = mana_pool
    return p


def _land(
    iid: int, seat: int = LOCAL, *, tapped: bool = False, name: str = "Mountain", text: str = ""
) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Basic Land",
        "card_types": ["CardType_Land"],
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
        "oracle_text": text,
    }


def _creature(
    iid: int,
    name: str,
    seat: int,
    power: int,
    toughness: int,
    oracle: str = "",
    *,
    tapped: bool = False,
    keywords: list[str] | None = None,
    attacking: bool = False,
    blocked: bool | None = None,
    blocking: bool = False,
    turn_entered: int = 1,
    damage: int = 0,
    blocked_by_ids: list[int] | None = None,
) -> dict:
    card: dict = {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Test",
        "card_types": ["CardType_Creature"],
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle,
        "is_tapped": tapped,
        "is_attacking": attacking,
        "is_blocking": blocking,
        "turn_entered_battlefield": turn_entered,
        "damage": damage,
    }
    if keywords is not None:
        card["keywords"] = list(keywords)
    if blocked is not None:
        card["is_blocked"] = blocked
    if blocked_by_ids is not None:
        card["blocked_by_ids"] = list(blocked_by_ids)
    return card


def _state(
    *,
    active: int = LOCAL,
    phase: str = "Phase_Main1",
    step: str = "",
    turn_number: int = 4,
    battlefield: list[dict] | None = None,
    hand: list[dict] | None = None,
    stack: list[dict] | None = None,
    life: int = 20,
    their_life: int = 20,
    mana_pool: dict | None = None,
    block_info: dict | None = None,
) -> dict:
    st: dict = {
        "match_id": "s2-test",
        "local_seat_id": LOCAL,
        "players": [
            _player(LOCAL, is_local=True, life=life, mana_pool=mana_pool),
            _player(OPP, is_local=False, life=their_life),
        ],
        "turn": {
            "turn_number": turn_number,
            "active_player": active,
            "priority_player": LOCAL,
            "phase": phase,
            "step": step,
        },
        "battlefield": list(battlefield or []),
        "hand": list(hand or []),
        "stack": list(stack or []),
        "graveyard": [],
        "exile": [],
    }
    if block_info is not None:
        st["block_info"] = block_info
    return st


def _cast_opt(idx: int, card: dict, *, payable: bool = True) -> DecisionOption:
    return DecisionOption(
        f"idx:{idx}",
        f"Cast {card['name']}",
        payable=payable,
        meta={"actionType": "ActionType_Cast", "instanceId": card["instance_id"]},
    )


def _act_opt(idx: int, card: dict, ability: str = "", *, payable: bool = True) -> DecisionOption:
    label = f"Activate {card['name']} [{ability}]" if ability else f"Activate {card['name']}"
    meta = {"actionType": "ActionType_Activate", "instanceId": card["instance_id"]}
    if ability:
        meta["ability_text"] = ability
    return DecisionOption(f"idx:{idx}", label, payable=payable, meta=meta)


def _pass_opt() -> DecisionOption:
    return DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"})


def _actions_menu(*options: DecisionOption) -> PendingDecision:
    return PendingDecision((4, 1), "ActionsAvailable", tuple(options), can_pass=True)


# ---------------------------------------------------------------------------
# S2-1: Auto-queue post-reload / slow-result-screen resilience
# ---------------------------------------------------------------------------


def _make_nav(recent_mode: bool = True) -> AutoQueueNavigator:
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_recent_match.return_value = {"ok": False, "error": "ref does not name an object result"}
    nav = AutoQueueNavigator(
        backend=_CaptureBackend(),
        get_game_state=lambda: {},
        bridge=bridge,
    )
    nav._recent_mode = recent_mode
    nav.enabled = True
    return nav


def test_s2_1_restore_after_reload_resets_stale_home_stage_and_progress_clock():
    nav = _make_nav()
    record = {
        "ended_match_id": "match-old",
        "stage": "home",
        "recent_mode": True,
        "queue_started": False,
        "idle_grind": False,
        "saved_at": time.time() - 3.0,
    }
    before = time.monotonic()
    assert nav.restore_after_reload(record) is True
    assert nav._stage == "resuming"
    assert nav._progress_at >= before

    # Opposite controls: queue_started preserves "queue", idle_grind preserves "home",
    # paused preserves "paused".
    nav_q = _make_nav()
    assert nav_q.restore_after_reload({**record, "stage": "queue", "queue_started": True}) is True
    assert nav_q._stage == "queue"

    nav_idle = _make_nav(recent_mode=False)
    assert nav_idle.restore_after_reload({**record, "recent_mode": False, "idle_recent": True}) is True
    assert nav_idle._stage == "home"

    nav_paused = _make_nav()
    assert nav_paused.restore_after_reload({**record, "paused_reason": "Rate limited", "retry_in_s": 25})
    assert nav_paused._stage == "paused"
    assert nav_paused.retry_scheduled


def test_s2_1_retry_after_pause_resets_bridge_result_state():
    nav = _make_nav()
    nav._bridge_result_done = True
    nav._bridge_leaves = 2
    nav.paused_reason = "Recent Match stopped: transient"
    nav._stage = "paused"
    nav._retry_after_pause()
    assert nav._bridge_result_done is False
    assert nav._bridge_leaves == 0
    assert nav._stage == "resuming"
    assert nav.paused_reason == ""


def test_s2_1_queue_recent_retries_transient_errors_and_stops_on_terminal_errors():
    nav = _make_nav()
    nav._progress_at = time.monotonic() - 95.0
    nav.active = True
    gen = nav._generation
    nav._queue_recent(gen, nav._abort, nav._bridge)
    assert "ref does not name an object result" in nav.paused_reason
    assert nav.retry_scheduled is True

    # Opposite control: terminal entry/event errors pause without automatic retry.
    nav2 = _make_nav()
    nav2._progress_at = time.monotonic()
    nav2.active = True
    nav2._bridge.get_recent_match.return_value = {"ok": False, "error": "no available entry found"}
    nav2._queue_recent(nav2._generation, nav2._abort, nav2._bridge)
    assert "no available entry" in nav2.paused_reason
    assert nav2.retry_scheduled is False


# ---------------------------------------------------------------------------
# S2-2: Close combat & combat-step patience (arbitrary colors/archetypes)
# ---------------------------------------------------------------------------


def test_s2_2_opponent_early_window_holds_and_controls():
    bolt = {
        "instance_id": 10,
        "name": "Azure Zap",
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "oracle_text": "Azure Zap deals 3 damage to any target.",
    }
    menu = _actions_menu(_cast_opt(0, bolt), _pass_opt())

    # Opp Main1 with 0 opposing creatures -> hold
    s_main1_empty = _state(active=OPP, phase="Phase_Main1", hand=[bolt], battlefield=[_land(1), _land(2)])
    assert len(opponent_early_window_holds(menu, s_main1_empty)) == 1

    # Opposite control: Opp Main1 with 1 opposing creature -> do NOT hold
    opp_bear = _creature(20, "Ivory Bear", OPP, 2, 2)
    s_main1_cre = _state(active=OPP, phase="Phase_Main1", hand=[bolt], battlefield=[_land(1), opp_bear])
    assert opponent_early_window_holds(menu, s_main1_cre) == []

    # Opp BeginCombat: vanilla, defender, tapped trigger, summoned trigger -> HOLD
    tapped_atk_trig = _creature(
        21, "Tactician", OPP, 2, 2, "Whenever Tactician attacks, draw a card.", tapped=True
    )
    sick_atk_trig = _creature(22, "Squire", OPP, 2, 2, "Whenever Squire attacks, scry 1.", turn_entered=4)
    defender_trig = _creature(
        23, "Sentinel", OPP, 0, 5, "Defender\nAt the beginning of combat, gain 1 life.", keywords=["defender"]
    )
    s_bc_safe = _state(
        active=OPP,
        phase="Phase_Combat",
        step="Step_BeginCombat",
        turn_number=4,
        hand=[bolt],
        battlefield=[_land(1), _land(2), opp_bear, tapped_atk_trig, sick_atk_trig, defender_trig],
    )
    assert len(opponent_early_window_holds(menu, s_bc_safe)) == 1

    # Opposite controls: able attacker with combat/attack trigger (including haste on entry turn)
    hasty_trig = _creature(
        24,
        "Skirmisher",
        OPP,
        3,
        2,
        "Haste\nWhenever you attack, target creature gets +1/+0 until end of turn.",
        keywords=["haste"],
        turn_entered=4,
    )
    s_bc_trig = _state(
        active=OPP,
        phase="Phase_Combat",
        step="Step_BeginCombat",
        turn_number=4,
        hand=[bolt],
        battlefield=[_land(1), _land(2), hasty_trig],
    )
    assert opponent_early_window_holds(menu, s_bc_trig) == []

    # Stack threats block early hold (lifegain, hexproof, protection, discard, prevent damage, cast lockout)
    for threat_text in (
        "Target player gains 4 life.",
        "Target creature gains hexproof until end of turn.",
        "Target creature gains protection from red until end of turn.",
        "Target opponent discards two cards.",
        "Prevent all combat damage that would be dealt this turn.",
        "Opponents can't cast spells this turn.",
    ):
        threat = {"instance_id": 99, "controller_seat_id": OPP, "oracle_text": threat_text}
        s_threat = _state(
            active=OPP,
            phase="Phase_Main1",
            hand=[bolt],
            battlefield=[_land(1), _land(2)],
            stack=[threat],
        )
        assert opponent_early_window_holds(menu, s_threat) == [], threat_text


def test_s2_2_opponent_early_window_planner_respects_lethal_and_chip_release():
    bolt = {
        "instance_id": 10,
        "name": "Cinder Bolt",
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "oracle_text": "Cinder Bolt deals 3 damage to any target.",
    }
    menu = _actions_menu(_cast_opt(0, bolt), _pass_opt())

    # Non-lethal -> auto-passes deterministically without asking LLM
    be = _CaptureBackend('{"option_ids": ["idx:0"]}')
    planner = ActionPlanner(be, timeout=5.0, land_drop_first=False)
    s = _state(active=OPP, phase="Phase_Main1", hand=[bolt], battlefield=[_land(1), _land(2)], their_life=10)
    assert planner.plan_decision_options(menu, s) == ["pass"]
    assert be.calls == []
    assert planner.get_last_decision_trace().get("policy") == "opponent_early_window_hold"

    # Opposite control: lethal burn at 3 life -> NOT held, asks LLM
    s_lethal = _state(
        active=OPP, phase="Phase_Main1", hand=[bolt], battlefield=[_land(1), _land(2)], their_life=3
    )
    assert planner.plan_decision_options(menu, s_lethal) == ["idx:0"]
    assert len(be.calls) == 1


def test_s2_2_pre_block_pump_hold_and_controls():
    ability_eot = "{1}{W}: Test Vanguard gets +2/+0 until end of turn."
    vanguard = _creature(11, "Test Vanguard", LOCAL, 2, 2, ability_eot, attacking=True)
    subtype_ctr = _creature(
        12,
        "Woodland Ranger",
        LOCAL,
        1,
        2,
        "{2}{G}: Put a +1/+1 counter on this Elf. Activate only once each turn.",
        attacking=True,
    )
    opt_eot = _act_opt(0, vanguard, ability_eot)
    opt_ctr = _act_opt(
        1, subtype_ctr, "{2}{G}: Put a +1/+1 counter on this Elf. Activate only once each turn."
    )

    s_atk = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareAttack",
        battlefield=[_land(1), _land(2), _land(3), vanguard, subtype_ctr],
    )
    assert pre_block_pump_hold(opt_eot, vanguard, s_atk)
    assert pre_block_pump_hold(opt_ctr, subtype_ctr, s_atk)

    # Planner auto-passes in DeclareAttack when only pump + pass exist
    be = _CaptureBackend('{"option_ids": ["idx:0"]}')
    planner = ActionPlanner(be, timeout=5.0, land_drop_first=False)
    assert planner.plan_decision_options(_actions_menu(opt_eot, _pass_opt()), s_atk) == ["pass"]
    assert planner.get_last_decision_trace().get("policy") == "combat_pump_timing"
    assert be.calls == []

    # Opposite controls:
    # 1) DeclareBlock is NOT held
    s_blk = _state(active=LOCAL, phase="Phase_Combat", step="Step_DeclareBlock", battlefield=[vanguard])
    assert pre_block_pump_hold(opt_eot, vanguard, s_blk) == ""

    # 2) Non-mana {T} cost or non-pure keyword gain NOT held
    fly_pump = _creature(
        13,
        "Sky Knight",
        LOCAL,
        2,
        2,
        "{1}{W}: Sky Knight gets +1/+1 and gains flying until end of turn.",
        attacking=True,
    )
    assert pre_block_pump_hold(_act_opt(0, fly_pump), fly_pump, s_atk) == ""
    tap_pump = _creature(
        14, "Tapper", LOCAL, 2, 2, "{T}: Tapper gets +2/+0 until end of turn.", attacking=True
    )
    assert pre_block_pump_hold(_act_opt(0, tap_pump), tap_pump, s_atk) == ""

    # 3) +1/+1 counter against Shock on stack targeting Woodland Ranger -> NOT held (must allow save!)
    shock = {
        "instance_id": 91,
        "name": "Shock",
        "controller_seat_id": OPP,
        "targeting": [12],
        "oracle_text": "Shock deals 2 damage to any target.",
    }
    s_threat = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareAttack",
        battlefield=[_land(1), _land(2), _land(3), subtype_ctr],
        stack=[shock],
    )
    assert pre_block_pump_hold(opt_ctr, subtype_ctr, s_threat) == ""


def test_s2_2_dead_power_pump_activation_and_general_exemptions():
    ability = "{1}{B}: Shadow Fiend gets +2/+0 until end of turn."
    fiend = _creature(30, "Shadow Fiend", LOCAL, 2, 2, ability, attacking=True, blocked=True)
    titan = _creature(40, "Colossus", OPP, 6, 6, "", blocking=True)
    opt = _act_opt(0, fiend, ability)

    # 3 mana -> 1 activation -> max 4 power < 6 toughness -> dead pump (HELD & auto-passes)
    s_dead = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), _land(3), fiend, titan],
    )
    assert dead_power_pump_activation(opt, fiend, s_dead)
    be = _CaptureBackend('{"option_ids": ["idx:0"]}')
    planner = ActionPlanner(be, timeout=5.0, land_drop_first=False)
    assert planner.plan_decision_options(_actions_menu(opt, _pass_opt()), s_dead) == ["pass"]
    assert planner.get_last_decision_trace().get("policy") == "combat_pump_timing"

    # Path A: lethal burn/destroy on stack targeting Shadow Fiend -> dead pump (HELD)
    unblocked_fiend = _creature(30, "Shadow Fiend", LOCAL, 2, 2, ability, attacking=True, blocked=False)
    murder = {
        "instance_id": 92,
        "name": "Murder",
        "controller_seat_id": OPP,
        "targeting": [30],
        "oracle_text": "Destroy target creature.",
    }
    s_stack_kill = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), unblocked_fiend],
        stack=[murder],
    )
    assert dead_power_pump_activation(opt, unblocked_fiend, s_stack_kill)

    # Opposite controls (must NOT hold):
    # 1) Enough mana for multiple activations (6 mana -> 2 + 6 = 8 >= 6)
    s_multi_kills = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), _land(3), _land(4), _land(5), _land(6), fiend, titan],
    )
    assert dead_power_pump_activation(opt, fiend, s_multi_kills) == ""

    # 2) Keywords: trample / lifelink / deathtouch / double strike reaching kill
    for kw in ("trample", "lifelink", "deathtouch"):
        kw_cre = _creature(
            30,
            "Kw Fiend",
            LOCAL,
            2,
            2,
            "{1}{B}: Kw Fiend gets +2/+0 until end of turn.",
            keywords=[kw],
            attacking=True,
            blocked=True,
        )
        assert dead_power_pump_activation(_act_opt(0, kw_cre), kw_cre, s_dead) == ""
    ds_cre = _creature(
        30,
        "Twin Fiend",
        LOCAL,
        2,
        2,
        "{1}{B}: Twin Fiend gets +2/+0 until end of turn.",
        keywords=["double strike"],
        attacking=True,
        blocked=True,
    )
    assert dead_power_pump_activation(_act_opt(0, ds_cre), ds_cre, s_dead) == ""

    # 3) Power-on-dies trigger on our creature / board, or equal-to-power spell in hand
    dies_cre = _creature(
        30,
        "Spite Fiend",
        LOCAL,
        2,
        2,
        "When Spite Fiend dies, it deals damage equal to its power to each opponent.\n"
        "{1}{B}: Spite Fiend gets +2/+0 until end of turn.",
        attacking=True,
        blocked=True,
    )
    assert (
        dead_power_pump_activation(
            _act_opt(0, dies_cre, "{1}{B}: Spite Fiend gets +2/+0 until end of turn."), dies_cre, s_dead
        )
        == ""
    )
    fling = {
        "instance_id": 88,
        "name": "Hurl",
        "oracle_text": "Sacrifice a creature: deals damage equal to its power.",
    }
    assert dead_power_pump_activation(opt, fiend, {**s_dead, "hand": [fling]}) == ""

    # 4) Unbounded non-land mana source (Sol Ring / Treasure) in combat fails closed
    treasure = {
        "instance_id": 50,
        "name": "Treasure",
        "type_line": "Token Artifact — Treasure",
        "card_types": ["CardType_Artifact"],
        "controller_seat_id": LOCAL,
        "is_tapped": False,
        "oracle_text": "{T}, Sacrifice this token: Add one mana of any color.",
    }
    s_unbounded = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), treasure, fiend, titan],
    )
    assert dead_power_pump_activation(opt, fiend, s_unbounded) == ""

    # 5) Stack removal contested by our Counterspell on stack -> NOT held
    negate = {
        "instance_id": 93,
        "controller_seat_id": LOCAL,
        "targeting": [92],
        "oracle_text": "Counter target noncreature spell.",
    }
    assert dead_power_pump_activation(opt, unblocked_fiend, {**s_stack_kill, "stack": [negate, murder]}) == ""


def test_s2_2_declare_block_unblocked_pump_hint_present_vs_blocked_absent():
    ability = "{1}{G}: Put a +1/+1 counter on this Elf."
    unblk = _creature(12, "Free Elf", LOCAL, 2, 2, ability, attacking=True, blocked=False)
    blk = _creature(12, "Blocked Elf", LOCAL, 2, 2, ability, attacking=True, blocked=True)
    opp_blk = _creature(41, "Guard", OPP, 5, 5, "", blocking=True)
    opt = _act_opt(0, unblk, ability)
    menu = _actions_menu(opt, _pass_opt())

    be1 = _CaptureBackend('{"option_ids": ["idx:0"], "reasoning": "pump"}')
    ActionPlanner(be1, timeout=5.0, land_drop_first=False).plan_decision_options(
        menu,
        _state(
            active=LOCAL,
            phase="Phase_Combat",
            step="Step_DeclareBlock",
            battlefield=[_land(1), _land(2), unblk],
        ),
    )
    assert "COMBAT PUMP AVAILABLE:" in be1.calls[-1]["user"]

    be2 = _CaptureBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    ActionPlanner(be2, timeout=5.0, land_drop_first=False).plan_decision_options(
        _actions_menu(_act_opt(0, blk, ability), _pass_opt()),
        _state(
            active=LOCAL,
            phase="Phase_Combat",
            step="Step_DeclareBlock",
            battlefield=[_land(1), _land(2), blk, opp_blk],
        ),
    )
    assert "COMBAT PUMP AVAILABLE:" not in be2.calls[-1]["user"]


# ---------------------------------------------------------------------------
# S2-3: Generalized own-stack resolution patience
# ---------------------------------------------------------------------------


def test_s2_3_own_stack_continuation_and_controls():
    reset_seen_rules()
    sorcery_stack = {
        "instance_id": 200,
        "name": "Divination",
        "type_line": "Sorcery",
        "object_kind": "SPELL",
        "controller_seat_id": LOCAL,
        "oracle_text": "Draw two cards.",
    }
    cre_stack = {
        "instance_id": 201,
        "name": "Loyal Gryff",
        "type_line": "Creature — Hippogriff",
        "object_kind": "SPELL",
        "controller_seat_id": LOCAL,
        "oracle_text": "Flash\nFlying",
    }
    zap = {
        "instance_id": 210,
        "name": "Red Zap",
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "oracle_text": "Red Zap deals 2 damage to any target.",
    }
    flash_aura = {
        "instance_id": 211,
        "name": "Celestial Mantle",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{1}{W}",
        "keywords": ["flash"],
        "oracle_text": "Flash\nEnchant creature\nEnchanted creature gets +1/+2.",
    }
    mana_sink = _creature(212, "Bramble Giant", LOCAL, 2, 2, "{2}{G}: Put a +1/+1 counter on this Giant.")
    counter_spell = {
        "instance_id": 213,
        "name": "Twincast",
        "type_line": "Instant",
        "mana_cost": "{U}{U}",
        "oracle_text": "Copy target instant or sorcery spell.",
    }
    mana_dork = _creature(214, "Mana Elf", LOCAL, 1, 1, "{T}: Add {G}.")

    # Main1, Main2, and empty-combat BeginCombat with own spell on stack hold non-lethal burn,
    # beneficial Flash Aura (when creature spell on stack), and mana-spending side activation.
    for phase, step in (
        ("Phase_Main1", ""),
        ("Phase_Main2", ""),
        ("Phase_Combat", "Step_BeginCombat"),
    ):
        st = _state(
            active=LOCAL,
            phase=phase,
            step=step,
            battlefield=[_land(1), _land(2), _land(3), mana_sink, mana_dork],
            hand=[zap, flash_aura, counter_spell],
            stack=[cre_stack],
        )
        assert "damage-only instant" in own_stack_reserved_option(_cast_opt(0, zap), zap, st)
        assert "beneficial Aura" in own_stack_reserved_option(_cast_opt(1, flash_aura), flash_aura, st)
        assert "side activation" in own_stack_reserved_option(
            _act_opt(2, mana_sink, "{2}{G}: Put a +1/+1 counter on this Giant."), mana_sink, st
        )
        # Opposite controls: stack-interaction spell & mana ability are NEVER held
        assert own_stack_reserved_option(_cast_opt(3, counter_spell), counter_spell, st) == ""
        assert own_stack_reserved_option(_act_opt(4, mana_dork, "{T}: Add {G}."), mana_dork, st) == ""

    # End-to-end _plan_decision_options passes in empty BeginCombat when only held option + pass
    be = _CaptureBackend('{"option_ids": ["idx:0"]}')
    planner = ActionPlanner(be, timeout=5.0, land_drop_first=False)
    st_bc = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_BeginCombat",
        battlefield=[_land(1), _land(2), _land(3), mana_sink],
        hand=[zap],
        stack=[sorcery_stack],
    )
    menu_bc = _actions_menu(
        _cast_opt(0, zap), _act_opt(1, mana_sink, "{2}{G}: Put a +1/+1 counter on this Giant."), _pass_opt()
    )
    assert planner.plan_decision_options(menu_bc, st_bc) == ["pass"]
    assert be.calls == []

    # Opposite controls: own ABILITY on stack, opposing spell on stack, active combat, lethal burn
    own_ability = {
        "instance_id": 205,
        "name": "Triggered Draw",
        "object_kind": "ABILITY",
        "controller_seat_id": LOCAL,
        "oracle_text": "Draw a card.",
    }
    st_abil = _state(
        active=LOCAL, phase="Phase_Main1", battlefield=[mana_sink], hand=[zap], stack=[own_ability]
    )
    assert own_stack_reserved_option(_cast_opt(0, zap), zap, st_abil) == ""
    assert (
        own_stack_reserved_option(
            _act_opt(1, mana_sink, "{2}{G}: Put a +1/+1 counter on this Giant."), mana_sink, st_abil
        )
        == ""
    )

    st_opp = _state(
        active=LOCAL,
        phase="Phase_Main1",
        hand=[zap],
        stack=[{**sorcery_stack, "controller_seat_id": OPP}],
    )
    assert own_stack_reserved_option(_cast_opt(0, zap), zap, st_opp) == ""

    atk_cre = _creature(220, "Attacker", LOCAL, 2, 2, "", attacking=True)
    st_combat = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_BeginCombat",
        battlefield=[atk_cre, mana_sink],
        hand=[zap],
        stack=[sorcery_stack],
    )
    assert own_stack_reserved_option(_cast_opt(0, zap), zap, st_combat) == ""

    st_lethal = _state(active=LOCAL, phase="Phase_Main1", hand=[zap], stack=[sorcery_stack], their_life=2)
    assert own_stack_reserved_option(_cast_opt(0, zap), zap, st_lethal) == ""


# ---------------------------------------------------------------------------
# S2-4: shared_match_system_prompt KV-cache boundary
# ---------------------------------------------------------------------------


def test_s2_4_system_prompt_holds_head_and_playbook_and_user_starts_with_rules(monkeypatch):
    reset_seen_rules()
    book = playbook_for("replay")
    catalog = deepcopy(book.catalog)
    catalog[1003] = {
        "name": "Fixture Scout",
        "type_line": "Creature — Bird",
        "mana_cost": "{1}{W}",
        "cmc": 2,
        "power": "2",
        "toughness": "1",
        "oracle_text": "Flying\nWhen Fixture Scout enters, gain 1 life.",
    }
    catalog[1004] = {
        "name": "Hidden Drawn",
        "type_line": "Creature — Angel",
        "mana_cost": "{3}{W}{W}",
        "cmc": 5,
        "power": "4",
        "toughness": "4",
        "oracle_text": "Flying\nVigilance",
    }
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, _epoch: catalog[gid])
    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", lambda *args, **kwargs: {})

    def _match_state(turn: int, extra_hand: bool = False) -> dict:
        hand = [{**catalog[1003], "grp_id": 1003, "instance_id": 12, "owner_seat_id": 1}]
        if extra_hand:
            hand.append({**catalog[1004], "grp_id": 1004, "instance_id": 13, "owner_seat_id": 1})
        return {
            "match_id": "s2-4-cache-match",
            "local_seat_id": 1,
            "commander_grp_ids": [1001],
            "deck_cards": [1002] * 10 + [1003, 1004],
            "players": [
                {"seat_id": 1, "is_local": True, "life_total": 20, "commander_ids": [50]},
                {"seat_id": 2, "is_local": False, "life_total": 20},
            ],
            "turn": {"turn_number": turn, "active_player": 1, "phase": "Phase_Main1"},
            "hand": hand,
            "battlefield": [],
            "command": [{**catalog[1001], "grp_id": 1001, "instance_id": 50, "owner_seat_id": 1}],
            "graveyard": [],
            "exile": [],
            "stack": [],
            "zones": {"library_count": 9, "opponent_hand_count": 5},
            "decision_context": {"type": "actions_available"},
        }

    # Backwards-compat default: omitting head/playbook returns exact base
    s1 = _match_state(1)
    assert shared_match_system_prompt(s1, reference_head="HEAD", playbook_core="CORE").endswith(
        "\n\nHEAD\n\nCORE"
    )
    assert shared_match_system_prompt(s1) != ""

    # Compare all live match callers: decision.mulligan, decision.typed, decision.plan, decision.pay, background.game_plan
    cap = _CaptureBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    planner = ActionPlanner(cap, timeout=5.0, land_drop_first=False, deck_playbook_fn=lambda: book)
    mull_dec = PendingDecision(
        (1, 0),
        "Mulligan",
        (DecisionOption("mull:keep", "Keep"), DecisionOption("mull:mull", "Mulligan")),
        min_select=1,
        max_select=1,
    )
    typed_dec = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": 1003, "instanceId": 12, "hasAutoTap": True}
            ],
        },
        resolve_name=lambda gid: catalog.get(gid, {}).get("name", ""),
    )
    planner._llm_decision_options(mull_dec, deepcopy(s1))
    mull_call = cap.calls[-1]
    assert mull_call["class"] == "decision.mulligan"

    planner._llm_decision_options(typed_dec, deepcopy(s1))
    typed_call_1 = cap.calls[-1]

    planner._llm_decision_options(typed_dec, deepcopy(_match_state(3, extra_hand=True)))
    typed_call_2 = cap.calls[-1]

    plan_cap = _CaptureBackend('{"actions": [{"pick": 1}]}')
    ActionPlanner(plan_cap, timeout=5.0, land_drop_first=False, deck_playbook_fn=lambda: book).plan_actions(
        deepcopy(s1), "new_turn", [f"Cast {catalog[1002]['name']} [OK]", "Pass"]
    )
    plan_call = plan_cap.calls[-1]

    pay_cap = _CaptureBackend('{"pay": false, "reasoning": "hold"}')
    ActionPlanner(
        pay_cap, timeout=5.0, land_drop_first=False, deck_playbook_fn=lambda: book
    ).plan_pay_or_decline("Tax", "Pay {1} or counter spell.", deepcopy(s1))
    pay_call = pay_cap.calls[-1]

    gp_reply = json.dumps(
        {
            "role": "aggressor",
            "win_conditions": ["Attack with Hidden Drawn"],
            "path": "Curve out",
            "threat": "sweeper",
            "develop_next": "cast Hidden Drawn",
            "turns": [{"turn": "T", "land": "", "cast": ["Hidden Drawn"], "attack": "none", "hold": ""}],
            "switch_if": ["opponent threatens lethal"],
        }
    )
    gp_cap = _CaptureBackend(gp_reply)
    mgr = GamePlanManager(gp_cap, separate_strategy_planning=True)
    mgr.set_playbook_source(lambda: book)
    assert mgr.maybe_reform(deepcopy(s1)) is not None
    gp_call = gp_cap.calls[-1]

    prep = prepare_match_context(
        deepcopy(s1), compact_decision=True, compact_starting_deck=True, source_ids_complete=True
    )
    head, _rules = split_deck_reference(prep["deck_reference_stable"])
    fields, core = DeckPlaybook._decision_context_lines(book, s1, request_type=None)
    core_text = "\n".join(f for i, f in enumerate(fields) if i in core)
    expected_sys = shared_match_system_prompt(s1, reference_head=head, playbook_core=core_text)

    for call in (mull_call, typed_call_1, typed_call_2, plan_call, pay_call, gp_call):
        assert call["system"] == expected_sys
        assert call["system"].find("DECK REFERENCE") < call["system"].find(core_text)
        assert call["user"].startswith("RULES OF THE CARDS SEEN THIS GAME")


# ---------------------------------------------------------------------------
# Reviewer-driven regression suite (B1..B2, M1..M4, m1..m6)
# ---------------------------------------------------------------------------


def test_review_b1_own_stack_sync_activations_and_upkeep_unheld():
    finale = {
        "instance_id": 200,
        "name": "Overrun Spell",
        "type_line": "Sorcery",
        "object_kind": "SPELL",
        "controller_seat_id": LOCAL,
        "oracle_text": "Creatures you control get +3/+3 until end of turn.",
    }
    manland = {
        "instance_id": 310,
        "name": "Mutavault",
        "type_line": "Land",
        "card_types": ["CardType_Land"],
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "oracle_text": "{T}: Add {C}.\n{1}: Mutavault becomes a 2/2 creature with all creature types until end of turn.",
    }
    token_maker = _creature(311, "Token Forge", LOCAL, 2, 2, "{2}{W}: Create a 1/1 Soldier creature token.")
    zap = {
        "instance_id": 312,
        "name": "Shock",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "oracle_text": "Shock deals 2 damage to any target.",
    }
    st_main = _state(
        active=LOCAL,
        phase="Phase_Main1",
        battlefield=[_land(1), _land(2), _land(3), manland, token_maker],
        hand=[zap],
        stack=[finale],
    )
    assert (
        own_stack_reserved_option(
            _act_opt(
                0, manland, "{1}: Mutavault becomes a 2/2 creature with all creature types until end of turn."
            ),
            manland,
            st_main,
        )
        == ""
    )
    assert (
        own_stack_reserved_option(
            _act_opt(1, token_maker, "{2}{W}: Create a 1/1 Soldier creature token."),
            token_maker,
            st_main,
        )
        == ""
    )
    # Upkeep window does NOT hold un-planned damage-only burn on own stack (leaves to upkeep_holds).
    st_upkeep = {
        **st_main,
        "turn": {
            "turn_number": 4,
            "active_player": LOCAL,
            "priority_player": LOCAL,
            "phase": "Phase_Beginning",
            "step": "Step_Upkeep",
        },
    }
    assert own_stack_reserved_option(_cast_opt(2, zap), zap, st_upkeep) == ""


def test_review_b2_multi_attacker_fail_closed_and_windows_blocker_block_info():
    ability = "{1}{R}: this gets +2/+0 until end of turn."
    a1 = _creature(30, "Atk One", LOCAL, 2, 2, ability, attacking=True, blocked=True)
    a2 = _creature(31, "Atk Two", LOCAL, 2, 2, ability, attacking=True, blocked=False)
    a3_unk = _creature(32, "Atk Three", LOCAL, 2, 2, ability, attacking=True, blocked=None)
    titan = _creature(40, "Colossus", OPP, 6, 6, "", blocking=True)
    bear = _creature(41, "Bear", OPP, 2, 2, "", blocking=True)
    opt1 = _act_opt(0, a1, ability)

    # Ambiguous: 2 blocked/unknown attackers without block_info -> MUST fail closed!
    s_ambig = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), a1, a3_unk, titan, bear],
    )
    assert dead_power_pump_activation(opt1, a1, s_ambig) == ""

    # Unambiguous when second attacker is known unblocked (is_blocked=False) -> still detects dead pump
    s_one_blocked = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), a1, a2, titan],
    )
    assert dead_power_pump_activation(opt1, a1, s_one_blocked)

    # Windows blocker-keyed block_info {blocker_id: [attacker_ids]}:
    # a1 blocked by titan (6/6 -> dead pump), a3_unk blocked by bear (2/2 -> Wait: a3=2/2 already kills bear? use 3/3)
    hill = _creature(41, "Hill Giant", OPP, 3, 3, "", blocking=True)
    s_win = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), a1, a3_unk, titan, hill],
        block_info={"40": [30], "41": [32]},
    )
    assert dead_power_pump_activation(opt1, a1, s_win)
    assert dead_power_pump_activation(_act_opt(1, a3_unk, ability), a3_unk, s_win) == ""

    # macOS attacker-keyed block_info {attacker_id: [blocker_ids]}:
    s_mac = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), a1, a3_unk, titan, hill],
        block_info={"30": [40], "32": [41]},
    )
    assert dead_power_pump_activation(opt1, a1, s_mac)
    assert dead_power_pump_activation(_act_opt(1, a3_unk, ability), a3_unk, s_mac) == ""


def test_review_m1_double_strike_first_strike_survival_not_held():
    ability = "{1}{W}: Duelist gets +2/+0 until end of turn."
    duelist = _creature(
        30, "Duelist", LOCAL, 2, 2, ability, keywords=["double strike"], attacking=True, blocked=True
    )
    wall = _creature(40, "Wall", OPP, 3, 4, "", blocking=True)
    # Without pump: 2*2 = 4 kills Wall only in regular damage, so Wall kills Duelist back!
    # With +2/+0 pump: first-strike power reaches 4, killing Wall BEFORE regular damage and saving Duelist!
    st = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), duelist, wall],
    )
    assert dead_power_pump_activation(_act_opt(0, duelist, ability), duelist, st) == ""


def test_review_m2_m3_m4_and_minor_guards():
    from arenamcp.play_safety import _mana_only_activation_cost, _pure_self_stat_pump_delta

    # M2: two opposing removal spells on stack do NOT "contest" each other -> pump is still dead!
    ability = "{1}{B}: Fiend gets +2/+0 until end of turn."
    fiend = _creature(30, "Fiend", LOCAL, 2, 2, ability, attacking=True, blocked=False)
    murder1 = {
        "instance_id": 91,
        "controller_seat_id": OPP,
        "targeting": [30],
        "oracle_text": "Destroy target creature.",
    }
    murder2 = {
        "instance_id": 92,
        "controller_seat_id": OPP,
        "targeting": [30],
        "oracle_text": "Destroy target creature.",
    }
    st_double_opp = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), fiend],
        stack=[murder1, murder2],
    )
    assert dead_power_pump_activation(_act_opt(0, fiend, ability), fiend, st_double_opp)

    # M3 (burn_timing): missing local_seat_id or active_player + non-dict battlefield item fail closed cleanly
    bolt = {
        "instance_id": 10,
        "name": "Bolt",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "oracle_text": "Bolt deals 3 damage to any target.",
    }
    menu = _actions_menu(_cast_opt(0, bolt), _pass_opt())
    st_missing_active = {
        "local_seat_id": LOCAL,
        "turn": {"phase": "Phase_Main1"},
        "battlefield": ["malformed_entry"],
        "hand": [bolt],
        "stack": [],
    }
    assert opponent_early_window_holds(menu, st_missing_active) == []

    # M4: floating mana in DeclareAttack fails closed so floating attack-trigger mana isn't wasted
    vanguard_abil = "{1}{W}: Vanguard gets +2/+0 until end of turn."
    vanguard = _creature(11, "Vanguard", LOCAL, 2, 2, vanguard_abil, attacking=True)
    st_float = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareAttack",
        battlefield=[vanguard],
        mana_pool={"red": 1, "colorless": 1},
    )
    assert pre_block_pump_hold(_act_opt(0, vanguard, vanguard_abil), vanguard, st_float) == ""

    # m1: tapped static cost reducer & multi-mana "Add two mana" land fail closed; bool pool value ignored
    reducer = {
        "instance_id": 51,
        "name": "Training Grounds",
        "type_line": "Enchantment",
        "controller_seat_id": LOCAL,
        "is_tapped": True,
        "oracle_text": "Activated abilities of creatures you control cost {2} less to activate.",
    }
    fiend_blk = _creature(30, "Fiend", LOCAL, 2, 2, ability, attacking=True, blocked=True)
    titan = _creature(40, "Colossus", OPP, 4, 4, "", blocking=True)
    st_tapped_red = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), reducer, fiend_blk, titan],
    )
    assert dead_power_pump_activation(_act_opt(0, fiend_blk, ability), fiend_blk, st_tapped_red) == ""

    double_land = _land(60, name="Sol Land", text="{T}: Add two mana of any color.")
    st_double_land = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), double_land, fiend_blk, titan],
    )
    assert dead_power_pump_activation(_act_opt(0, fiend_blk, ability), fiend_blk, st_double_land) == ""

    # m2: Phyrexian /P excluded, 2-brid {2/R} accepted; modern Arena bare "this" pump parsed
    assert _mana_only_activation_cost("{R/P}") is None
    assert _mana_only_activation_cost("{2/R}") == "{2/R}"
    assert _pure_self_stat_pump_delta("Put a +1/+1 counter on this.", "Kavu") == (1, 1)
    assert _pure_self_stat_pump_delta("this gets +2/+0 until end of turn.", "Shade") == (2, 0)

    # m3: curly apostrophe bounce kills, opposing power-rewarding removal keeps +2/+0 unheld, modified_toughness shrink
    curly_bounce = {
        "instance_id": 95,
        "controller_seat_id": OPP,
        "targeting": [30],
        "oracle_text": "Return target creature to its owner\u2019s hand.",
    }
    assert dead_power_pump_activation(
        _act_opt(0, fiend, ability), fiend, {**st_double_opp, "stack": [curly_bounce]}
    )
    power_exile = {
        "instance_id": 96,
        "controller_seat_id": OPP,
        "targeting": [30],
        "oracle_text": "Exile target creature. Its controller gains life equal to its power.",
    }
    assert (
        dead_power_pump_activation(
            _act_opt(0, fiend, ability), fiend, {**st_double_opp, "stack": [power_exile]}
        )
        == ""
    )
    fiend_mod = {**fiend, "toughness": 4, "modified_toughness": 2}
    shrink = {
        "instance_id": 97,
        "type_line": "Instant",
        "controller_seat_id": OPP,
        "targeting": [30],
        "oracle_text": "Target creature gets -2/-2 until end of turn.",
    }
    assert dead_power_pump_activation(
        _act_opt(0, fiend_mod, ability), fiend_mod, {**st_double_opp, "stack": [shrink]}
    )

    # m4/m5/m6: non-recent reload keeps stage="home", uppercase terminal error stops, ds + plural note
    nav_legacy = _make_nav(recent_mode=False)
    assert nav_legacy.restore_after_reload({"ended_match_id": "m", "stage": "home"}) is True
    assert nav_legacy._stage == "home"

    nav_upper = _make_nav(recent_mode=True)
    nav_upper._progress_at = time.monotonic()
    nav_upper.active = True
    nav_upper._bridge.get_recent_match.return_value = {"ok": False, "error": "NO AVAILABLE ENTRY"}
    nav_upper._queue_recent(nav_upper._generation, nav_upper._abort, nav_upper._bridge)
    assert nav_upper.retry_scheduled is False

    ds_ctr_abil = "{1}{G}: Put two +1/+1 counters on this."
    ds_unblk = _creature(
        12, "Twin Elf", LOCAL, 2, 2, ds_ctr_abil, keywords=["double strike"], attacking=True, blocked=False
    )
    st_ds = _state(
        active=LOCAL,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        battlefield=[_land(1), _land(2), ds_unblk],
    )
    notes = ActionPlanner._unblocked_combat_pump_notes(
        _actions_menu(_act_opt(0, ds_unblk, ds_ctr_abil), _pass_opt()), st_ds
    )
    assert len(notes) == 1
    assert "2 permanent +1/+1 counters" in notes[0]
    assert "+4 unblocked combat damage (double strike)" in notes[0]

    # m4 explicit test: _hold_opponent_early_burn honors plan_chip_release, burn_combo_for, and _lethal_this_turn
    from unittest.mock import patch

    shock_c = {
        "instance_id": 10,
        "name": "Shock",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "oracle_text": "Shock deals 2 damage to any target.",
    }
    strike_c = {
        "instance_id": 11,
        "name": "Lightning Strike",
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "oracle_text": "Lightning Strike deals 3 damage to any target.",
    }
    st_opp_m1 = _state(
        active=OPP,
        phase="Phase_Main1",
        hand=[shock_c],
        battlefield=[_land(1), _land(2)],
        their_life=10,
    )
    be_chip = _CaptureBackend('{"option_ids": ["idx:0"], "reasoning": "chip now"}')
    planner_chip = ActionPlanner(be_chip, timeout=5.0, land_drop_first=False)
    planner_chip.set_game_plan("Chip the opponent with Shock this turn.")
    assert planner_chip.plan_decision_options(
        _actions_menu(_cast_opt(0, shock_c), _pass_opt()), st_opp_m1
    ) == ["idx:0"]
    assert len(be_chip.calls) == 1

    # Opposite control without chip plan holds through opponent Main1
    be_no_chip = _CaptureBackend('{"option_ids": ["idx:0"]}')
    planner_no_chip = ActionPlanner(be_no_chip, timeout=5.0, land_drop_first=False)
    assert planner_no_chip.plan_decision_options(
        _actions_menu(_cast_opt(0, shock_c), _pass_opt()), st_opp_m1
    ) == ["pass"]
    assert be_no_chip.calls == []

    # burn_combo_for releases two-burn kill on a 4/4 vanilla blocker in opponent BeginCombat
    opp_beast = _creature(45, "Beast", OPP, 4, 4)
    st_opp_bc_combo = _state(
        active=OPP,
        phase="Phase_Combat",
        step="Step_BeginCombat",
        hand=[shock_c, strike_c],
        battlefield=[_land(1), _land(2), _land(3), opp_beast],
        their_life=12,
    )
    be_combo = _CaptureBackend(
        '{"option_ids": ["idx:1"], "reasoning": "Lightning Strike plus Shock kill Beast."}'
    )
    planner_combo = ActionPlanner(be_combo, timeout=5.0, land_drop_first=False)
    assert planner_combo.plan_decision_options(
        _actions_menu(_cast_opt(0, shock_c), _cast_opt(1, strike_c), _pass_opt()),
        st_opp_bc_combo,
    ) == ["idx:1"]
    assert len(be_combo.calls) == 1

    # _lethal_this_turn=True aborts early hold; _lethal_this_turn=None preserves hold
    be_lethal = _CaptureBackend('{"option_ids": ["idx:0"], "reasoning": "lethal"}')
    planner_lethal = ActionPlanner(be_lethal, timeout=5.0, land_drop_first=False)
    with patch("arenamcp.action_planner._lethal_this_turn", return_value=True):
        assert planner_lethal.plan_decision_options(
            _actions_menu(_cast_opt(0, shock_c), _pass_opt()), st_opp_m1
        ) == ["idx:0"]
    assert len(be_lethal.calls) == 1
    with patch("arenamcp.action_planner._lethal_this_turn", return_value=None):
        assert planner_lethal.plan_decision_options(
            _actions_menu(_cast_opt(0, shock_c), _pass_opt()), st_opp_m1
        ) == ["pass"]
    assert len(be_lethal.calls) == 1
