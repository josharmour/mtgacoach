"""Necromancer loop and Stingerquill redirect, bug_20261006_185803 (2026-10-06, turns 14-16).

standalone.log / Player.log:

* 18:56:06 Stingerquill Charm (395) was cast with mode 1, ability 70361 "deals 3
  damage to any target". The SelectTargetsReq carried targetingAbilityGrpId
  70361. The model picked Greenhouse Propagator (389, opponent's 2/3), but the
  whole card text also contains "Target creature gains first strike and
  deathtouch". The classifier missed "any target", read the card as beneficial,
  and logged "Overriding beneficial LLM target pick ['tgt:389'] (opponent
  permanent) with ['tgt:266']". The 3 damage killed our own Yuriko, Hope from
  the Shadows.
* 18:57:01-18:57:14 Theoretical Necromancer's graveyard ability (173944, source
  455, targetSourceZoneId 37 = our graveyard) "Return another target creature
  card from your graveyard to your hand" read as harmful through the bounce
  rule. Every pick of our Yuriko (399) was replaced with a cancel, the cancel
  un-counted the activation, and the next priority window activated it again.
  This happened four times until the user force-stopped the autopilot.
"""

import time
from unittest.mock import MagicMock

import arenamcp.action_planner as action_planner
import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import build_pending_decision
from arenamcp.target_effects import effect_mentions_harm, target_effect_is_harmful

LOCAL, OPP = 2, 1

# Arena card DB texts (Raw_CardDatabase, 2026-10-06).
NECROMANCER = (
    "{o3oB}, Exile this card from your graveyard: Return another target creature card from your "
    "graveyard to your hand."
)
CHARM = (
    "Choose one — \n•Stingerquill Charm deals 3 damage to any target. \n•Target creature gains first "
    "strike and deathtouch until end of turn. \n•Create a 2/2 colorless Wizard Soldier creature token "
    "named Cadet. It gains haste until end of turn."
)
ABILITIES = {
    173944: NECROMANCER,
    70361: "CARDNAME deals 3 damage to any target.",
    208260: "Target creature gains first strike and deathtouch until end of turn.",
    208261: "Create a 2/2 colorless Wizard Soldier creature token named Cadet. It gains haste until end of turn.",
}


def _card(iid, name, seat, power=None, toughness=None, oracle="", type_line="Creature"):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def _players():
    return [
        {"seat_id": OPP, "life_total": 23, "is_local": False},
        {"seat_id": LOCAL, "life_total": 10, "is_local": True},
    ]


class _Backend:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.reply


def _planner(reply):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._timeout = 1.0
    planner._backend = _Backend(reply)
    return planner


def _targets_poll(ids, *, game_state_id, msg_id, hot=()):
    """The mac bridge's SelectTargetsRequest shape (mac_bridge_adapter)."""
    slot = [
        {
            "targetInstanceId": iid,
            "targetIdx": 1,
            "grpId": 0,
            "legalAction": "Select",
            "highlight": "Hot" if iid in hot else "Tepid",
        }
        for iid in ids
    ]
    return {
        "has_pending": True,
        "request_type": "SelectTargets",
        "request_class": "SelectTargetsRequest",
        "game_state_id": game_state_id,
        "msg_id": msg_id,
        "can_cancel": True,
        "target_selections": [
            {"targetIdx": 1, "minTargets": 1, "maxTargets": 1, "selectedTargets": 0, "targets": slot}
        ],
        "target_candidates": slot,
    }


def _log_request(ids, ability_id, source_id, card_grp, zone=None):
    """selectTargetsReq exactly as Player.log records it."""
    selection = {
        "targetIdx": 1,
        "targets": [
            {
                "targetInstanceId": iid,
                "legalAction": "SelectAction_Select",
                "highlight": "HighlightType_Tepid",
            }
            for iid in ids
        ],
        "minTargets": 1,
        "maxTargets": 1,
        "prompt": {"promptId": 1031, "parameters": [{"parameterName": "CardId", "numberValue": card_grp}]},
        "targetingAbilityGrpId": ability_id,
        "targetingPlayer": LOCAL,
    }
    if zone:
        selection["targetSourceZoneId"] = zone
    return {"targets": [selection], "sourceId": source_id, "abilityGrpId": card_grp}


def _known_abilities(monkeypatch):
    monkeypatch.setattr(action_planner, "_ability_rules_text", lambda aid: ABILITIES.get(int(aid), ""))


def _no_ability_db(monkeypatch):
    monkeypatch.setattr(action_planner, "_ability_rules_text", lambda aid: "")


# --- classification -----------------------------------------------------------


def test_returning_a_card_from_our_graveyard_is_beneficial():
    assert target_effect_is_harmful(NECROMANCER) is False
    assert (
        target_effect_is_harmful("Return target creature card from your graveyard to the battlefield.")
        is False
    )
    assert (
        target_effect_is_harmful("Return up to two target creature cards from your graveyard to your hand.")
        is False
    )
    assert (
        target_effect_is_harmful(
            "Put target creature card from a graveyard onto the battlefield under your control."
        )
        is False
    )
    # The bounce is still harmful, and so is graveyard hate.
    assert target_effect_is_harmful("Return target creature to its owner's hand.") is True
    assert target_effect_is_harmful("Exile target card from a graveyard.") is True


def test_damage_to_any_target_is_harmful_and_the_charm_is_mixed():
    assert target_effect_is_harmful(ABILITIES[70361]) is True
    assert target_effect_is_harmful("Lightning Bolt deals 3 damage to any target.") is True
    assert target_effect_is_harmful(ABILITIES[208260]) is False
    # Mode 1 (burn) and mode 2 (deathtouch) together: unknown, never "beneficial".
    assert target_effect_is_harmful(CHARM) is None


def test_harm_wording_veto_ignores_costs_and_recursion():
    assert effect_mentions_harm(CHARM)
    assert effect_mentions_harm(
        "Choose one —\n•Target creature gains first strike.\n•Deal 2 damage divided "
        "as you choose among one or two targets."
    )
    assert not effect_mentions_harm(NECROMANCER)  # "Exile this card" is the cost
    assert not effect_mentions_harm(ABILITIES[208260])
    assert not effect_mentions_harm("Put a +1/+1 counter on target creature.")


# --- 18:57:01 Theoretical Necromancer -----------------------------------------------

NECRO_IDS = (271, 399, 408, 449)


def _necro_state(*, bridge=True):
    request = _log_request(NECRO_IDS, 173944, 455, 106298, zone=37)
    state = {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 16, "active_player": LOCAL, "phase": "Phase_Main1"},
        "players": _players(),
        "battlefield": [
            _card(351, "Geist of Saint Thalia", LOCAL, 1, 2),
            _card(451, "Mindseeker Oculus", LOCAL, 2, 1),
            _card(382, "Yuriko, Blade of the Mighty", OPP, 4, 5),
            _card(389, "Greenhouse Propagator", OPP, 2, 3),
            _card(437, "Tomik, Orzhov Lawmage", OPP, 2, 1),
        ],
        "graveyard": [
            _card(271, "Diviner of Victory", LOCAL, 1, 1),
            _card(378, "Theoretical Necromancer", LOCAL, 4, 1, NECROMANCER),
            _card(399, "Yuriko, Hope from the Shadows", LOCAL, 1, 1),
            _card(408, "Living Library", LOCAL, 0, 4),
            _card(449, "Codie, Ravenous Codex", LOCAL, 1, 4),
            _card(272, "Teyo, Lightshield Expert", OPP, 1, 1),
        ],
        "hand": [],
        "stack": [
            {
                "instance_id": 455,
                "grp_id": 173944,
                "name": "Theoretical Necromancer ability",
                "type_line": "Ability",
                "object_kind": "ABILITY",
                "oracle_text": NECROMANCER,
                "controller_seat_id": LOCAL,
                "parent_instance_id": 378,
            }
        ],
        "decision_context": {
            "type": "target_selection",
            "source_card": "Theoretical Necromancer",
            "source_id": 455,
            "targets": request["targets"],
            "raw": request,
            "source_parent_instance_id": 378,
            "source_card_oracle_text": NECROMANCER,
            "source_oracle_text": NECROMANCER,
        },
    }
    if bridge:
        # The bridge payload from the bug report (GRE_RequestPayload).
        state["_bridge_request_payload"] = {
            "requestType": "SelectTargets",
            "requestClass": "SelectTargetsRequest",
            "targetSelections": [
                {
                    "targetIdx": 1,
                    "targets": ["{}", "{}", "{}", "{}"],
                    "minTargets": 1,
                    "maxTargets": 1,
                    "selectedTargets": 0,
                    "targetSourceZoneId": 37,
                    "targetingAbilityGrpId": 173944,
                    "targetingPlayer": LOCAL,
                }
            ],
            "abilityGrpId": 173944,
            "sourceId": 455,
        }
    return state


def _necro_decision():
    names = {
        271: "Diviner of Victory",
        399: "Yuriko, Hope from the Shadows",
        408: "Living Library",
        449: "Codie",
    }
    return build_pending_decision(
        _targets_poll(NECRO_IDS, game_state_id=460, msg_id=648),
        resolve_instance=lambda iid: names.get(iid, ""),
    )


def test_necromancer_returns_our_yuriko_as_the_model_chose(monkeypatch):
    _known_abilities(monkeypatch)
    reply = '{"option_ids": ["tgt:399"], "reasoning": "Recasting Yuriko for {U} blanks their 4/5 Blade"}'
    assert _planner(reply).plan_decision_options(_necro_decision(), _necro_state()) == ["tgt:399"]


def test_necromancer_pick_stands_from_the_card_text_alone(monkeypatch):
    # No ability DB and no bridge: the log's card text decides, and it reads beneficial.
    _no_ability_db(monkeypatch)
    reply = '{"option_ids": ["tgt:399"], "reasoning": "get Yuriko back"}'
    assert _planner(reply).plan_decision_options(_necro_decision(), _necro_state(bridge=False)) == ["tgt:399"]


def test_coach_no_longer_withholds_our_graveyard_cards(monkeypatch):
    _known_abilities(monkeypatch)
    legal = [
        "Select target: Diviner of Victory (YOURS)",
        "Select target: Yuriko, Hope from the Shadows (YOURS)",
        "Select target: Living Library (YOURS)",
        "Select target: Codie, Ravenous Codex (YOURS)",
    ]
    planner = _planner("{}")
    assert planner._filter_legal_actions_for_planning(_necro_state(), legal) == legal


# --- 18:56:06 Stingerquill Charm, mode 1 ------------------------------------------

CHARM_IDS = (1, 2, 266, 347, 351, 358, 372, 382, 389)
OURS = {2, 266, 347, 351, 372}


def _charm_state(*, mode=70361, bridge=True):
    request = _log_request(CHARM_IDS, mode, 395, 106390) if mode else _log_request(CHARM_IDS, 0, 395, 106390)
    if not mode:
        del request["targets"][0]["targetingAbilityGrpId"]
    state = {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 14, "active_player": LOCAL, "phase": "Phase_Main1"},
        "players": _players(),
        "battlefield": [
            _card(266, "Yuriko, Hope from the Shadows", LOCAL, 1, 1),
            _card(347, "Codie, Ravenous Codex", LOCAL, 1, 4),
            _card(351, "Geist of Saint Thalia", LOCAL, 1, 2),
            _card(372, "Living Library", LOCAL, 0, 4),
            _card(358, "Traxos, Scourge Eternal", OPP, 5, 4),
            _card(382, "Yuriko, Blade of the Mighty", OPP, 4, 5),
            _card(389, "Greenhouse Propagator", OPP, 2, 3),
        ],
        "graveyard": [],
        "hand": [],
        "stack": [
            {
                "instance_id": 395,
                "grp_id": 106390,
                "name": "Stingerquill Charm",
                "type_line": "Instant",
                "object_kind": "CARD",
                "oracle_text": CHARM,
                "controller_seat_id": LOCAL,
            }
        ],
        "decision_context": {
            "type": "target_selection",
            "source_card": "Stingerquill Charm",
            "source_id": 395,
            "targets": request["targets"],
            "raw": request,
            "source_oracle_text": CHARM,
        },
    }
    if bridge:
        selection = {
            "targetIdx": 1,
            "minTargets": 1,
            "maxTargets": 1,
            "selectedTargets": 0,
            "targetingPlayer": 2,
        }
        if mode:
            selection["targetingAbilityGrpId"] = mode
        state["_bridge_request_payload"] = {
            "targetSelections": [selection],
            "abilityGrpId": 106390,
            "sourceId": 395,
        }
    return state


def _charm_decision():
    return build_pending_decision(
        _targets_poll(CHARM_IDS, game_state_id=387, msg_id=549, hot=(1, 358, 382, 389)),
        resolve_instance=lambda iid: "",
    )


PROPAGATOR = (
    '{"option_ids": ["tgt:389"], "reasoning": "3 damage cleanly kills the 2/3 Greenhouse Propagator"}'
)


def test_charm_damage_mode_keeps_the_pick_on_their_creature(monkeypatch):
    _known_abilities(monkeypatch)
    planner = _planner(PROPAGATOR)
    assert planner.plan_decision_options(_charm_decision(), _charm_state()) == ["tgt:389"]
    # The model is shown the chosen mode, not the deathtouch mode.
    effect = next(
        line for line in planner._backend.prompts[0].splitlines() if line.startswith("SOURCE EFFECT")
    )
    assert "deals 3 damage to any target" in effect and "deathtouch" not in effect


def test_charm_mode_is_read_from_the_log_request_without_the_bridge(monkeypatch):
    _known_abilities(monkeypatch)
    assert _planner(PROPAGATOR).plan_decision_options(_charm_decision(), _charm_state(bridge=False)) == [
        "tgt:389"
    ]


def test_charm_with_unknown_mode_never_moves_damage_onto_our_creature(monkeypatch):
    _no_ability_db(monkeypatch)
    chosen = _planner(PROPAGATOR).plan_decision_options(_charm_decision(), _charm_state(mode=0))
    assert chosen == [DECLINE_DECISION]  # mixed text, no stated intent: decline, never redirect
    intent = (
        '{"option_ids": ["tgt:389"], "target_controllers": {"tgt:389": "opponent"}, '
        '"reasoning": "kill the Propagator"}'
    )
    assert _planner(intent).plan_decision_options(_charm_decision(), _charm_state(mode=0)) == ["tgt:389"]


def test_misread_beneficial_charm_keeps_the_models_enemy_target(monkeypatch):
    # The 18:56:09 classification (False) must not move the damage to tgt:266.
    _no_ability_db(monkeypatch)
    monkeypatch.setattr(action_planner, "target_effect_is_harmful", lambda text: False)
    assert _planner(PROPAGATOR).plan_decision_options(_charm_decision(), _charm_state(mode=0)) == ["tgt:389"]


def test_blind_fallback_never_aims_a_damage_text_at_our_board(monkeypatch):
    _no_ability_db(monkeypatch)
    monkeypatch.setattr(action_planner, "target_effect_is_harmful", lambda text: False)
    chosen = _planner("no json here").plan_decision_options(_charm_decision(), _charm_state(mode=0))
    assert chosen == [DECLINE_DECISION]
    # With the mode known the fallback aims at an opposing target.
    monkeypatch.undo()
    _known_abilities(monkeypatch)
    chosen = _planner("no json here").plan_decision_options(_charm_decision(), _charm_state())
    assert chosen and not {int(oid[4:]) for oid in chosen} & OURS


# --- the activate/cancel loop -------------------------------------------------------


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _actions_poll(game_state_id=459):
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": game_state_id,
        "can_pass": True,
        "actions": [
            {
                "actionType": "ActionType_Activate",
                "grpId": 106298,
                "instanceId": 378,
                "abilityGrpId": 173944,
                "manaCost": [{"color": '[ "Generic" ]', "count": 3}, {"color": '[ "Black" ]', "count": 1}],
                "hasAutoTap": True,
                "autoTapActions": [{"instanceId": 367}, {"instanceId": 400}],
            },
            {"actionType": "ActionType_Pass"},
        ],
    }


def _engine(monkeypatch):
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    planner = MagicMock()
    # The live loop: decline the targets, then take the activation again.
    planner.plan_decision_options.side_effect = lambda decision, gs: (
        [DECLINE_DECISION]
        if decision.request_type == "SelectTargets"
        else [o.option_id for o in decision.options if o.option_id != "pass"][:1] or ["pass"]
    )
    engine = AutopilotEngine(
        planner=planner, get_game_state=lambda: {}, config=AutopilotConfig(dry_run=False)
    )
    bridge = MagicMock()
    bridge.connected = True
    engine._gre_bridge = bridge
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False
    return engine, bridge, planner


def _loop_state(turn=16):
    state = _necro_state(bridge=False)
    state["turn"] = {"turn_number": turn, "active_player": LOCAL, "phase": "Phase_Main1"}
    return state


def test_self_cancelled_activation_is_withheld_for_the_rest_of_the_turn(monkeypatch):
    engine, bridge, planner = _engine(monkeypatch)
    state = _loop_state()

    bridge.get_pending_actions.return_value = _actions_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    assert bridge.submit_action_by_index.call_count == 1

    bridge.get_pending_actions.return_value = _targets_poll(NECRO_IDS, game_state_id=460, msg_id=648)
    assert engine._try_typed_decision_path(state, "decision_required")
    bridge.cancel_action.assert_called_once()

    # 18:57:04 re-offered the activation; now only Pass reaches the planner.
    bridge.get_pending_actions.return_value = _actions_poll(461)
    assert engine._try_typed_decision_path(state, "decision_required")
    assert bridge.submit_action_by_index.call_count == 1
    offered = planner.plan_decision_options.call_args.args[0]
    assert [o.option_id for o in offered.options] == ["pass"]
    bridge.submit_pass.assert_called_once()

    # The legacy planner path hides it too, but not other plays.
    legal = ["Activate Ability: Theoretical Necromancer [OK]", "Cast Codie, Ravenous Codex", "Pass"]
    assert engine._drop_exhausted_activations(legal, state) == legal[1:]

    # A new turn offers it again.
    bridge.get_pending_actions.return_value = _actions_poll(500)
    assert engine._try_typed_decision_path(_loop_state(turn=18), "decision_required")
    assert bridge.submit_action_by_index.call_count == 2


def test_self_cancel_guard_is_per_ability(monkeypatch):
    engine, _, _ = _engine(monkeypatch)
    state = _loop_state()
    engine._self_cancelled_plays = {(16, "ActionType_Activate", 378, 173944, "theoretical necromancer")}
    assert engine._self_cancel_withheld(state, "ActionType_Activate", 378, 173944)
    assert not engine._self_cancel_withheld(state, "ActionType_Activate", 378, 208425)  # another ability
    assert not engine._self_cancel_withheld(state, "ActionType_Cast", 378, 0, "theoretical necromancer")
    assert not engine._self_cancel_withheld(_loop_state(turn=18), "ActionType_Activate", 378, 173944)


def test_a_cancel_long_after_the_play_is_not_attributed(monkeypatch):
    engine, _, _ = _engine(monkeypatch)
    state = _loop_state()
    stale = time.monotonic() - 60
    engine._last_typed_play = (stale, 16, "ActionType_Activate", 378, 173944, "theoretical necromancer")
    engine._withhold_after_self_cancel(state, "SelectTargets cancelled")
    assert not engine._self_cancel_withheld(state, "ActionType_Activate", 378, 173944)
