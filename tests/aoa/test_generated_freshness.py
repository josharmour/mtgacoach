import copy
import os
import sys
import threading
from dataclasses import replace

import pytest
from test_headless_booth import observation, source  # noqa: F401
from test_strategy_content import card

from arenamcp.commentary.client import CommentaryClient
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.contracts import Utterance
from arenamcp.commentary.timeline import position_fingerprint, valid_for


@pytest.mark.usefixtures("source")
@pytest.mark.parametrize("scope", ["position", "snapshot"])
def test_real_worker_binds_only_audited_position_dependencies(scope):
    obs = observation()
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"] = [card(20, "Known creature", "")]
    obs = replace(obs, public_state=public)
    config = CommentaryConfig(
        mode="commentary", generative=True, python=sys.executable, aoa_source=os.environ["AOA_SOURCE"]
    )

    def rpc(system, context, audit):
        if audit:
            assert "state:battlefield" in context["lines"][0]["refs"]
            return {
                "checks": [
                    {
                        "index": 0,
                        "supported": True,
                        "adds_insight": True,
                        "unsupported_claim": "",
                        "scope": scope,
                    }
                ]
            }
        return {
            "turns": [
                {
                    "role": "color_analyst",
                    "text": "The creature could turn this spell into pressure.",
                    "refs": [context["new_events"][0], "state:battlefield"],
                }
            ]
        }

    client = CommentaryClient()
    try:
        result = client.generate(obs, config, rpc, threading.Event())
        assert len(result["utterances"]) == 1, result["status"]
        line = Utterance.from_wire(result["utterances"][0])
        assert line.position_scope == ("audit:field,identity" if scope == "position" else "")
        assert line.expires_at == obs.arrived_at + (15 if scope == "position" else 8)
        changed = copy.deepcopy(public)
        changed["players"][0]["life_total"] = 17
        changed["zones"]["stack"] = []
        changed["turn_info"]["phase"] = "Combat"
        fresh = replace(
            obs,
            snapshot_id="changed",
            public_state=changed,
            private_state={"hand": [card(30, "New draw", "")]},
        )
        assert valid_for(line, fresh) == (scope == "position")
        changed["zones"]["battlefield"][0]["power"] = 9
        assert not valid_for(line, fresh)
        assert not valid_for(line, replace(obs, epoch=replace(obs.epoch, game_number=2)))
    finally:
        client.close()


def test_private_and_player_changes_invalidate_exact_components():
    obs = observation()
    for scope, change in [
        ("facts:identity,players", replace(obs, public_state={**obs.public_state, "players": []})),
        ("facts:identity,private", replace(obs, private_state={"hand": [card(33, "Draw", "")]})),
        ("facts:identity,zones", replace(obs, public_state={**obs.public_state, "zones": {}})),
    ]:
        assert position_fingerprint(obs, scope) != position_fingerprint(change, scope)
