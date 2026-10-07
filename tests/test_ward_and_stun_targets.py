"""Ward and stun targeting, from bug_20261006_174855 (2026-10-06 17:48, FRA draft, turn 8).

Player.log: at 17:47:57 we had three untapped lands (Swamp 223, Islands 230
and 251) with Island 262 still in hand. The autopilot cast Seasoned Cryomancer
(idx:4) with all three, discarded two nonlands, and its reflexive trigger
(274) offered Unflinching Hortimancer (226, opponent, Ward {1}) and our own
Cryomancer (263) as "up to two" targets (minTargets 0, AllowCancel_No). The
planner picked 226 but declined it as "unclassified" -> MANUAL REQUIRED; the
user picked it by hand, the ward trigger (275) resolved with no mana to pay,
and the stun was countered. The Island was played afterwards.
"""

from dataclasses import replace
from types import SimpleNamespace

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot, submit_option
from arenamcp.gre_bridge import GREBridge
from arenamcp.target_effects import target_effect_is_harmful
from arenamcp.ward import untapped_land_drop, ward_cast_note, ward_of, ward_of_text

LOCAL, OPP = 2, 1
CRYOMANCER = (
    "When this creature enters, draw two cards, then discard two cards. When you discard one or more "
    "nonland cards this way, tap up to that many target creatures and put a stun counter on each of them.\n"
    "{o3oUoU}, Exile this card from your graveyard: Draw two cards."
)
HORTIMANCER = (
    "Ward {o1}\nWhenever you gain life, put a +1/+1 counter on this creature.\n"
    "Whenever you gain life, put a <nobr>+1/+1</nobr> counter on this creature."
)


def _land(iid, name, seat, tapped, oracle=None):
    basic = {"Swamp": "B", "Island": "U", "Plains": "W", "Mountain": "R"}.get(name)
    return {
        "instance_id": iid,
        "name": name,
        "type_line": f"Basic Land — {name}" if basic else "Land",
        "oracle_text": oracle or f"({{T}}: Add {{{basic}}}.)",
        "card_types": ["Land"],
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
    }


def _creature(iid, name, seat, power, toughness, oracle="", tapped=False, mana_cost="{1}{W}"):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "card_types": ["Creature"],
        "oracle_text": oracle,
        "mana_cost": mana_cost,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
        "turn_entered_battlefield": 3,
    }


HORTIMANCER_CARD = _creature(226, "Unflinching Hortimancer", OPP, 2, 1, HORTIMANCER, tapped=True)


def _state(our_lands_tapped, *, untapped_islands=0, extra=()):
    """The board from the bug report; our three lands tapped or not."""
    battlefield = [
        _land(221, "Dedicated Commons", OPP, False, "{oT}: Add {oR} or {oW}."),
        _land(225, "Plains", OPP, False),
        _land(236, "Mountain", OPP, False),
        _land(255, "Mountain", OPP, False),
        dict(HORTIMANCER_CARD),
        _land(223, "Swamp", LOCAL, our_lands_tapped),
        _land(230, "Island", LOCAL, our_lands_tapped),
        _land(251, "Island", LOCAL, our_lands_tapped),
        *[_land(900 + n, "Island", LOCAL, False) for n in range(untapped_islands)],
        *extra,
    ]
    return {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 8, "active_player": LOCAL, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": OPP, "life_total": 20, "is_local": False},
            {"seat_id": LOCAL, "life_total": 14, "is_local": True},
        ],
        "battlefield": battlefield,
        "hand": [],
        "stack": [],
        "graveyard": [],
    }


# --- 17:47:57: the cast -----------------------------------------------------


def _priority_state():
    state = _state(our_lands_tapped=False)
    state["hand"] = [
        {
            "instance_id": 222,
            "grp_id": 106264,
            "name": "Seasoned Cryomancer",
            "type_line": "Creature — Human Wizard",
            "oracle_text": CRYOMANCER,
            "mana_cost": "{1}{U}{U}",
            "controller_seat_id": LOCAL,
            "owner_seat_id": LOCAL,
        },
        {
            "instance_id": 186,
            "name": "Mindseeker Oculus",
            "type_line": "Creature — Homunculus",
            "oracle_text": "When this creature enters, empower Jace 4.",
            "mana_cost": "{2}{U}",
            "controller_seat_id": LOCAL,
        },
        {**_land(262, "Island", LOCAL, False), "controller_seat_id": LOCAL},
        {
            **_land(244, "Room of Refuge", LOCAL, False),
            "oracle_text": "This land enters tapped. As it enters, choose a color.\n{oT}: Add one mana of the chosen color.",
        },
    ]
    return state


CAST_CRYOMANCER = {
    "actionType": "ActionType_Cast",
    "grpId": 106264,
    "instanceId": 222,
    "manaCost": [{"color": '[ "Generic" ]', "count": 1}, {"color": '[ "Blue" ]', "count": 2}],
    "autoTapActions": [{"instanceId": 230}, {"instanceId": 251}, {"instanceId": 223}],
    "hasAutoTap": True,
}


def _priority_decision():
    return PendingDecision(
        request_id=(175, 237),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:3",
                "Cast Mindseeker Oculus",
                payable=True,
                meta={
                    "actionType": "ActionType_Cast",
                    "instanceId": 186,
                    "manaCost": [{"count": 3}],
                },
            ),
            DecisionOption("idx:4", "Cast Seasoned Cryomancer", payable=True, meta=dict(CAST_CRYOMANCER)),
            DecisionOption(
                "idx:5",
                "Play land: Room of Refuge",
                meta={"actionType": "ActionType_Play", "instanceId": 244},
            ),
            DecisionOption(
                "idx:6", "Play land: Island", meta={"actionType": "ActionType_Play", "instanceId": 262}
            ),
            DecisionOption("pass", "Pass"),
        ),
        can_pass=True,
    )


def test_hortimancer_ward_is_parsed_from_arena_text():
    ward = ward_of(HORTIMANCER_CARD)
    assert ward is not None and ward.mana == 1 and ward.label == "Ward {1}"
    assert ward_of_text("Flying, ward {o2}").mana == 2
    assert ward_of_text("Ward—Pay 3 life.").life == 3
    assert ward_of_text("Ward—Discard a card.").discard == 1
    assert ward_of_text("Whenever you gain life, put a +1/+1 counter on this creature.") is None


def test_cast_note_says_cryomancer_leaves_no_mana_for_the_ward():
    state, decision = _priority_state(), _priority_decision()
    assert untapped_land_drop(state, decision.options)  # Island 262 enters untapped
    note = ward_cast_note(state, CAST_CRYOMANCER, land_drop=True)
    assert "Unflinching Hortimancer has Ward {1}" in note
    assert "you'd have 0 untapped mana (1 if you play a land first)" in note
    assert "countered" in note


def test_cast_note_reports_spare_mana_and_skips_non_targeting_spells():
    state = _priority_state()
    state["battlefield"].append(_land(276, "Island", LOCAL, False))
    assert "you'd have 1 untapped mana left to pay it" in ward_cast_note(state, CAST_CRYOMANCER)
    assert ward_cast_note(state, {"actionType": "ActionType_Cast", "instanceId": 186}) == ""
    state["battlefield"] = [card for card in state["battlefield"] if card["instance_id"] != 226]
    assert ward_cast_note(state, CAST_CRYOMANCER) == ""


def test_room_of_refuge_is_not_an_untapped_land_drop():
    state, decision = _priority_state(), _priority_decision()
    only_room = replace(decision, options=tuple(o for o in decision.options if o.option_id != "idx:6"))
    assert not untapped_land_drop(state, only_room.options)


class _Backend:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.reply


def _planner(reply='{"option_ids": [], "reasoning": "none"}'):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._timeout = 1.0
    planner._backend = _Backend(reply)
    return planner


def test_priority_prompt_carries_the_ward_fact_on_the_cast_option():
    planner = _planner('{"option_ids": ["idx:6"], "reasoning": "land first"}')
    planner._llm_decision_options(_priority_decision(), _priority_state())
    cast_line = next(line for line in planner._backend.prompts[0].splitlines() if line.startswith("- idx:4"))
    assert "[WARD: Unflinching Hortimancer has Ward {1}" in cast_line
    assert "1 if you play a land first" in cast_line


# --- 17:48:16: the stun trigger's targets ------------------------------------


def _stun_state(untapped_islands=0, extra=()):
    state = _state(our_lands_tapped=True, untapped_islands=untapped_islands, extra=extra)
    cryomancer = _creature(263, "Seasoned Cryomancer", LOCAL, 2, 2, CRYOMANCER, mana_cost="{1}{U}{U}")
    cryomancer["turn_entered_battlefield"] = 8
    state["battlefield"].append(cryomancer)
    state["hand"] = [{**_land(262, "Island", LOCAL, False)}]
    state["stack"] = [
        {
            "instance_id": 274,
            "grp_id": 208426,
            "name": "Seasoned Cryomancer ability",
            "type_line": "Ability",
            "object_kind": "ABILITY",
            "oracle_text": CRYOMANCER,
            "controller_seat_id": LOCAL,
            "parent_instance_id": 263,
        }
    ]
    state["_bridge_request_payload"] = {"sourceId": 274}
    state["decision_context"] = {
        "type": "target_selection",
        "source_id": 274,
        "source_card": "Seasoned Cryomancer",
        "source_oracle_text": CRYOMANCER,
    }
    return state


def _stun_decision(*ids, minimum=0, selected=0, can_cancel=False):
    ids = ids or (226, 263)
    return PendingDecision(
        request_id=(181, 251),
        request_type="SelectTargets",
        options=tuple(DecisionOption(f"tgt:{iid}", f"Target #{iid}") for iid in ids),
        min_select=max(0, minimum - selected),
        max_select=2 - selected,
        can_cancel=can_cancel,
        source_label="Seasoned Cryomancer",
        slots=(TargetSlot(1, minimum, 2, selected, tuple(ids)),),
    )


def test_tap_and_stun_effects_harm_their_target():
    assert target_effect_is_harmful(CRYOMANCER) is True
    assert target_effect_is_harmful("Tap target creature. Put a stun counter on it.") is True
    assert target_effect_is_harmful("Put a stun counter on target creature.") is True
    assert target_effect_is_harmful(
        "Tap target creature. It doesn't untap during its controller's next untap step."
    )
    assert target_effect_is_harmful("Target creature can't block this turn.") is True
    assert target_effect_is_harmful("Untap target creature.") is False
    # Divining Duelist's modes tap OR untap: still mixed, as before.
    assert target_effect_is_harmful("Choose one —\n•Tap target creature.\n•Untap target creature.") is None
    assert target_effect_is_harmful("You may tap or untap target artifact, creature, or land.") is None
    # A granted ability in quotes describes the wearer, not this equip.
    assert target_effect_is_harmful('Equipped creature has "{T}: Tap target creature."') is None


def test_stun_on_opponents_creature_is_accepted_when_ward_is_payable():
    planner = _planner('{"option_ids": ["tgt:226"], "reasoning": "stun their attacker"}')
    assert planner.plan_decision_options(_stun_decision(), _stun_state(untapped_islands=1)) == ["tgt:226"]


def test_stun_on_our_own_creature_is_redirected_or_declined():
    own_pick = '{"option_ids": ["tgt:263"], "reasoning": "tap it"}'
    assert _planner(own_pick).plan_decision_options(_stun_decision(), _stun_state(untapped_islands=1)) == [
        "tgt:226"
    ]
    # Only our creature left: an uncancellable optional trigger takes no target,
    assert _planner(own_pick).plan_decision_options(_stun_decision(263), _stun_state()) == []
    # a cast still being made is cancelled, and a required target declines.
    assert _planner(own_pick).plan_decision_options(_stun_decision(263, can_cancel=True), _stun_state()) == [
        DECLINE_DECISION
    ]
    assert _planner(own_pick).plan_decision_options(_stun_decision(263, minimum=1), _stun_state()) == [
        DECLINE_DECISION
    ]


def test_unpayable_ward_on_optional_trigger_takes_no_target():
    planner = _planner('{"option_ids": ["tgt:226"], "reasoning": "stun Hortimancer"}')
    assert planner.plan_decision_options(_stun_decision(), _stun_state()) == []
    assert "Ward {1}" in planner.get_decision_reasoning([])
    assert planner.get_last_decision_trace()["target_validation"] == "no_targets"


def test_unpayable_ward_prefers_an_unwarded_enemy():
    keeper = _creature(300, "Keeper of the Quiet Hour", OPP, 3, 2)
    state = _stun_state(extra=(keeper,))
    decision = _stun_decision(226, 263, 300)
    for pick in ('["tgt:226"]', '["tgt:226", "tgt:300"]'):
        planner = _planner('{"option_ids": ' + pick + ', "reasoning": "stun"}')
        assert planner.plan_decision_options(decision, state) == ["tgt:300"]
    # The fallback (no usable model answer) ranks a warded enemy last even when it is bigger.
    state["battlefield"][4]["power"] = 5
    assert _planner().plan_decision_options(decision, state) == ["tgt:300"]


def test_required_target_with_unpayable_ward_keeps_existing_choice():
    planner = _planner('{"option_ids": ["tgt:226"], "reasoning": "stun"}')
    assert planner.plan_decision_options(_stun_decision(minimum=1), _stun_state()) == ["tgt:226"]


def test_spell_targets_before_paying_so_its_cost_is_not_spare_mana():
    # Casting "Tap up to one target creature" for {2}{U} with three untapped
    # lands: nothing is left for Ward {1}, and the cast can still be cancelled.
    state = _state(our_lands_tapped=False)
    spell = {
        "instance_id": 410,
        "name": "Frost Snap",
        "type_line": "Instant",
        "object_kind": "CARD",
        "oracle_text": "Tap up to one target creature. Draw a card.",
        "mana_cost": "{2}{U}",
        "controller_seat_id": LOCAL,
    }
    state["stack"] = [spell]
    state["_bridge_request_payload"] = {"sourceId": 410}
    decision = replace(_stun_decision(226), max_select=1, can_cancel=True, source_label="Frost Snap")
    planner = _planner('{"option_ids": ["tgt:226"], "reasoning": "tap it"}')
    assert planner.plan_decision_options(decision, state) == [DECLINE_DECISION]
    state["battlefield"].append(_land(277, "Island", LOCAL, False))
    assert _planner('{"option_ids": ["tgt:226"]}').plan_decision_options(decision, state) == ["tgt:226"]


def test_target_prompt_marks_the_unpayable_ward():
    planner = _planner('{"option_ids": ["tgt:226"], "reasoning": "stun"}')
    planner.plan_decision_options(_stun_decision(), _stun_state())
    prompt = planner._backend.prompts[0]
    assert '"ward": "Ward {1}", "ward_payable_now": false' in prompt
    assert "Mana available to pay it: 0" in prompt


def test_representation_with_a_target_already_chosen_commits_it():
    # 17:48:39: 226 selected, only our Cryomancer still selectable, AllowCancel_Abort.
    decision = _stun_decision(263, selected=1, can_cancel=True)
    planner = _planner('{"option_ids": [], "reasoning": "do not stun my own creature"}')
    assert planner.plan_decision_options(decision, _stun_state(untapped_islands=1)) == []


# --- submission ----------------------------------------------------------------


def test_empty_target_selection_is_submitted_only_when_optional():
    sent = []
    bridge = SimpleNamespace(submit_targets=lambda ids: sent.append(ids) or True)
    assert submit_option(bridge, _stun_decision(), [])
    assert sent == [[]]
    assert not submit_option(bridge, _stun_decision(minimum=1), [])
    assert sent == [[]]


def test_only_the_native_adapter_receives_an_empty_target_list():
    bridge = GREBridge.__new__(GREBridge)
    sent = []
    bridge._send_safe = lambda command, timeout=None: sent.append(command) or {"ok": True}
    bridge._mac_adapter = None
    assert bridge.submit_targets([]) is False  # the plugin would autofill a target
    assert sent == []
    bridge._mac_adapter = object()
    assert bridge.submit_targets([]) is True
    assert sent == [{"action": "submit_targets", "target_instance_id": None, "target_instance_ids": []}]


# --- the ward trigger asking us to pay ---------------------------------------------


def _ward_trigger_state(untapped_islands=1):
    state = _stun_state(untapped_islands=untapped_islands)
    state["stack"].append(
        {
            "instance_id": 275,
            "grp_id": 143868,
            "name": "Unflinching Hortimancer ability",
            "type_line": "Ability",
            "object_kind": "ABILITY",
            "oracle_text": HORTIMANCER,
            "controller_seat_id": OPP,
            "parent_instance_id": 226,
        }
    )
    state["decision_context"] = {"type": "pay_costs", "sourceId": 275}
    state["_bridge_request_payload"] = {"sourceId": 275}
    state["_bridge_can_cancel"] = True
    return state


def test_ward_payment_prompt_is_accepted_without_the_model():
    decision = PendingDecision(
        request_id=(183, 254),
        request_type="OptionalAction",
        options=(
            DecisionOption("optional:accept", "Accept the optional effect", meta={"sourceId": 275}),
            DecisionOption("optional:decline", "Decline the optional effect", meta={"sourceId": 275}),
        ),
    )
    planner = _planner('{"option_ids": ["optional:decline"], "reasoning": "save mana"}')
    assert planner.plan_decision_options(decision, _ward_trigger_state()) == ["optional:accept"]
    assert planner._backend.prompts == []


def test_optional_cost_gate_never_declines_a_ward_payment():
    from arenamcp.autopilot_modes import _AutopilotModesMixin

    class _Engine(_AutopilotModesMixin):
        _last_cast_submitted = None
        _last_cast_submitted_ts = 0.0

        def _source_spell_is_harmful_to_target(self, *args):
            raise AssertionError("a ward payment must not reach the harm check")

    assert _Engine()._should_decline_optional_cost(_ward_trigger_state()) is None


def test_autopilot_submits_no_targets_for_the_reported_stun(monkeypatch):
    import arenamcp.autopilot as autopilot_module
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    poll = {
        "has_pending": True,
        "request_type": "SelectTargets",
        "game_state_id": 181,
        "msg_id": 251,
        "source_instance_id": 274,
        "can_cancel": False,
        "target_candidates": [
            {"targetInstanceId": 226, "targetIdx": 1, "grpId": 0},
            {"targetInstanceId": 263, "targetIdx": 1, "grpId": 0},
        ],
        "target_selections": [{"targetIdx": 1, "minTargets": 0, "maxTargets": 2, "selectedTargets": 0}],
    }
    submitted = []

    class _Bridge:
        connected = True

        def connect(self):
            return True

        def get_pending_actions(self):
            return poll

        def submit_targets(self, ids):
            submitted.append(ids)
            return True

    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _Bridge())
    engine = AutopilotEngine(
        planner=_planner('{"option_ids": ["tgt:226"], "reasoning": "stun Hortimancer"}'),
        mapper=None,
        controller=None,
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )
    notifications = []
    engine._ui_advice_fn = lambda text, label: notifications.append(text)
    state = {**_stun_state(), "_bridge_connected": True, "_bridge_request_type": "SelectTargets"}
    assert engine._try_typed_decision_path(state, "decision_required") is True
    assert submitted == [[]]
    assert notifications[-1].startswith("Choosing no targets.")
    assert "Ward {1} with 0 mana available, which would counter the whole effect" in notifications[-1]


def test_ward_facts_never_break_a_decision():
    state = _priority_state()
    garbage = {**CAST_CRYOMANCER, "manaCost": [{"count": "x"}]}
    assert ward_cast_note(state, garbage) == ""
    assert not untapped_land_drop(
        state, [SimpleNamespace(meta={"actionType": "ActionType_Play", "instanceId": object()})]
    )
