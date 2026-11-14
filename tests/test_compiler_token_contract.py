import json

import pytest

from arenamcp.card_facts.compiler import _token_contract_problems, compile_one
from arenamcp.card_facts.validate import PrintedCard


def data(effect):
    return {
        "name": "Test",
        "mana_cost": "{1}",
        "type_line": "Artifact",
        "body": {},
        "modes": [{"name": "cast", "cost": {"mana": "{1}"}}],
        "effects": [{"trigger": "ETB", "effect": effect}],
        "confidence": "high",
    }


def test_explicit_color_and_legendary_require_unknown():
    text = "When this enters, you may create Boo, a legendary 1/1 red Hamster creature token with trample and haste."
    printed = PrintedCard("Test", oracle_text=text)
    near = data(
        {
            "effect_type": "CreateToken",
            "spec": {"name": "Boo", "power": 1, "toughness": 1, "legendary": True, "colors": ["R"]},
        }
    )
    assert _token_contract_problems(near, printed)
    assert _token_contract_problems(data({"effect_type": "Unknown", "text": text}), printed) == []
    assert _token_contract_problems(data({"effect_type": "Unknown", "text": "Create a token."}), printed)


@pytest.mark.parametrize(
    "text",
    [
        "Create a 1/1 green creature token.",
        "Create a legendary creature token.",
        "Create a colorless artifact token.",
    ],
)
def test_schema_gap_not_card_name_specific(text):
    assert _token_contract_problems(
        data({"effect_type": "CreateToken", "spec": {}}), PrintedCard("Other", oracle_text=text)
    )


@pytest.mark.parametrize(
    "text",
    [
        "Create a Treasure token.",
        "Red spells cost less. Create a Treasure token.",
        "Destroy target red creature token.",
    ],
)
def test_unrelated_or_supported_statements_not_rejected(text):
    assert (
        _token_contract_problems(
            data({"effect_type": "CreateToken", "spec": {}}), PrintedCard("Other", oracle_text=text)
        )
        == []
    )


def test_real_compiler_retry_retains_full_unknown():
    text = "Create a 1/1 red creature token."
    printed = PrintedCard("Test", mana_cost="{1}", type_line="Artifact", oracle_text=text)

    class Resolver:
        def resolve(self, identifier):
            return printed

    class Backend:
        last_request_metrics = None

        def __init__(self):
            self.calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            effect = (
                {"effect_type": "CreateToken", "spec": {"power": 1, "toughness": 1}}
                if self.calls == 1
                else {"effect_type": "Unknown", "text": text}
            )
            return json.dumps(data(effect))

    backend = Backend()
    result = compile_one("Test", resolver=Resolver(), backend=backend)
    assert backend.calls == 2
    assert result.status == "compiled", result.problems
    assert result.record.effects[0].effect.text == text


@pytest.mark.parametrize(
    "text",
    [
        "Create a Treasure token, then put a +1/+1 counter on target red creature token.",
        "Whenever you create a red creature token, draw a card. Create a Treasure token.",
    ],
)
def test_other_token_target_or_trigger_is_not_creation_property(text):
    assert (
        _token_contract_problems(
            data({"effect_type": "CreateToken", "spec": {"name": "Treasure"}}),
            PrintedCard("Test", oracle_text=text),
        )
        == []
    )
