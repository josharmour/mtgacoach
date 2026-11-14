import copy
import json

import pytest

from arenamcp.card_facts.compiler import _raw_native_type_problems, compile_one
from arenamcp.card_facts.validate import PrintedCard


@pytest.mark.parametrize(
    "field,value", [("life", 1.5), ("discard", True), ("exile_from_graveyard", "2"), ("tap", "false")]
)
def test_cost_coercions_refused(field, value):
    assert _raw_native_type_problems({"modes": [{"cost": {field: value}}]})


@pytest.mark.parametrize("value", ["false", 0, None])
def test_mode_boolean_exact(value):
    assert _raw_native_type_problems({"modes": [{"stays_on_battlefield": value}]})


@pytest.mark.parametrize(
    "field,value",
    [("count", 1.5), ("power", "2"), ("toughness", True), ("tapped", "false"), ("copy_of_self", 0)],
)
def test_token_native_types_exact(field, value):
    assert _raw_native_type_problems(
        {"effects": [{"effect": {"effect_type": "CreateToken", "spec": {field: value}}}]}
    )


def test_valid_native_and_unknown_extensions_unchanged():
    data = {
        "modes": [
            {
                "stays_on_battlefield": False,
                "cost": {"tap": False, "life": 0, "discard": 2, "loyalty": -2, "sacrifice": {"count": 1}},
            }
        ],
        "effects": [
            {"effect": {"effect_type": "Unknown", "text": "Original source", "future_count": "X"}},
            {
                "effect": {
                    "effect_type": "CreateToken",
                    "spec": {
                        "name": "Test",
                        "count": 1,
                        "power": None,
                        "toughness": 0,
                        "tapped": False,
                        "colors": ["R"],
                    },
                }
            },
        ],
    }
    before = copy.deepcopy(data)
    assert _raw_native_type_problems(data) == []
    assert data == before
    assert _raw_native_type_problems({"modes": None, "effects": None}) == []


def test_compiler_retry_before_cost_coercion():
    card = PrintedCard("Test", mana_cost="{1}", type_line="Artifact")

    class Resolver:
        def resolve(self, _):
            return card

    class Backend:
        last_request_metrics = None

        def __init__(self):
            self.calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            return json.dumps(
                {
                    "name": "Test",
                    "mana_cost": "{1}",
                    "type_line": "Artifact",
                    "modes": [
                        {
                            "name": "cast",
                            "cost": {"mana": "{1}", "tap": "false" if self.calls == 1 else False},
                        }
                    ],
                }
            )

    backend = Backend()
    result = compile_one("Test", resolver=Resolver(), backend=backend)
    assert backend.calls == 2 and result.status == "compiled"
    assert result.record.modes[0].cost.tap is False
