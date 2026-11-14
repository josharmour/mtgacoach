import copy
import json

import pytest

from arenamcp.card_facts.compiler import _raw_native_type_problems, compile_one
from arenamcp.card_facts.validate import PrintedCard

FIELDS = [
    ("GainLife", "amount"),
    ("Draw", "count"),
    ("Damage", "amount"),
    ("Pump", "power"),
    ("Pump", "toughness"),
    ("AddMana", "amount"),
    ("PutCounters", "count"),
]


@pytest.mark.parametrize("tag,key", FIELDS)
@pytest.mark.parametrize("value", [True, 1.5, "X", None])
def test_noninteger_native_effect_rejected(tag, key, value):
    assert _raw_native_type_problems({"effects": [{"effect": {"effect_type": tag, key: value}}]})


@pytest.mark.parametrize("tag,key", FIELDS)
def test_integer_effect_fields_preserved(tag, key):
    data = {"effects": [{"effect": {"effect_type": tag, key: 2, "future_extension": "unchanged"}}]}
    before = copy.deepcopy(data)
    assert _raw_native_type_problems(data) == []
    assert data == before


def test_signed_pump_defaults_and_unknown_preserved():
    data = {
        "effects": [
            {"effect": {"effect_type": "Pump", "power": -2, "toughness": 0}},
            {"effect": {"effect_type": "Draw"}},
            {"effect": {"effect_type": "Unknown", "text": "Original variable statement", "amount": "X"}},
        ]
    }
    assert _raw_native_type_problems(data) == []


def test_variable_damage_retries_to_unknown_not_guessed_integer():
    text = "This deals X damage to target creature."

    class Resolver:
        def resolve(self, _):
            return PrintedCard("Test", type_line="Artifact", oracle_text=text)

    class Backend:
        last_request_metrics = None

        def __init__(self):
            self.calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            effect = (
                {"effect_type": "Damage", "amount": "X", "target": "creature"}
                if self.calls == 1
                else {"effect_type": "Unknown", "text": text}
            )
            return json.dumps({"name": "Test", "type_line": "Artifact", "effects": [{"effect": effect}]})

    backend = Backend()
    result = compile_one("Test", resolver=Resolver(), backend=backend)
    assert backend.calls == 2 and result.status == "compiled"
    assert result.record.effects[0].effect.text == text
