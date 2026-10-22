"""Block and attack declarations that Arena rejected or the autopilot refused (2026-10-19, macOS bridge).

Match cd41ad5a (Brawl), turn 9, Player.log 07:15-08:02:
- 07:34:34: the plan blocked Biting-Palm Ninja (menace from a counter, ability
  grpId 142) with one Rabbit. The chump guard dropped the block on Dark Leo &
  Shredder (its "deal combat damage to a player" payoff unread), three blocks
  went out, and SubmitBlockersReq got FailureReason_CombatRestrictionViolated.
- Arena re-issued DeclareBlockersReq (gameStateId 224) with Rabbit 484 still
  on Shark Shredder 468 (selectedAttackerInstanceIds [468], no
  attackerInstanceIds). The re-plan named that block and was rejected ("Combat
  instance 468 is not eligible"); the safe default's six blocks were accepted
  next to 484, the finalize check refused the "extra" block: MANUAL REQUIRED.
  At 07:35:15 every blocker showed a selection and no attackerInstanceIds; the
  watchdog read "no creature can block any attacker", logged "[No Blocks]",
  and its confirm committed all seven blocks.
- 07:33:23 / 08:26:34: identical copies of our own creatures ("Hare Apparent
  #1", a bare "Fleeting Effigy" twice) were rejected as ambiguous.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from unittest.mock import MagicMock, Mock

import pytest

import arenamcp.autopilot_bridge as autopilot_bridge
from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_progress import DecisionProgressGuard
from arenamcp.combat_identity import combat_identity_prompt, resolve_combatant, resolve_combatants
from arenamcp.combat_solver import blocker_options, selected_blocks
from arenamcp.combat_strategy import repair_menace_blocks, safe_default_blocks, wasteful_chump_blocks
from arenamcp.gamestate import GameObject, GameState, _handle_decision_message

OPPONENT, LOCAL = 1, 2
DARK_LEO, PALM_NINJA, SHARK, NINJA_TOKEN = 442, 453, 468, 477
HARE_A, HARE_B = 460, 486
RABBITS = (484, 485, 490, 491, 492)
BLOCKERS = (HARE_A, 484, 485, HARE_B, 490, 491, 492)  # Arena's order in the request
ATTACKERS = [DARK_LEO, SHARK, NINJA_TOKEN, PALM_NINJA]  # Arena's order in attackerInstanceIds
DARK_LEO_TEXT = (
    "Sneak {W}{B}\nAttacking Ninjas you control have deathtouch.\nWhenever Dark Leo & Shredder deal "
    "combat damage to a player, create a 1/1 black Ninja creature token. Then if you control five or "
    "more Ninjas, that player loses half their life, rounded up."
)


def creature(iid, name, power, toughness, seat, *, keywords=(), attacking=False, token=False, text=""):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Token Creature" if token else "Creature",
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "keywords": list(keywords),
        "is_attacking": attacking,
        "is_token": token,
        "object_kind": "TOKEN" if token else "CARD",
        "oracle_text": text,
    }


def incident_board(life=20, extra_threat_power=8, dark_leo_text=DARK_LEO_TEXT):
    """Turn 9 of match cd41ad5a. Their non-attacking body sets next turn's clock (17)."""
    battlefield = [
        creature(
            DARK_LEO,
            "Dark Leo & Shredder",
            1,
            3,
            OPPONENT,
            keywords=["deathtouch"],
            attacking=True,
            text=dark_leo_text,
        ),
        creature(
            PALM_NINJA, "Biting-Palm Ninja", 3, 3, OPPONENT, keywords=["deathtouch", "menace"], attacking=True
        ),
        creature(
            SHARK,
            "Shark Shredder, Killer Clone",
            4,
            4,
            OPPONENT,
            keywords=["first strike", "deathtouch"],
            attacking=True,
        ),
        creature(NINJA_TOKEN, "Ninja", 1, 1, OPPONENT, keywords=["deathtouch"], attacking=True, token=True),
        creature(900, "Leatherhead", extra_threat_power, extra_threat_power, OPPONENT),
        creature(HARE_A, "Hare Apparent", 2, 2, LOCAL),
        creature(HARE_B, "Hare Apparent", 2, 2, LOCAL),
        *(creature(iid, "Rabbit", 1, 1, LOCAL, token=True) for iid in RABBITS),
    ]
    return {
        "local_seat_id": LOCAL,
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": life},
            {"seat_id": OPPONENT, "is_local": False, "life_total": 25},
        ],
        "battlefield": battlefield,
        "hand": [],
        "turn": {"turn_number": 9, "active_player": OPPONENT, "step": "Step_DeclareBlock"},
        "_bridge_request_type": "DeclareBlockers",
        "_bridge_request_class": "DeclareBlockersRequest",
    }


def blockers_request(selected=None, gsid=222, msg=294):
    """The DeclareBlockersReq as the Mac bridge shapes it; a selected blocker lists no attackers."""
    selected = selected or {}
    blockers = []
    for blocker in BLOCKERS:
        entry = {"blockerInstanceId": blocker, "maxAttackers": 1, "selectedAttackerInstanceIds": []}
        if blocker in selected:
            entry["attackerInstanceIds"] = []
            entry["selectedAttackerInstanceIds"] = [selected[blocker]]
        else:
            entry["attackerInstanceIds"] = list(ATTACKERS)
        blockers.append(entry)
    return {
        "has_pending": True,
        "request_type": "DeclareBlockers",
        "request_class": "DeclareBlockersRequest",
        "bridge_runtime": "il2cpp-macos",
        "game_state_id": gsid,
        "msg_id": msg,
        "blockers": blockers,
    }


def with_context(state, request, remember=True):
    """The log-derived decision context; ``remember``: the log saw this combat's first request."""
    selected = selected_blocks(request["blockers"])
    state["decision_context"] = {
        "type": "declare_blockers",
        "raw_blockers": request["blockers"],
        "legal_blocker_ids": [b["blockerInstanceId"] for b in request["blockers"]],
        "attacker_ids": sorted(ATTACKERS),
        "blocker_options": {blocker: list(ATTACKERS) for blocker in selected} if remember else {},
    }
    return state


class ArenaBlockers:
    """A Mac-bridge DeclareBlockers request, as Arena answers updates and confirms.

    An update sets each listed blocker's selection; a selected blocker lists no
    attackers, a withdrawn one (empty selection) lists them again. Like the
    adapter, a selection outside the blocker's attackerInstanceIds is refused.
    A confirm of a declaration with a lone blocker on a menace attacker is
    rejected (the request stays); otherwise the request closes, recording what
    was committed.
    """

    def __init__(self, request, board):
        self.connected = True
        self.request = deepcopy(request)
        self.board = {card["instance_id"]: card for card in board["battlefield"]}
        self.blocker_calls = []
        self.committed = None

    def connect(self):
        return True

    def get_pending_actions(self):
        return deepcopy(self.request)

    def submit_blockers(self, assignments):
        self.blocker_calls.append(deepcopy(assignments))
        request = deepcopy(self.request)
        if not assignments:
            selection = selected_blocks(request["blockers"])
            per_attacker = {}
            for attackers in selection.values():
                for attacker in attackers:
                    per_attacker[attacker] = per_attacker.get(attacker, 0) + 1
            if any(n == 1 and "menace" in self.board[a]["keywords"] for a, n in per_attacker.items()):
                return True  # IllegalRequest: the request stays
            self.committed = selection
            self.request = {"has_pending": False}
            return True
        by_id = {b["blockerInstanceId"]: b for b in request["blockers"]}
        for entry in assignments:
            blocker = by_id[entry["blockerInstanceId"]]
            if not set(entry["attackerInstanceIds"]) <= set(blocker.get("attackerInstanceIds") or []):
                return False  # AdapterError: Illegal attackers for blocker
        for entry in assignments:
            blocker = by_id[entry["blockerInstanceId"]]
            blocker["selectedAttackerInstanceIds"] = list(entry["attackerInstanceIds"])
            blocker["attackerInstanceIds"] = [] if entry["attackerInstanceIds"] else list(ATTACKERS)
        request["game_state_id"] += 1
        request["msg_id"] += 2
        self.request = request
        return True

    def auto_respond(self):
        pytest.fail("combat requests must never be auto-responded")


def engine_for(bridge, state):
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = bridge
    engine._abort_event = threading.Event()
    engine._progress_guard = DecisionProgressGuard()
    engine._progress_game_state = {}
    engine._progress_last_poll = None
    engine._config = MagicMock(dry_run=False)
    engine._path_stats = {}
    engine._gre_bridge_failed_methods = set()
    engine._advice_recorder = None
    engine._get_game_state = lambda: state
    return engine


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(autopilot_bridge.time, "sleep", lambda seconds: None)


def parse(response, menu, state, context):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_menu = menu
    return planner._parse_response(json.dumps(response), menu, context, game_state=state)


BLOCK_MENU = [
    "Block with: Hare Apparent #1",
    "Block with: Rabbit #1",
    "Block with: Rabbit #2",
    "Block with: Hare Apparent #2",
    "Block with: Rabbit #3",
    "Block with: Rabbit #4",
    "Block with: Rabbit #5",
    "Done (confirm blockers)",
]


def select(blocker, attacker=None):
    return {"blockerInstanceId": blocker, "attackerInstanceIds": [attacker] if attacker else []}


# --- Bug 1: menace ------------------------------------------------------------------


@pytest.mark.usefixtures("instant_combat_acks")
def test_lone_block_on_a_menace_attacker_never_leaves_the_bridge():
    """07:34:34: 484->Shark, 485->Biting-Palm Ninja (menace), 490->Dark Leo, 491->Ninja.

    At 8 life each chump is what keeps us above CHUMP_DANGER_LIFE (8 - 3, 8 - 4), so the chump guard
    keeps both and the menace repair alone drops the lone Ninja block. At the live 20, their next attack
    through the creatures we keep is far from lethal (#171: no longer their raw board power), so the
    chump guard drops both chumps; this test is about the menace block.
    """
    state = with_context(incident_board(life=8), blockers_request())
    bridge = ArenaBlockers(blockers_request(), state)
    action = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={
            "*Rabbit [id:484]": "Shark Shredder, Killer Clone [id:468]",
            "*Rabbit [id:485]": "Biting-Palm Ninja [id:453]",
            "*Rabbit [id:490]": "Dark Leo & Shredder [id:442]",
            "*Rabbit [id:491]": "*Ninja [id:477]",
        },
        blocker_instance_assignments={484: SHARK, 485: PALM_NINJA, 490: DARK_LEO, 491: NINJA_TOKEN},
    )

    result = engine_for(bridge, state)._try_gre_bridge_blockers(action)

    assert result is not None and result.success
    # The block on Dark Leo stops its hit payoff and stays; the lone menace block goes.
    assert bridge.blocker_calls == [[select(484, SHARK), select(490, DARK_LEO), select(491, NINJA_TOKEN)], []]
    assert bridge.committed == {484: {SHARK}, 490: {DARK_LEO}, 491: {NINJA_TOKEN}}
    assert result.submitted_action.blocker_instance_assignments == {
        484: SHARK,
        490: DARK_LEO,
        491: NINJA_TOKEN,
    }
    assert "menace" in result.submitted_action.reasoning


def test_chump_guard_reads_a_plural_deal_combat_damage_payoff():
    """Dark Leo & Shredder: 'Whenever Dark Leo & Shredder deal combat damage to a player, ...'."""
    plan = {490: DARK_LEO}
    assert wasteful_chump_blocks(with_context(incident_board(), blockers_request()), plan) == {}
    no_text = with_context(incident_board(dark_leo_text=""), blockers_request())
    assert set(wasteful_chump_blocks(no_text, plan)) == {490}  # 07:34:34 without the payoff


def test_menace_repair_drops_a_lone_block_but_keeps_a_pair():
    state = with_context(incident_board(), blockers_request())
    repaired, notes = repair_menace_blocks(state, {485: PALM_NINJA, 491: NINJA_TOKEN})
    assert repaired == {491: NINJA_TOKEN}
    assert len(notes) == 1 and "two or more" in notes[0]
    pair = {485: PALM_NINJA, 490: PALM_NINJA}
    assert repair_menace_blocks(state, pair) == (pair, [])


def test_menace_repair_adds_a_partner_when_dropping_the_block_is_lethal():
    # At 3 life the Ninja's 3 damage kills: a second creature joins the block.
    state = with_context(incident_board(life=3), blockers_request())
    plan = {484: SHARK, 485: PALM_NINJA, 490: DARK_LEO, 491: NINJA_TOKEN}
    repaired, notes = repair_menace_blocks(state, plan)
    partners = [b for b, a in repaired.items() if a == PALM_NINJA]
    assert len(partners) == 2 and 485 in partners
    assert "lethal" in notes[0]


def test_chump_guard_weighs_a_menace_pair_as_one_block():
    state = with_context(incident_board(extra_threat_power=0), blockers_request())
    pair = {485: PALM_NINJA, 490: PALM_NINJA}
    dropped = wasteful_chump_blocks(state, pair)
    assert set(dropped) == {485, 490}  # both 1/1s die to the 3/3 deathtouch Ninja, which survives
    assert "Rabbit 1/1 and Rabbit 1/1" in dropped[485]
    # A pair that kills the attacker is not a chump; neither member is ever dropped alone.
    hares = {HARE_A: PALM_NINJA, HARE_B: PALM_NINJA}
    assert wasteful_chump_blocks(state, hares) == {}
    assert wasteful_chump_blocks(state, {HARE_A: PALM_NINJA, 485: PALM_NINJA}) == {}


def test_planner_drops_the_lone_menace_block_before_the_survival_check():
    state = with_context(incident_board(), blockers_request())
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_BLOCKERS,
                blocker_assignments={
                    "*Rabbit [id:485]": "Biting-Palm Ninja [id:453]",
                    "*Rabbit [id:491]": "*Ninja [id:477]",
                },
                blocker_instance_assignments={485: PALM_NINJA, 491: NINJA_TOKEN},
                reasoning="Blank the Ninjas",
            )
        ],
        voice_advice="Block Biting-Palm Ninja and the Ninja token with Rabbits.",
    )
    ActionPlanner(Mock())._check_block_menace(plan, state, state["decision_context"])
    action = plan.actions[0]
    assert action.blocker_instance_assignments == {491: NINJA_TOKEN}
    assert action.blocker_assignments == {"*Rabbit [id:491]": "*Ninja [id:477]"}
    assert "Biting-Palm" not in plan.voice_advice


# --- Bug 2: selections Arena already shows ------------------------------------------------


def test_log_remembers_each_blockers_attackers_across_the_combat():
    """gsid 222 lists 484's attackers; the re-issued gsid 224 shows it selected and lists none."""
    gs = GameState()
    gs.turn_info.active_player = OPPONENT
    gs.turn_info.turn_number = 9
    for iid in (*ATTACKERS, *BLOCKERS):
        seat = OPPONENT if iid in ATTACKERS else LOCAL
        gs.game_objects[iid] = GameObject(
            instance_id=iid, grp_id=1, zone_id=28, owner_seat_id=seat, controller_seat_id=seat
        )
    gs._resolve_card_name = lambda grp_id: "Creature"

    def declare(request):
        blockers = [
            {k: v for k, v in entry.items() if v != []} for entry in deepcopy(request["blockers"])
        ]  # the log omits empty lists
        _handle_decision_message(
            gs, "GREMessageType_DeclareBlockersReq", {"declareBlockersReq": {"blockers": blockers}}
        )
        return gs.decision_context

    assert declare(blockers_request())["blocker_options"] == {}
    context = declare(blockers_request({484: SHARK}, gsid=224, msg=300))
    assert context["blocker_options"] == {484: ATTACKERS}
    assert blocker_options(context["raw_blockers"], context["blocker_options"])[484] == set(ATTACKERS)
    gs.turn_info.turn_number = 11  # a later combat never inherits these
    assert declare(blockers_request({484: SHARK}, gsid=300, msg=400))["blocker_options"] == {}


def test_a_selected_blocker_is_offered_with_all_its_options():
    request = blockers_request({484: SHARK}, gsid=224, msg=300)
    state = with_context(incident_board(), request)
    context = state["decision_context"]
    assert blocker_options(request["blockers"], context["blocker_options"])[484] == set(ATTACKERS)
    assert blocker_options(request["blockers"])[484] == {SHARK}  # without the log: only its block
    text = combat_identity_prompt(state, context)
    assert "*Rabbit [id:484] may block: Dark Leo & Shredder [id:442]" in text
    assert "(selected now: Shark Shredder, Killer Clone [id:468])" in text


@pytest.mark.usefixtures("instant_combat_acks")
def test_replan_naming_the_selected_block_is_accepted_and_not_resent():
    """07:34:42: the re-plan proposed 484->468 again and got 'Combat instance 468 is not eligible'."""
    request = blockers_request({484: SHARK}, gsid=224, msg=300)
    # At 13 life the Shark's 4 would leave us at CHUMP_DANGER_LIFE without the chump: the guard keeps it.
    # (At 20 their next swing through our six creatures left is nowhere near lethal: #171 drops it.)
    state = with_context(incident_board(life=13, extra_threat_power=4), request)
    plan = parse(
        {
            "actions": [
                {
                    "action_type": "declare_blockers",
                    "blocker_assignments": {
                        "*Rabbit [id:484]": "Shark Shredder, Killer Clone [id:468]",
                        "*Rabbit [id:491]": "*Ninja [id:477]",
                    },
                }
            ]
        },
        BLOCK_MENU,
        state,
        state["decision_context"],
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].blocker_instance_assignments == {484: SHARK, 491: NINJA_TOKEN}

    bridge = ArenaBlockers(request, state)
    result = engine_for(bridge, state)._try_gre_bridge_blockers(plan.actions[0])

    assert result is not None and result.success
    assert bridge.blocker_calls == [[select(491, NINJA_TOKEN)], []]
    assert bridge.committed == {484: {SHARK}, 491: {NINJA_TOKEN}}


@pytest.mark.usefixtures("instant_combat_acks")
def test_a_selection_the_plan_does_not_make_is_withdrawn():
    request = blockers_request({484: SHARK}, gsid=224, msg=300)
    state = with_context(incident_board(), request)
    action = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"*Rabbit [id:491]": "*Ninja [id:477]"},
        blocker_instance_assignments={491: NINJA_TOKEN},
    )
    bridge = ArenaBlockers(request, state)
    assert engine_for(bridge, state)._try_gre_bridge_blockers(action).success
    assert bridge.blocker_calls == [[select(491, NINJA_TOKEN), select(484)], []]
    assert bridge.committed == {491: {NINJA_TOKEN}}


@pytest.mark.usefixtures("instant_combat_acks")
def test_no_blocks_withdraws_every_shown_selection_before_confirming():
    """07:35:15: "[No Blocks]" was logged while the confirm committed seven blocks."""
    shown = {HARE_A: DARK_LEO, 484: SHARK, 485: DARK_LEO, HARE_B: PALM_NINJA, 490: PALM_NINJA}
    shown.update({491: SHARK, 492: NINJA_TOKEN})
    request = blockers_request(shown, gsid=225, msg=302)
    state = with_context(incident_board(), request)
    bridge = ArenaBlockers(request, state)
    result = engine_for(bridge, state)._try_gre_bridge_blockers(GameAction(ActionType.DECLARE_BLOCKERS))
    assert result.success
    assert bridge.blocker_calls == [[select(blocker) for blocker in BLOCKERS], []]
    assert bridge.committed == {}


@pytest.mark.usefixtures("instant_combat_acks")
def test_a_selected_blocker_moved_to_another_attacker_takes_two_updates():
    request = blockers_request({484: SHARK}, gsid=224, msg=300)
    state = with_context(incident_board(), request)
    action = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"*Rabbit [id:484]": "*Ninja [id:477]"},
        blocker_instance_assignments={484: NINJA_TOKEN},
    )
    bridge = ArenaBlockers(request, state)
    assert engine_for(bridge, state)._try_gre_bridge_blockers(action).success
    assert bridge.blocker_calls == [[select(484)], [select(484, NINJA_TOKEN)], []]
    assert bridge.committed == {484: {NINJA_TOKEN}}


@pytest.mark.usefixtures("instant_combat_acks")
def test_finalize_still_refuses_a_dropped_selection():
    request = blockers_request({484: SHARK}, gsid=224, msg=300)
    state = with_context(incident_board(), request)
    action = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"*Rabbit [id:491]": "*Ninja [id:477]"},
        blocker_instance_assignments={491: NINJA_TOKEN},
    )
    bridge = ArenaBlockers(request, state)
    original = bridge.submit_blockers

    def lose_the_update(assignments):
        if assignments:  # Arena re-issues the request without applying the update
            bridge.blocker_calls.append(deepcopy(assignments))
            bridge.request = {**deepcopy(bridge.request), "game_state_id": 225, "msg_id": 302}
            return True
        return original(assignments)

    bridge.submit_blockers = lose_the_update
    assert engine_for(bridge, state)._try_gre_bridge_blockers(action) is None
    # The refused declaration is retried once as the safe default (2026-10-21
    # 22:09); when Arena drops that too the autopilot hands over.
    assert bridge.blocker_calls[0] == [select(491, NINJA_TOKEN), select(484)]
    assert 2 <= len(bridge.blocker_calls) <= 3 and all(call for call in bridge.blocker_calls)
    assert bridge.committed is None


@pytest.mark.usefixtures("instant_combat_acks")
def test_a_rejected_declaration_is_retried_with_one_blocker_per_attacker():
    """2026-10-21 22:09 (match 7b898598): nine Rabbits on a 9/9 under Flopsie's
    "can't be blocked by more than one creature" came back IllegalRequest
    InvalidBlocker; the refusal stood the autopilot down and the attack went
    unblocked. Arena rejects the gang block here; the retry keeps one Rabbit."""
    request = blockers_request(gsid=224, msg=300)
    state = with_context(incident_board(), request)
    action = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={
            "*Rabbit [id:484]": "Shark Shredder, Killer Clone [id:468]",
            "*Rabbit [id:485]": "Shark Shredder, Killer Clone [id:468]",
            "*Rabbit [id:490]": "Shark Shredder, Killer Clone [id:468]",
        },
        blocker_instance_assignments={484: SHARK, 485: SHARK, 490: SHARK},
    )
    bridge = ArenaBlockers(request, state)
    original = bridge.submit_blockers

    def reject_gang_blocks(assignments):
        per_attacker = {}
        for entry in assignments:
            for attacker in entry["attackerInstanceIds"]:
                per_attacker[attacker] = per_attacker.get(attacker, 0) + 1
        if any(n > 1 for n in per_attacker.values()):  # IllegalRequest: re-issued without the selection
            bridge.blocker_calls.append(deepcopy(assignments))
            bridge.request = {**deepcopy(bridge.request), "game_state_id": 225, "msg_id": 302}
            return True
        return original(assignments)

    bridge.submit_blockers = reject_gang_blocks
    engine = engine_for(bridge, state)
    engine._get_game_state = lambda: state
    result = engine._try_gre_bridge_blockers(action)
    assert result is not None and result.success
    assert bridge.blocker_calls[0] == [select(484, SHARK), select(485, SHARK), select(490, SHARK)]
    assert bridge.committed is not None
    committed_on_shark = [b for b, a in bridge.committed.items() if a == {SHARK}]
    assert len(committed_on_shark) <= 1  # the retry went through the guards (a lone chump at 20 life is dropped)


@pytest.mark.usefixtures("instant_combat_acks")
@pytest.mark.parametrize("remember", [True, False])
def test_safe_default_plans_with_selected_blockers_and_commits_its_plan(remember):
    """07:34:45: the safe default's six blocks were accepted next to 484 and the finalize refused."""
    request = blockers_request({484: SHARK}, gsid=224, msg=300)
    state = with_context(incident_board(), request, remember=remember)
    assignments, _reason = safe_default_blocks(state, request)
    bridge = ArenaBlockers(request, state)

    assert engine_for(bridge, state)._try_interactive_safe_default(state, "decision_required") is True

    assert bridge.blocker_calls[-1] == []
    assert bridge.committed == {blocker: {attacker} for blocker, attacker in assignments.items()}
    # A full search (not the greedy fallback's lone chump): two creatures kill the menace Ninja.
    assert sum(attacker == PALM_NINJA for attacker in assignments.values()) >= 2
    moved = assignments.get(484, SHARK) != SHARK
    assert len(bridge.blocker_calls) == (3 if moved else 2)  # a moved selection takes two updates


def test_safe_default_never_reads_selected_blockers_as_unable_to_block():
    """07:35:15: every blocker showed a selection; the safe default said 'no creature can block'."""
    shown = {HARE_A: DARK_LEO, 484: SHARK, 485: DARK_LEO, HARE_B: PALM_NINJA, 490: PALM_NINJA}
    shown.update({491: SHARK, 492: NINJA_TOKEN})
    request = blockers_request(shown, gsid=225, msg=302)
    for remember in (True, False):
        assignments, reason = safe_default_blocks(with_context(incident_board(), request, remember), request)
        assert "no creature can block" not in reason
        assert assignments  # 9 damage at 20 life: the solver blocks


def test_mac_adapter_withdraws_a_blocker_with_an_empty_selection():
    from tests.test_mac_bridge_adapter import MSG, FakeGame, adapter_for, listing

    blocker = {
        "$c": MSG + "Blocker",
        "$h": 21,
        "blockerInstanceId_": 300,
        "attackerInstanceIds_": listing(),
        "selectedAttackerInstanceIds_": listing(400),
    }
    game = FakeGame(
        {"$c": "GreClient.Rules.DeclareBlockersRequest", "$h": 5, "AllBlockers": listing(blocker)}
    )
    response = adapter_for(game).handle(
        {"action": "submit_blockers", "assignments": [{"blockerInstanceId": 300, "attackerInstanceIds": []}]}
    )
    assert response == {"ok": True, "submitted_type": "DeclareBlockers", "needs_finalize": True}
    [submit] = game.submits()
    assert any(op.get("method") == "Clear" for op in submit)
    assert not any(op.get("method") == "Add" and op.get("args") == [{"uint": 400}] for op in submit)


# --- Bug 8: identical copies of our own creatures ----------------------------------------


def effigy_board():
    """08:26:34: board order Fleeting Effigy 337, Hired Claw 335, Fleeting Effigy 333, Emberheart 302."""
    ours = [
        creature(337, "Fleeting Effigy", 2, 2, LOCAL),
        creature(335, "Hired Claw", 1, 2, LOCAL),
        creature(333, "Fleeting Effigy", 2, 2, LOCAL),
        creature(302, "Emberheart Challenger", 2, 2, LOCAL),
    ]
    for card in ours:
        card["oracle_text"] = f"{card['name']} rules text."
    return {
        "local_seat_id": LOCAL,
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": 20},
            {"seat_id": OPPONENT, "is_local": False, "life_total": 12},
        ],
        "battlefield": ours,
    }


ATTACK_MENU = [
    "Attack with: Emberheart Challenger (2/2)",
    "Attack with: Fleeting Effigy #1 (2/2)",
    "Attack with: Fleeting Effigy #2 (2/2)",
    "Done (confirm attackers)",
]


def test_a_repeated_bare_name_attacks_with_each_identical_copy():
    state = effigy_board()
    player = {"type": "Player", "playerSystemSeatId": OPPONENT}
    raw = [{"attackerInstanceId": iid, "legalDamageRecipients": [player]} for iid in (302, 333, 337)]
    context = {"type": "declare_attackers", "raw_attackers": raw, "legal_attacker_ids": [302, 333, 337]}
    state["decision_context"] = context
    plan = parse(
        {
            "actions": [
                {
                    "action_type": "declare_attackers",
                    "attacker_names": ["Emberheart Challenger", "Fleeting Effigy", "Fleeting Effigy"],
                    "attacker_targets": {"Emberheart Challenger": "Opponent", "Fleeting Effigy": "Opponent"},
                }
            ]
        },
        ATTACK_MENU,
        state,
        context,
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].attacker_instance_ids == [302, 333, 337]


def test_menu_ordinals_bind_first_and_bare_names_take_the_copies_left():
    state = effigy_board()
    eligible = [302, 333, 337]
    # Board "#1" is 337 but the menu's "#1" is 333: the menu entry wins for identical copies.
    assert resolve_combatant("Fleeting Effigy #1", state, eligible, local_side=True) == 333
    assert resolve_combatants(
        ["Fleeting Effigy", "Fleeting Effigy #1"], state, eligible, local_side=True
    ) == [
        337,
        333,
    ]
    with pytest.raises(ValueError):
        resolve_combatants(["Fleeting Effigy"] * 3, state, eligible, local_side=True)
    with pytest.raises(ValueError):
        resolve_combatants(["Fleeting Effigy #2", "Fleeting Effigy #2"], state, eligible, local_side=True)


def test_copies_that_differ_still_need_an_instance_id():
    state = effigy_board()
    state["battlefield"][0]["counters"] = {"+1/+1": 1}
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_combatants(["Fleeting Effigy", "Fleeting Effigy"], state, [302, 333, 337], local_side=True)


def test_hare_apparent_menu_ordinal_blocks_with_that_copy():
    """07:33:23: board order put 460 before 447; the menu (request order) put 447 first."""
    state = {
        "local_seat_id": LOCAL,
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": 21},
            {"seat_id": OPPONENT, "is_local": False, "life_total": 25},
        ],
        "battlefield": [
            creature(460, "Hare Apparent", 2, 2, LOCAL),
            creature(447, "Hare Apparent", 2, 2, LOCAL),
            creature(458, "Ninja", 2, 2, OPPONENT, attacking=True),
            creature(464, "Rabbit", 1, 1, LOCAL, token=True),
        ],
    }
    raw = [{"blockerInstanceId": b, "attackerInstanceIds": [458]} for b in (447, 460, 464)]
    context = {"type": "declare_blockers", "raw_blockers": raw, "legal_blocker_ids": [447, 460, 464]}
    state["decision_context"] = context
    plan = parse(
        {
            "actions": [
                {"action_type": "declare_blockers", "blocker_assignments": {"Hare Apparent #1": "Ninja"}}
            ]
        },
        ["Block with: Hare Apparent #1", "Block with: Hare Apparent #2", "Block with: Rabbit"],
        state,
        context,
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].blocker_instance_assignments == {447: 458}


def test_bare_token_name_takes_a_free_copy_that_can_block_the_attacker():
    """Rabbit 484 is the first copy but (without the log's memory) can only keep its block on 468."""
    request = blockers_request({484: SHARK}, gsid=224, msg=300)
    state = with_context(incident_board(), request, remember=False)
    plan = parse(
        {
            "actions": [
                {
                    "action_type": "declare_blockers",
                    "blocker_assignments": {
                        "*Rabbit [id:491]": "*Ninja [id:477]",
                        "*Rabbit": "*Ninja [id:477]",
                    },
                }
            ]
        },
        BLOCK_MENU,
        state,
        state["decision_context"],
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].blocker_instance_assignments == {491: NINJA_TOKEN, 485: NINJA_TOKEN}
