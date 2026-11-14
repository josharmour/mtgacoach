import json

import pytest

from arenamcp.card_facts.compiler import _printed_cast_contract_problems, compile_one
from arenamcp.card_facts.validate import PrintedCard


@pytest.mark.parametrize("cost", ["{0}", "{1}{G}", "{X}{R}", "{W/U}"])
def test_nonland_missing_normal_cast_rejected(cost):
    card = PrintedCard("Spell", cost, "Artifact")
    assert _printed_cast_contract_problems({"modes": [{"name": "activate"}]}, card)
    assert not _printed_cast_contract_problems({"modes": [{"name": "cast"}]}, card)


@pytest.mark.parametrize(
    "card",
    [
        PrintedCard("Land", "", "Land"),
        PrintedCard("No cost", "", "Creature"),
        PrintedCard("Front // Back", "{1}", "Creature // Land"),
        PrintedCard("Faces", "{1}", "Creature", "front\n---\nback"),
    ],
)
def test_out_of_scope_not_inferred(card):
    assert not _printed_cast_contract_problems({"modes": []}, card)


def test_persistent_mox_omission_fails_without_synthesizing_cast():
    card = PrintedCard(
        "Chrome Mox",
        "{0}",
        "Artifact",
        "Imprint — When Chrome Mox enters, you may exile a nonartifact, nonland card from your hand.\n{T}: Add one mana of any of the exiled card's colors.",
    )
    data = {
        "name": card.name,
        "mana_cost": card.mana_cost,
        "type_line": card.type_line,
        "modes": [{"name": "activate", "cost": {"tap": True}}],
    }

    class Backend:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            return json.dumps(data)

    class Resolver:
        def resolve(self, _):
            return card

    backend = Backend()
    result = compile_one(card.name, resolver=Resolver(), backend=backend)
    assert result.status == "failed" and backend.calls == 2
    assert any("normal cast mode" in problem for problem in result.problems)
    assert all(mode.name != "cast" for mode in result.record.modes)


def test_exact_retained_pilot_mox_missing_cast_rejected():
    import hashlib
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures/f3_pilot_20261114/chrome_mox.json").read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest() == "bbd3f0a54e11ad9c62f935de59e1c982da98d1bb4da6ad3b498af482b7c055c3"
    )
    record = json.loads(raw)["record"]
    card = PrintedCard("Chrome Mox", "{0}", "Artifact")
    assert _printed_cast_contract_problems(record, card)
    assert record["effects"][1]["effect"]["effect_type"] == "Unknown"
