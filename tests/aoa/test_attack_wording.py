"""Attack calls agree with the attacker count, subject, and event age."""

from dataclasses import replace

import pytest
from test_headless_booth import observation

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.headless import deterministic


@pytest.mark.parametrize("count,noun", [(1, "creature"), (2, "creatures")])
@pytest.mark.parametrize("age", [0, 5])
@pytest.mark.parametrize("persona", ["technical", "hype"])
@pytest.mark.parametrize(
    "names,seat,actor,verb",
    [
        ({1: "Armour.tv"}, 1, "Armour.tv", "attacks"),
        ({2: "Opponent"}, 2, "Opponent", "attacks"),
        ({}, 1, "You", "attack"),
        ({}, 2, "Your opponent", "attacks"),
    ],
)
def test_attack_call_grammar(count, noun, age, persona, names, seat, actor, verb):
    obs = observation()
    event = replace(
        obs.events[0],
        kind="combat",
        seat=seat,
        payload={"count": count},
        received_at=obs.arrived_at - age,
    )
    obs = replace(obs, events=(event,), public_state={**obs.public_state, "player_names": names})
    lines = deterministic(obs, CommentaryConfig(mode="commentary", persona=persona, speakers=1))
    ending = "!" if persona == "hype" and age == 0 else "."
    verb = "attacked" if age else verb
    assert lines[0].text == f"{actor} {verb} with {count} {noun}{ending}"
