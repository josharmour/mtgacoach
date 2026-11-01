"""Name-level plan diagnostics must preserve instance-level attack legality."""

from types import SimpleNamespace

import pytest

from arenamcp.line_search import _plan_attackers


@pytest.mark.parametrize("ready,tapped", [(True, False), (False, False), (True, True)])
def test_named_copy_attack_keeps_only_ready_instances(ready, tapped):
    bodies = [
        {"instance_id": 1, "name": "Witness", "_can_attack": True},
        {"instance_id": 2, "name": "Witness", "_can_attack": True},
    ]
    search = SimpleNamespace(sick=lambda body, *_: body["instance_id"] == 2 or not ready)
    mid = SimpleNamespace(
        node=SimpleNamespace(k=0, abs_turn=12),
        ours=bodies,
        our_tapped={1} if tapped else set(),
        entered={1: 10, 2: 12},
    )
    issues = []
    attackers = _plan_attackers(search, mid, "Witness", "T", issues)
    assert attackers == ((1,) if ready and not tapped else ())
    assert bool(issues) is (not ready or tapped)
    if issues:
        assert "T: Witness can't attack the turn it is cast" in issues
