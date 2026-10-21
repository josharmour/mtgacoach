"""The all-in declaration names attackers the bridge can resolve (ledger #208).

2026-10-20 18:14:25 (match 4ab26c53, Brawl Leonardo v12): the combat solver went all in with 21
attackers (5 Hare Apparent, 16 Rabbits) for a Sneak line. The override labelled copies '#N' by
sorted instance ID, but combat_identity numbers copies in board order, so "Hare Apparent #1"
named a copy the menu did not offer: 'Combat creature ... is unavailable or ambiguous', the whole
declaration was rejected and the safe default attacked with nothing.
"""

from __future__ import annotations

from tests import leonardo_boards_20261019 as lb

from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.combat_identity import resolve_combatants


def _declare_step_board_order_differs() -> tuple[dict, list[int]]:
    state = lb.t12_main1()
    state["turn"].update({"phase": "Phase_Combat", "step": "Step_DeclareAttack"})
    # Player.log zone order is not instance-ID order; the newest Hare just entered and can't attack.
    state["battlefield"] = list(reversed(state["battlefield"]))
    ours = [
        c["instance_id"]
        for c in state["battlefield"]
        if c.get("controller_seat_id", c.get("owner_seat_id")) == lb.LOCAL
        and c["name"] in ("Hare Apparent", "Rabbit")
        and not c.get("is_tapped")
    ]
    newest_hare = max(
        c["instance_id"]
        for c in state["battlefield"]
        if c["name"] == "Hare Apparent" and c["instance_id"] in ours
    )
    legal = [i for i in ours if i != newest_hare]
    state["decision_context"] = {"type": "declare_attackers", "legal_attacker_ids": legal}
    return state, legal


def test_every_all_in_label_resolves_to_its_own_legal_attacker():
    state, legal = _declare_step_board_order_differs()
    action = _BridgeSubmitMixin._all_in_attack_action(None, state, [])
    assert action is not None and len(action.attacker_names) == len(legal)
    resolved = resolve_combatants(action.attacker_names, state, legal, local_side=True)
    assert sorted(resolved) == sorted(legal)
