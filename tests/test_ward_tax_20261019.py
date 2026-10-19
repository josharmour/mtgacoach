"""Ward taxes, from match a9cd5a2e (2026-10-19, ledger #50 and #51).

Player.log (a9cd5a2e, Mono-Red Burn vs a W/U Spider deck):

- T8 10:36:45: Impractical Joke #319 aimed at Skyward Spider #287, a 4/3 with
  its own Ward {2} wearing Sheltered by Ghosts ("has lifelink and ward {2}").
  The Spider's uniqueAbilities listed ward 141939 twice; two ward triggers
  (#320, #321) went on the stack. With two untapped Mountains left we paid the
  first {2} (PayCostsReq msgId 244) and the second countered the Joke anyway:
  the board-math line ("plays Mountain, then casts Impractical Joke") and the
  prompts all read one {2}.
- T10 10:38:28: Burst Lightning #332 aimed at the fresh 2/2 Skyward Spider
  #328; its ward trigger #333 asked for {2} (PayCostsReq msgId 318, manaCost
  objectId 332 — our own spell) with Mountains 291 and 318 untapped.
  The ward lookup (then ``ward_trigger_source``, now ``pending_ward_payment``)
  got our spell's id and gave up, the optional-cost gate asked the model
  "Optional cost from: Burst Lightning", the model read Burst's "{4} kicker"
  and declined, and Burst Lightning was countered.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest
from tests.fakes import make_engine

from arenamcp import ward
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_pay_costs

LOCAL, OPP = 1, 2

SPIDER_TEXT = "Ward {o2}\nCARDNAME has flying as long as it's modified."
AURA_TEXT = (
    "Enchant creature you control\nWhen CARDNAME enters, exile target nonland permanent an opponent controls "
    "until CARDNAME leaves the battlefield.\nEnchanted creature gets <nobr>+1/+0</nobr> and has lifelink and "
    "ward {o2}."
)
JOKE_TEXT = "Damage can't be prevented this turn. CARDNAME deals 3 damage to up to one target creature or planeswalker."
BURST_TEXT = (
    "Kicker {o4}\nCARDNAME deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead."
)
EMBERHEART_TEXT = (
    "Haste\nProwess\nValiant — Whenever this creature becomes the target of a spell or ability you control for "
    "the first time each turn, exile the top card of your library. Until end of turn, you may play that card."
)
# The card database's text for the Spiders' ability grpIds (Abilities/Localizations_enUS).
ABILITY_TEXT = {
    141939: "Ward {o2}",
    172428: "CARDNAME has flying as long as it's modified.",
    8: "Flying",
    12: "Lifelink",
}
# GameObject 287 at gameStateId 194 (10:36:55): its own ward, the modified-flying
# line, and flying, lifelink and a second ward from Sheltered by Ghosts (97964, 92090, 92090).
SPIDER_287_ABILITIES = [141939, 172428, 8, 12, 141939]


def _pay_costs(line: str) -> dict:
    return json.loads(line)


# Player.log 10:36:52, GREMessageType_PayCostsReq msgId 244 (the first of the Joke's two ward triggers).
PAY_COSTS_244 = _pay_costs(
    """{"type": "GREMessageType_PayCostsReq", "systemSeatIds": [1], "msgId": 244, "gameStateId": 179,
 "prompt": {"promptId": 11, "parameters": [{"parameterName": "Cost", "type": "ParameterType_NonLocalizedString",
  "stringValue": "o2"}]},
 "payCostsReq": {"manaCost": [{"color": ["ManaColor_Generic"], "count": 2, "objectId": 319}],
  "paymentActions": {"actions": [
   {"actionType": "ActionType_Activate_Mana", "grpId": 65385, "instanceId": 291, "facetId": 291, "abilityGrpId": 1004,
    "manaPaymentOptions": [{"mana": [{"manaId": 236, "color": "ManaColor_Red", "srcInstanceId": 291,
     "specs": [{"type": "ManaSpecType_Predictive"}], "abilityGrpId": 1004, "count": 1}]}], "isBatchable": true,
    "uniqueAbilityId": 226},
   {"actionType": "ActionType_Activate_Mana", "grpId": 65385, "instanceId": 318, "facetId": 318, "abilityGrpId": 1004,
    "manaPaymentOptions": [{"mana": [{"manaId": 237, "color": "ManaColor_Red", "srcInstanceId": 318,
     "specs": [{"type": "ManaSpecType_Predictive"}], "abilityGrpId": 1004, "count": 1}]}], "isBatchable": true,
    "uniqueAbilityId": 260}]},
  "paymentSelection": {"context": "SelectionContext_ManaPool", "optionContext": "OptionContext_Payment",
   "listType": "SelectionListType_Dynamic", "idType": "IdType_ManaId",
   "validationType": "SelectionValidationType_NonRepeatable", "minWeight": -2147483648, "maxWeight": 2147483647},
  "autoTapActionsReq": {"autoTapSolutions": [{"autoTapActions": [
   {"instanceId": 291, "abilityGrpId": 1004, "manaPaymentOption": {"mana": [{"manaId": 236, "color": "ManaColor_Red",
    "srcInstanceId": 291, "specs": [{"type": "ManaSpecType_Predictive"}], "abilityGrpId": 1004, "count": 1}]}},
   {"instanceId": 318, "abilityGrpId": 1004, "manaPaymentOption": {"mana": [{"manaId": 237, "color": "ManaColor_Red",
    "srcInstanceId": 318, "specs": [{"type": "ManaSpecType_Predictive"}], "abilityGrpId": 1004, "count": 1}]}}]}]}},
 "allowCancel": "AllowCancel_Continue"}"""
)

# Player.log 10:38:28, GREMessageType_PayCostsReq msgId 318: the same shape, for Burst Lightning #332.
PAY_COSTS_318 = json.loads(
    json.dumps(PAY_COSTS_244)
    .replace('"objectId": 319', '"objectId": 332')
    .replace('"manaId": 236', '"manaId": 342')
    .replace('"manaId": 237', '"manaId": 343')
)
PAY_COSTS_318.update({"msgId": 318, "gameStateId": 233})


@pytest.fixture(autouse=True)
def _ability_texts(monkeypatch):
    """The card database's ability texts, without opening the database."""
    monkeypatch.setattr(
        ward, "_ability_text", lambda ability_id: ABILITY_TEXT.get(int(ability_id)), raising=False
    )
    monkeypatch.setattr(ward, "_ABILITY_WARDS", {}, raising=False)
    monkeypatch.setattr(ward, "_UNREAD_UNTIL", {}, raising=False)


def _land(iid, name, seat, tapped=False):
    symbol = {"Mountain": "R", "Plains": "W"}[name]
    return {
        "instance_id": iid,
        "name": name,
        "type_line": f"Basic Land — {name}",
        "oracle_text": f"({{T}}: Add {{{symbol}}}.)",
        "card_types": ["CardType_Land"],
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
        "turn_entered_battlefield": 1,
    }


def _spider(iid, power, toughness, *, tapped, abilities, entered):
    return {
        "instance_id": iid,
        "grp_id": 97964,
        "name": "Skyward Spider",
        "type_line": "Creature — Spider Human Hero",
        "oracle_text": SPIDER_TEXT,
        "mana_cost": "{W/U}{W/U}",
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "is_tapped": tapped,
        "keywords": ["flying", "lifelink"] if 12 in abilities else [],
        "ability_grp_ids": list(abilities),
        "controller_seat_id": OPP,
        "owner_seat_id": OPP,
        "turn_entered_battlefield": entered,
    }


AURA = {
    "instance_id": 297,
    "grp_id": 92090,
    "name": "Sheltered by Ghosts",
    "type_line": "Enchantment — Aura",
    "oracle_text": AURA_TEXT,
    "mana_cost": "{1}{W}",
    "card_types": ["CardType_Enchantment"],
    "controller_seat_id": OPP,
    "owner_seat_id": OPP,
    "attached_to_id": 287,
}
SPIDER_TOKEN = {
    "instance_id": 316,
    "name": "Spider",
    "type_line": "Token Creature — Spider",
    "oracle_text": "Menace, reach",
    "card_types": ["CardType_Creature"],
    "power": 2,
    "toughness": 1,
    "keywords": ["menace", "reach"],
    "object_kind": "TOKEN",
    "controller_seat_id": OPP,
    "owner_seat_id": OPP,
    "turn_entered_battlefield": 7,
}


def _ward_trigger(iid, parent):
    """A ward trigger as the snapshot serializes it (the ability's own text from the card database)."""
    return {
        "instance_id": iid,
        "grp_id": 141939,
        "name": "Ability (ID: 141939)",
        "type_line": "Ability",
        "oracle_text": "Ward {o2}",
        "object_kind": "ABILITY",
        "controller_seat_id": OPP,
        "owner_seat_id": OPP,
        "parent_instance_id": parent,
    }


def _our_spell(iid, name, text, target):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Instant" if name == "Burst Lightning" else "Sorcery",
        "oracle_text": text,
        "mana_cost": "{R}",
        "object_kind": "CARD",
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "targeting": [target],
    }


def _snapshot(turn, battlefield, stack, pay_costs, *, life=(12, 22)):
    log = GameState()
    _handle_pay_costs(log, pay_costs)
    return {
        "match_id": "a9cd5a2e",
        "local_seat_id": LOCAL,
        "opponent_seat_id": OPP,
        "turn": {
            "turn_number": turn,
            "active_player": LOCAL,
            "priority_player": LOCAL,
            "phase": "Phase_Main1",
        },
        "players": [
            {"seat_id": LOCAL, "life_total": life[0], "is_local": True},
            {"seat_id": OPP, "life_total": life[1], "is_local": False},
        ],
        "battlefield": battlefield,
        "hand": [],
        "graveyard": [],
        "stack": stack,
        "pending_decision": "Pay Costs",
        "decision_context": dict(log.decision_context),
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "PayCostsReq",
        "_bridge_request_class": "PayCostsRequest",
        "_bridge_can_cancel": True,
    }


def _theirs(spider_287_tapped=True):
    return [
        _land(279, "Plains", OPP, True),
        _land(286, "Plains", OPP, True),
        _land(296, "Plains", OPP),
        _spider(287, 4, 3, tapped=spider_287_tapped, abilities=SPIDER_287_ABILITIES, entered=3),
        dict(AURA),
        dict(SPIDER_TOKEN),
    ]


def _t8_state(extra_mountains=0, triggers=(320, 321)):
    """10:36:53: the Joke paid with Mountain 281; ward triggers #320 and #321 on the stack."""
    ours = [_land(281, "Mountain", LOCAL, True), _land(291, "Mountain", LOCAL), _land(318, "Mountain", LOCAL)]
    ours += [_land(900 + n, "Mountain", LOCAL) for n in range(extra_mountains)]
    # Top-first, as Player.log listed it at gsid 177: [321, 320, 319] (ledger #115).
    stack = [
        *(_ward_trigger(t, 287) for t in sorted(triggers, reverse=True)),
        _our_spell(319, "Impractical Joke", JOKE_TEXT, 287),
    ]
    return _snapshot(8, ours + _theirs(), stack, PAY_COSTS_244)


def _t10_state():
    """10:38:28: Burst Lightning #332 on the stack, Skyward Spider #328's ward trigger #333 above it."""
    ours = [_land(281, "Mountain", LOCAL, True), _land(291, "Mountain", LOCAL), _land(318, "Mountain", LOCAL)]
    theirs = [
        *_theirs(),
        _spider(328, 2, 2, tapped=False, abilities=[141939, 172428], entered=9),
    ]
    theirs[3].update(power=5, toughness=4)  # the +1/+1 counter from Origin of Spider-Man (T9)
    stack = [
        _ward_trigger(333, 328),
        _our_spell(332, "Burst Lightning", BURST_TEXT, 328),
    ]  # gsid 231: [333, 332]
    return _snapshot(10, ours + theirs, stack, PAY_COSTS_318, life=(5, 27))


class _RefusingPlanner:
    """The pay/decline model call must not decide a ward tax."""

    def plan_pay_or_decline(self, name, oracle, game_state):
        raise AssertionError(f"asked the model about a ward tax ({name})")


# --- #50: a ward tax on our own spell ------------------------------------------------


def test_the_ward_trigger_is_found_from_our_own_spell_id():
    state = _t10_state()
    assert state["decision_context"]["source_id"] == 332  # the PayCostsReq names Burst Lightning
    payment = ward.pending_ward_payment(state, 332)
    assert payment is not None
    assert [t.entry["instance_id"] for t in payment.triggers] == [333]
    assert payment.permanent["instance_id"] == 328
    assert payment.ward.mana == 2
    assert payment.describe() == "the ward of Skyward Spider (Ward {2}), or Burst Lightning is countered"


def test_ward_tax_on_burst_lightning_is_paid_without_asking_the_model():
    engine = make_engine(planner=_RefusingPlanner())
    assert engine._should_decline_optional_cost(_t10_state()) is None
    # A stale second trigger from the one-ward Spider must not make {2} look like {4}.
    state = _t10_state()
    state["stack"].append(_ward_trigger(334, 328))
    assert engine._should_decline_optional_cost(state) is None


class _PayCostsBridge:
    connected = True

    def __init__(self, msg_id):
        self.msg_id = msg_id
        self.auto_tap_calls = 0
        self.cancel_calls = 0
        self.payments = []

    def connect(self):
        return True

    def get_pending_actions(self):
        return {
            "ok": True,
            "has_pending": True,
            "request_type": "PayCostsReq",
            "request_class": "PayCostsRequest",
            "can_cancel": True,
            "game_state_id": 1,
            "msg_id": self.msg_id,
        }

    def submit_auto_tap(self, solution_index=0):
        self.auto_tap_calls += 1
        return True

    def submit_payment_action(self, instance_id, ability_grp_id, color):
        self.payments.append((instance_id, color))
        return True

    def cancel_action(self):
        self.cancel_calls += 1
        return True


def _engine(monkeypatch, bridge, spell):
    import arenamcp.autopilot as autopilot_module
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    monkeypatch.setattr(autopilot_module.time, "sleep", lambda *_: None)
    engine = AutopilotEngine(planner=_RefusingPlanner(), config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = bridge
    engine._pause_for_manual = Mock()
    engine._try_typed_decision_path = lambda game_state, trigger: None  # PayCosts is not a typed family
    engine._recent_bot_submissions.append((time.monotonic(), spell))  # the typed path cast it
    return engine


def test_autopilot_pays_the_ward_tax_on_burst_lightning(monkeypatch):
    bridge = _PayCostsBridge(318)
    engine = _engine(monkeypatch, bridge, "burst lightning")
    assert engine.process_trigger(_t10_state(), "decision_required") is True
    assert (bridge.auto_tap_calls, bridge.cancel_calls) == (1, 0)
    engine._pause_for_manual.assert_not_called()


def test_pay_prompt_names_the_amount_and_the_ward():
    from arenamcp.action_planner import ActionPlanner

    prompts = []

    class _Backend:
        def complete(self, system, user, *args, **kwargs):
            prompts.append(user)
            return '{"pay": true, "reasoning": "keep Burst"}'

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._timeout = 1.0
    planner._backend = _Backend()
    assert planner.plan_pay_or_decline("Burst Lightning", BURST_TEXT, _t10_state()) is True
    assert "Cost to pay now: {2} (you have 2 mana to pay it with)" in prompts[0]
    assert (
        "This is a WARD TAX, not an optional extra: pay the ward of Skyward Spider (Ward {2}), "
        "or Burst Lightning is countered." in prompts[0]
    )


def test_typed_cast_counts_as_our_own_play_for_its_cost():
    from arenamcp.decisions import DecisionOption

    state = _t10_state()
    state["stack"] = state["stack"][:1]  # no ward: Burst Lightning's own cost
    engine = make_engine(planner=_RefusingPlanner())
    burst = DecisionOption(
        "idx:0",
        "Cast Burst Lightning",
        meta={"actionType": "ActionType_Cast", "instanceId": 160, "grpId": 93905},
    )
    engine._note_typed_play(state, burst)
    assert engine._should_decline_optional_cost(state) is None
    # Someone else's trigger asking right after our cast is still gated.
    state["decision_context"]["source_card"] = "Go-Shintai of Hidden Cruelty"
    assert not engine._paying_for_own_play(state)


# --- #51: ward instances add up ---------------------------------------------------------


def test_gre_ability_list_counts_both_wards():
    log = GameState()
    log._update_game_object(
        {
            "instanceId": 287,
            "grpId": 97964,
            "type": "GameObjectType_Card",
            "zoneId": 28,
            "ownerSeatId": 2,
            "controllerSeatId": 2,
            "cardTypes": ["CardType_Creature"],
            "power": {"value": 4},
            "toughness": {"value": 3},
            "abilityOriginalCardGrpIds": [97964, 92090, 92090],
            "uniqueAbilities": [
                {"id": 223, "grpId": 141939},
                {"id": 224, "grpId": 172428},
                {"id": 236, "grpId": 8},
                {"id": 237, "grpId": 12},
                {"id": 238, "grpId": 141939},
            ],
        }
    )
    snapshot = log.game_objects[287].to_dict()
    assert snapshot["ability_grp_ids"] == SPIDER_287_ABILITIES
    spider = {**snapshot, "oracle_text": SPIDER_TEXT, "name": "Skyward Spider"}
    total = ward.ward_of(spider)
    assert (total.mana, total.count) == (4, 2)
    assert total.label == "Ward {2} twice (4 mana in all)"


def test_aura_grant_adds_to_the_printed_ward_without_gre_abilities():
    spider = _spider(287, 4, 3, tapped=True, abilities=[], entered=3)
    assert ward.ward_of(spider).mana == 2
    assert ward.ward_of(spider, [AURA]).mana == 4
    assert ward.ward_of(AURA) is None  # the Aura grants ward; it has none itself
    state = _t8_state()
    for card in state["battlefield"]:
        card.pop("ability_grp_ids", None)
    [(card, total)] = ward.opposing_wards(state, {"creature"})
    assert card["instance_id"] == 287 and total.mana == 4


def test_unreadable_ability_texts_fall_back_to_the_card_text(monkeypatch):
    asked = []
    monkeypatch.setattr(
        ward, "_ability_text", lambda ability_id: asked.append(ability_id)
    )  # database not ready
    spider = _spider(287, 4, 3, tapped=True, abilities=SPIDER_287_ABILITIES, entered=3)
    assert ward.ward_of(spider, [AURA]).mana == 4
    assert ward.ward_of(spider).mana == 2
    assert asked == sorted(
        set(SPIDER_287_ABILITIES), key=SPIDER_287_ABILITIES.index
    )  # each asked once for now


def test_joke_cast_note_counts_both_wards():
    state = _t8_state()  # before the cast (10:36:43): all three Mountains untapped
    state["stack"] = []
    state["battlefield"][0]["is_tapped"] = False
    state["hand"] = [
        {
            "instance_id": 162,
            "name": "Impractical Joke",
            "type_line": "Sorcery",
            "oracle_text": JOKE_TEXT,
            "mana_cost": "{R}",
            "controller_seat_id": LOCAL,
            "owner_seat_id": LOCAL,
        }
    ]
    note = ward.ward_cast_note(
        state, {"actionType": "ActionType_Cast", "instanceId": 162, "manaCost": [{"count": 1}]}
    )
    assert (
        "Skyward Spider has Ward {2} twice (4 mana in all): after paying for this you'd have 2 untapped mana"
        in note
    )
    assert "so targeting it gets the whole effect countered" in note


def test_first_ward_payment_is_declined_when_the_second_cannot_be_paid(monkeypatch):
    state = _t8_state()
    engine = make_engine(planner=_RefusingPlanner())
    reason = engine._should_decline_optional_cost(state)
    assert reason == (
        "unpayable ward tax: the ward of Skyward Spider (Ward {2} twice (4 mana in all)), "
        "or Impractical Joke is countered, and only 2 mana is available"
    )
    bridge = _PayCostsBridge(244)
    engine = _engine(monkeypatch, bridge, "impractical joke")
    assert engine.process_trigger(_t8_state(), "decision_required") is True
    assert (bridge.auto_tap_calls, bridge.cancel_calls) == (0, 1)  # the two Mountains stay untapped


def test_both_wards_are_paid_when_the_mana_covers_them():
    engine = make_engine(planner=_RefusingPlanner())
    assert engine._should_decline_optional_cost(_t8_state(extra_mountains=2)) is None
    # The second trigger alone, after the first {2} was paid.
    assert engine._should_decline_optional_cost(_t8_state(triggers=(320,))) is None


def _t8_priority_board():
    """10:36:42: our three Mountains untapped, Joke x2, Burst Lightning and Emberheart Challenger in hand."""
    battlefield = [
        _land(281, "Mountain", LOCAL),
        _land(291, "Mountain", LOCAL),
        _land(318, "Mountain", LOCAL),
    ]
    battlefield += _theirs()
    for card in battlefield:
        card.pop("ability_grp_ids", None)  # the text path: printed ward + Sheltered by Ghosts' grant

    def hand_card(iid, name, text, cost, types, **extra):
        return {
            "instance_id": iid,
            "name": name,
            "oracle_text": text,
            "mana_cost": cost,
            "type_line": types,
            "card_types": ["CardType_" + types.split()[0]],
            "controller_seat_id": LOCAL,
            "owner_seat_id": LOCAL,
            **extra,
        }

    return {
        "match_id": "a9cd5a2e",
        "local_seat_id": LOCAL,
        "opponent_seat_id": OPP,
        "turn": {"turn_number": 8, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1",
                 "step": ""},
        "players": [
            {"seat_id": LOCAL, "life_total": 12, "is_local": True, "lands_played": 1},
            {"seat_id": OPP, "life_total": 22, "is_local": False, "lands_played": 0},
        ],
        "battlefield": battlefield,
        "hand": [
            hand_card(162, "Impractical Joke", JOKE_TEXT, "{R}", "Sorcery"),
            hand_card(161, "Impractical Joke", JOKE_TEXT, "{R}", "Sorcery"),
            hand_card(160, "Burst Lightning", BURST_TEXT, "{R}", "Instant"),
            hand_card(304, "Emberheart Challenger", EMBERHEART_TEXT, "{1}{R}", "Creature — Mouse Warrior",
                      power=2, toughness=2, keywords=["haste"]),
        ],
        "graveyard": [],
        "stack": [],
        "zones": {"library_count": 49, "library_count_source": "log_zone_membership", "opponent_hand_count": 3},
        "deck_cards": [],
    }  # fmt: skip


def test_board_math_line_no_longer_aims_the_joke_into_four_ward():
    from arenamcp import board_assessment as ba
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search import search_lines

    model = build_board_model(deepcopy(_t8_priority_board()))
    spider = next(body for body in model.theirs if body["instance_id"] == 287)
    assert ba.ward_cost(spider) == 4
    best = search_lines(model, survival=False, lethal_now=False, hard_ms=1e9).best
    aimed = [target for step in best.steps for target in step.targets]
    assert ("Impractical Joke", "Skyward Spider") not in aimed, best.summary()
    # Without the Aura the Spider's single {2} is payable and the Joke kills it.
    board = _t8_priority_board()
    board["battlefield"] = [card for card in board["battlefield"] if card["instance_id"] != 297]
    best = search_lines(build_board_model(board), survival=False, lethal_now=False, hard_ms=1e9).best
    assert ("Impractical Joke", "Skyward Spider") in [t for step in best.steps for t in step.targets]


def test_ward_payable_checks_every_part():
    two = ward.combine_wards([ward.Ward("{2}", mana=2), ward.Ward("Pay 2 life", life=2)])
    state = {"local_seat_id": LOCAL, "players": [{"seat_id": LOCAL, "life_total": 3}]}
    assert two.label == "Ward {2} and Ward—Pay 2 life (2 mana in all)"
    assert ward.ward_payable(two, state, 2) is True
    assert ward.ward_payable(two, state, 1) is False
    state["players"][0]["life_total"] = 2
    assert ward.ward_payable(two, state, 5) is False
    sacrifice = ward.combine_wards([ward.Ward("{1}", mana=1), ward.Ward("Sacrifice a permanent")])
    assert ward.ward_payable(sacrifice, state, 1) is None
    assert ward.ward_payable(sacrifice, state, 0) is False
