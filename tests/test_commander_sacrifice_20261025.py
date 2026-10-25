"""Flare's sacrifice alternative was hidden as unpayable (bug_20261025_133034)."""

from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp import decisions
from arenamcp.commander_combat import commander_sacrifice_context
from arenamcp.decisions import build_pending_decision, submit_option
from arenamcp.play_safety import filter_play_options

RULE = "You may sacrifice a nontoken green creature rather than pay this spell's mana cost."


def menu(monkeypatch, mana_cost=None):
    monkeypatch.setattr(decisions, "_default_ability_description", lambda _: ("", RULE))
    alternative = {"actionType": "Cast", "grpId": 91004, "instanceId": 558, "alternativeGrpId": 172365}
    if mana_cost:
        alternative["manaCost"] = mana_cost
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {
                    "actionType": "Cast",
                    "grpId": 91004,
                    "instanceId": 558,
                    "hasAutoTap": True,
                    "manaCost": [{"color": "Green", "count": 2}],
                },
                alternative,
            ],
        },
        resolve_name=lambda _: "Flare of Cultivation",
    )


def state():
    return {
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True, "commander_ids": [670]}],
        "commander_casts": {"103511": 1},
        "battlefield": [
            {
                "name": "The Notary Hobbits",
                "instance_id": 670,
                "grp_id": 103511,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "object_kind": "CARD",
                "type_line": "Legendary Creature — Halfling Advisor",
                "mana_cost": "{3}{G}{G}",
                "oracle_text": "When this enters, create two copies.",
            }
        ],
        "hand": [
            {
                "name": "Flare of Cultivation",
                "instance_id": 558,
                "grp_id": 91004,
                "type_line": "Sorcery",
                "oracle_text": RULE,
                "mana_cost": "{1}{G}{G}",
            }
        ],
    }


def test_nonmana_alternative_reaches_the_model_and_submits_its_own_index(monkeypatch):
    decision = filter_play_options(menu(monkeypatch), state())
    option = decision.find("idx:1")
    assert option is not None and option.payable is None
    assert "cannot auto-pay" not in option.label
    assert RULE in option.label
    assert decision.find("idx:0").payable is True
    bridge = Mock()
    bridge.submit_action_by_index.return_value = True
    assert submit_option(bridge, decision, ["idx:1"])
    assert bridge.submit_action_by_index.call_args.args == (1,)


def test_mana_bearing_alternative_is_not_assumed_payable(monkeypatch):
    assert menu(monkeypatch, [{"color": "Generic", "count": 2}]).find("idx:1").payable is False


def test_recovery_note_uses_current_original_and_tax_not_departed_copies(monkeypatch):
    text = "\n".join(commander_sacrifice_context(state(), menu(monkeypatch)))
    assert "0 other same-card bodies" in text
    assert "printed {3}{G}{G} plus {2} tax" in text
    assert "before sacrificing" in text
    changed = state()
    changed["players"][0]["commander_ids"] = [999]
    assert commander_sacrifice_context(changed, menu(monkeypatch)) == []


def test_recovery_note_does_not_invent_a_sacrifice_option():
    assert commander_sacrifice_context(state(), SimpleNamespace(options=[])) == []
