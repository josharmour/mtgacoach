"""Timeline replay of #141: startup + repeated turn *and threat* changes on the hosted model.

Time is scaled so one simulated second is ~100 real seconds: turn length 0.12 s
(~12 s), refresh cooldown 0.15 s (~15 s), first-plan budget 0.20 s (~20 s),
first-plan patience 0.45 s (~45 s), full budget 0.75 s (~75 s), and a hosted
'low'/'high' answer takes 0.45 s (~45 s) while 'none' takes 0.20 s.

The opponent adds a creature on turns 7, 11, 15 and 19, so the in-flight reform
also meets the threat-supersede path (their turn only).
"""

import json
import time

from arenamcp.game_plan import GamePlanManager

PLAN = json.dumps(
    {
        "role": "aggressor",
        "turns": [
            {"turn": "T", "land": "", "cast": [], "attack": "all", "hold": ""},
            {"turn": "T+1", "land": "", "cast": [], "attack": "all", "hold": ""},
            {"turn": "T+2", "land": "", "cast": [], "attack": "all", "hold": ""},
        ],
        "win_conditions": ["aggro beatdown"],
        "path": "race for lethal ~T6",
        "threat": "opponent flyers",
        "develop_next": "deploy a 2-drop",
    }
)

TURN_S = 0.12
THREAT_TURNS = {7, 11, 15, 19}


class HostedBackend:
    """Hosted 'current' model: no hidden reasoning is fast, low/high reasoning is slow."""

    model = "current"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens, **kwargs):
        effort = str(kwargs.get("reasoning_effort") or "")
        self.calls.append((effort, round(float(kwargs.get("request_timeout_s") or 0), 3)))
        time.sleep(0.20 if effort == "none" else 0.45)
        return PLAN


def state(turn, *, active=1, opp_creatures=0):
    bf = [
        {
            "type_line": "Creature — Goblin",
            "controller_seat_id": 1,
            "power": 2,
            "toughness": 2,
            "name": f"Mine{i}",
            "instance_id": 100 + i,
        }
        for i in range(2)
    ]
    bf += [
        {
            "type_line": "Creature — Treefolk",
            "controller_seat_id": 2,
            "power": 3,
            "toughness": 4,
            "name": f"Theirs{i}",
            "instance_id": 200 + i,
        }
        for i in range(opp_creatures)
    ]
    return {
        "local_seat_id": 1,
        "players": [
            {"is_local": True, "seat_id": 1, "life_total": 20, "hand_size": 5},
            {"is_local": False, "seat_id": 2, "life_total": 20, "hand_size": 5},
        ],
        "turn": {"turn_number": turn, "active_player": active, "phase": "Main1"},
        "battlefield": bf,
    }


def scaled(mgr):
    mgr._REFRESH_INTERVAL_S = 0.15
    mgr._FIRST_PLAN_PATIENCE_S = 0.45
    mgr._INITIAL_HOSTED_PLAN_TIMEOUT_S = 0.20
    mgr._YIELD_GRACE_S = 0.04
    mgr._timeout = 0.75
    return mgr


def run(turns=22):
    be = HostedBackend()
    mgr = scaled(GamePlanManager(be))
    events = []
    last = None
    first_plan_turn = None
    fresh_plan_turn = None
    landings = []
    seen_plan = None
    opp = 0
    t0 = time.monotonic()
    for turn in range(3, turns + 1):
        if turn in THREAT_TURNS:
            opp += 1
        # Two triggers per turn: the start of the turn and mid-turn.
        for _ in range(2):
            started = mgr.request_reform(state(turn, active=1 if turn % 2 else 2, opp_creatures=opp))
            now = time.monotonic() - t0
            if started:
                events.append(f"{now:5.2f}s T{turn}: reform started")
            # poll for state transitions while the turn elapses
            deadline = time.monotonic() + TURN_S / 2
            while time.monotonic() < deadline:
                plan = mgr._plan
                snap = (
                    plan.turn_formed if plan else None,
                    plan.effort if plan else None,
                    mgr._plan_attempts,
                    mgr._plan_cancels,
                    bool(mgr._inflight),
                )
                if snap != last:
                    now = time.monotonic() - t0
                    events.append(
                        f"{now:5.2f}s T{turn}: plan={snap[0]}/{snap[1]} attempts={snap[2]} "
                        f"cancels={snap[3]} inflight={snap[4]}"
                    )
                    if snap[0] is not None and first_plan_turn is None:
                        first_plan_turn = turn
                    if snap[0] is not None and snap[0] >= turn and fresh_plan_turn is None:
                        fresh_plan_turn = turn
                    last = snap
                if plan is not None and plan is not seen_plan:
                    seen_plan = plan
                    landings.append((plan.turn_formed, turn))
                time.sleep(0.01)
    print("\n".join(events))
    print()
    print("calls:", be.calls)
    print(f"first plan landed on turn {first_plan_turn}; fresh plan (formed this turn) on {fresh_plan_turn}")
    print(f"plans landed (formed turn -> landing turn): {landings}")
    print(
        f"attempts={mgr._plan_attempts} cancels={mgr._plan_cancels} "
        f"supersedes={mgr._consecutive_supersedes} outcome={mgr._plan_outcome}"
    )
    return first_plan_turn, fresh_plan_turn


if __name__ == "__main__":
    run()
