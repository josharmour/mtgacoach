from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.sideboard_fakes import SideboardWorld

from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.sideboard_driver import SideboardDriver
from arenamcp.sideboard_plan import SideboardAdvisor, apply_swaps, unchanged_plan, validate_plan


def setup():
    world = SideboardWorld()
    adapter = MacBridgeAdapter(world.send)
    state = adapter.handle({"action": "get_sideboard_state"})
    assert state["ok"], state
    return world, adapter, state


def command(state, plan=None):
    return {
        "action": "submit_sideboard",
        "request_id": state["request_id"],
        "source_deck": state["deck"],
        "plan": plan or unchanged_plan(state),
    }


def cards():
    return [
        {"grp_id": 1, "type_line": "Creature", "oracle_text": "Flying", "mana_cost": "{U}"},
        {"grp_id": 2, "type_line": "Creature", "oracle_text": "Reach", "mana_cost": "{U}"},
        {"grp_id": 7001, "type_line": "Basic Land", "oracle_text": "{T}: Add {U}."},
    ]


def test_native_state_has_exact_identity_and_actual_size_constraints():
    world, _, state = setup()
    assert state["request_id"] == ["match-318", 1, 40, 7, world.request]
    assert state["constraints"]["max_sideboard"] == 250  # Never a guessed constructed 15.
    assert state["deck"] == {
        "main": sorted(world.main),
        "sideboard": world.side,
        "commander": [],
        "companion": 0,
    }
    assert state["remaining_seconds"] == 60
    assert not world.submitted


def test_submit_swaps_only_match_local_clone_and_updates_local_match_info_once():
    world, adapter, state = setup()
    plan = apply_swaps(state, {"swaps": [{"out": 1, "in": 2, "count": 2}]}, cards())
    result = adapter.handle(command(state, plan))
    assert result["ok"] and result["awaiting_acceptance"]
    assert world.submitted == [(plan["main"], plan["sideboard"])]
    assert world.local_deck == world.submitted[0]
    assert world.ids(world.original, "DeckCards") == world.ids(world.saved, "DeckCards") == world.main
    assert (
        world.ids(world.original, "SideboardCards") == world.ids(world.saved, "SideboardCards") == world.side
    )
    assert not adapter.handle(command(state, plan))["ok"]
    assert len(world.submitted) == 1
    assert all(
        method not in {"AddCardToMainDeck", "RemoveCardFromMainDeck", "DoneButton_OnClick", "UpdateDeck"}
        for _, method, _ in world.calls
    )


@pytest.mark.parametrize("change", ["request", "deck", "format", "constraint", "manual"])
def test_changed_source_or_illegal_plan_rejected_before_any_submission(change):
    world, adapter, state = setup()
    cmd = command(state)
    if change == "request":
        cmd["request_id"] = ["old-match", 1, 40, 7, 4]
    elif change == "deck":
        cmd["source_deck"] = {**state["deck"], "sideboard": [99]}
    elif change == "format":
        world.objects[world.info]["SuperFormat"] = "Constructed"
    elif change == "constraint":
        world.objects[world.constraints]["MaxSideboardSize"] = 2
    else:
        row = world.objects[world.editor_main]["_items"][0].handle
        world.objects[row]["Quantity"] -= 1
    assert not adapter.handle(cmd)["ok"]
    assert not world.submitted


@pytest.mark.parametrize("change", ["request", "game", "constraint", "manual", "answered", "context"])
def test_same_frame_guard_catches_changes_after_final_read(change):
    world, adapter, state = setup()

    def mutate(w):
        if change == "request":
            w.objects[w.original_message]["MsgId"] = 8
        elif change == "game":
            w.objects[w.info]["GameNumber"] = 2
        elif change == "constraint":
            w.objects[w.constraints]["MaxDeckSize"] = 39
        elif change == "manual":
            row = w.objects[w.editor_main]["_items"][0].handle
            w.objects[row]["Quantity"] -= 1
        elif change == "answered":
            w.objects[w.request]["OnSubmit"] = None
        else:
            w.objects[10]["IsSideboarding"] = False

    world.before_submit = mutate
    result = adapter.handle(command(state))
    assert not result["ok"] and result["outcome_unknown"] is False
    assert not world.submitted


@pytest.mark.parametrize("lost,local_failure", [(True, False), (False, True)])
def test_uncertain_send_or_post_send_failure_never_resubmits(lost, local_failure):
    world, adapter, state = setup()
    world.lose_reply, world.after_submit_failure = lost, local_failure
    result = adapter.handle(command(state))
    assert not result["ok"] and result["outcome_unknown"]
    assert len(world.submitted) == 1
    # Even a server re-issued request for the same game is not blindly retried.
    from tests.mac_world import Obj

    world.objects[world.request]["OnSubmit"] = Obj(world.delegate)
    world.objects[world.original_message]["MsgId"] = 8
    fresh = adapter.handle({"action": "get_sideboard_state"})
    assert adapter.handle(command(fresh))["already_attempted"]
    assert len(world.submitted) == 1


@pytest.mark.parametrize(
    "proposal",
    [
        {"swaps": [{"out": 1, "in": 999, "count": 1}]},
        {"swaps": [{"out": 1, "in": 2, "count": 4}]},
        {"swaps": [{"out": 7001, "in": 2, "count": 1}]},
        {"swaps": [{"out": 1, "in": 2, "count": True}]},
        {"swaps": [{"out": 1, "in": 2, "count": 1}] * 2},
    ],
)
def test_invalid_or_land_swaps_rejected(proposal):
    _, _, state = setup()
    with pytest.raises(ValueError):
        apply_swaps(state, proposal, cards())


def test_missing_constraints_extra_cards_and_companion_changes_are_rejected():
    _, _, state = setup()
    with pytest.raises(ValueError):
        validate_plan({**state, "constraints": {}}, state["deck"])
    changed = {"main": state["deck"]["main"][:-1] + [999], "sideboard": state["deck"]["sideboard"]}
    with pytest.raises(ValueError):
        validate_plan(state, changed)
    state["deck"]["companion"] = 2
    with pytest.raises(ValueError):
        apply_swaps(state, {"swaps": [{"out": 1, "in": 2, "count": 1}]}, cards())


@pytest.mark.parametrize(
    "answer", ['{"swaps": [], "reason":"Keep it"}', "not json", '{"swaps":[{"out":1,"in":999,"count":1}]}']
)
def test_advisor_empty_or_invalid_plan_preserves_deck(answer):
    _, _, state = setup()
    backend = SimpleNamespace(complete=Mock(return_value=answer))
    advisor = SideboardAdvisor(backend, timeout=0.1)
    try:
        plan, reason = advisor.choose(state, cards(), [cards()[0]])
        assert plan == unchanged_plan(state) and reason
    finally:
        advisor._executor.shutdown(wait=True)


def test_driver_submits_once_and_next_game_gets_its_own_plan():
    _, _, state = setup()
    live = [state]
    calls, notices = [], []

    def send(action, **kw):
        calls.append((action, kw))
        return deepcopy(live[0]) if action == "get_sideboard_state" else {"ok": True}

    advisor = SimpleNamespace(choose=Mock(side_effect=lambda s, c, o: (unchanged_plan(s), "keep")))
    driver = SideboardDriver(
        send, notices.append, lambda: advisor, lambda s: {"cards": cards(), "opponent": []}
    )
    driver.step(lambda: False)
    driver.step(lambda: False)
    assert [x[0] for x in calls].count("submit_sideboard") == 1
    assert advisor.choose.call_count == 1
    live[0] = deepcopy(state)
    live[0]["request_id"][1] = 2
    driver.step(lambda: False)
    assert [x[0] for x in calls].count("submit_sideboard") == 2


@pytest.mark.parametrize(
    "outcome", [{"ok": False, "error": "rejected"}, {"ok": False, "outcome_unknown": True}]
)
def test_driver_does_not_repeat_rejected_or_uncertain_submission(outcome):
    _, _, state = setup()
    submit = Mock(return_value=outcome)

    def send(action, **kw):
        return state if action == "get_sideboard_state" else submit(**kw)

    driver = SideboardDriver(send, lambda s: None)
    driver.step(lambda: False)
    driver.step(lambda: False)
    submit.assert_called_once()


def test_driver_toggle_off_during_reasoning_prevents_submission():
    _, _, state = setup()
    calls = []
    cancelled = [False]

    def choose(*args):
        cancelled[0] = True
        return unchanged_plan(state), "keep"

    def send(action, **kw):
        calls.append(action)
        return state

    driver = SideboardDriver(send, lambda s: None, lambda: SimpleNamespace(choose=choose))
    driver.step(lambda: cancelled[0])
    assert calls == ["get_sideboard_state"]


def test_scene_unlock_uses_exact_value_overload_and_preserves_styles():
    world, adapter, state = setup()
    assert adapter.handle(command(state))["ok"]
    assert world.new_scene_state == {
        "MatchScene_SubScene": "DuelScene",
        "MatchScene_SubSceneTransitioning": False,
        "MatchScene_LockSceneTransitions": False,
        "FirstGameStatePlaybackComplete": True,
        "GameComplete": True,
    }
    assert world.objects[world.styles]["_items"] == ["style-a"]
    batch = world.sent[-1]
    update = next(op for op in batch if op.get("method") == "SetState")
    assert len(update["args"]) == 1
    assert batch[update["args"][0]["ref"]]["op"] == "new"


def test_old_probe_without_retained_value_support_stops_before_sending():
    world, adapter, state = setup()
    world.retain_values = False
    result = adapter.handle(command(state))
    assert not result["ok"] and not result["outcome_unknown"]
    assert not world.submitted


def test_concurrent_call_cannot_submit_same_game_twice():
    import concurrent.futures
    import threading

    world, adapter, state = setup()
    started, release = threading.Event(), threading.Event()

    def hold(_):
        started.set()
        assert release.wait(2)

    world.before_submit = hold
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(adapter.handle, command(state))
        try:
            assert started.wait(2)
            assert adapter.handle(command(state))["already_attempted"]
        finally:
            release.set()
        assert first.result()["ok"]
    assert len(world.submitted) == 1


def test_spent_request_while_waiting_is_not_reported_as_a_new_failure():
    _, _, state = setup()
    read = [state]
    notices = []

    def send(action, **kw):
        return read[0] if action == "get_sideboard_state" else {"ok": True}

    driver = SideboardDriver(send, notices.append)
    driver.step(lambda: False)
    read[0] = {"ok": False, "error": "OnSubmit spent"}
    driver.step(lambda: False)
    assert notices[-1] == "Sideboard submitted; waiting for Arena to start the next game."
    driver.left_sideboard()
    driver.step(lambda: False)
    assert "manual review" in notices[-1]


def test_short_sideboard_timer_uses_unchanged_deck_without_model_delay():
    _, _, state = setup()
    state["remaining_seconds"] = 6
    calls = []
    advisor_fn = Mock(side_effect=AssertionError("no slow model call"))

    def send(action, **kw):
        calls.append((action, kw))
        return state if action == "get_sideboard_state" else {"ok": True}

    driver = SideboardDriver(send, lambda s: None, advisor_fn)
    driver.step(lambda: False)
    advisor_fn.assert_not_called()
    assert calls[-1][1]["plan"] == unchanged_plan(state)


def test_reissued_deck_request_is_reported_without_a_second_submission():
    _, _, state = setup()
    live = [deepcopy(state)]
    submit, notices = Mock(return_value={"ok": True}), []

    def send(action, **kw):
        return live[0] if action == "get_sideboard_state" else submit(**kw)

    driver = SideboardDriver(send, notices.append)
    driver.step(lambda: False)
    live[0]["request_id"][3] += 1
    driver.step(lambda: False)
    assert "another deck" in notices[-1]
    submit.assert_called_once()


def test_lingering_spent_request_eventually_gets_explicit_manual_status():
    _, _, state = setup()
    live, clock, notices = [state], [0.0], []

    def send(action, **kw):
        return live[0] if action == "get_sideboard_state" else {"ok": True}

    driver = SideboardDriver(send, notices.append, clock=lambda: clock[0])
    driver.step(lambda: False)
    live[0] = {"ok": False, "error": "already answered"}
    clock[0] = 20
    driver.step(lambda: False)
    assert "has not advanced" in notices[-1] and "will not be repeated" in notices[-1]


def test_valid_model_swap_is_selected_and_timeout_keeps_original_deck():
    import threading

    _, _, state = setup()
    backend = SimpleNamespace(
        complete=Mock(return_value='{"swaps":[{"out":1,"in":2,"count":1}],"reason":"Reach for their flyers"}')
    )
    advisor = SideboardAdvisor(backend, timeout=0.1)
    try:
        plan, _ = advisor.choose(state, cards(), [cards()[0]])
        assert plan["main"].count(2) == 1 and plan["sideboard"].count(1) == 1
    finally:
        advisor._executor.shutdown(wait=True)
    release = threading.Event()

    def slow(*args, **kwargs):
        assert release.wait(2)
        return '{"swaps":[{"out":1,"in":2,"count":1}]}'

    advisor = SideboardAdvisor(SimpleNamespace(complete=slow), timeout=0.01)
    try:
        plan, _ = advisor.choose(state, cards(), [cards()[0]])
        assert plan == unchanged_plan(state)
        # A still-running answer must not be reused for a later request.
        next_state = deepcopy(state)
        next_state["request_id"][1] = 2
        plan, _ = advisor.choose(next_state, cards(), [cards()[0]])
        assert plan == unchanged_plan(next_state)
    finally:
        release.set()
        advisor._executor.shutdown(wait=True)


@pytest.mark.parametrize("special", ["companion", "commander"])
def test_retaining_deck_preserves_special_piles_and_identities(special):
    from tests.mac_world import Obj

    world, adapter, _ = setup()
    if special == "companion":
        world.objects[world.original]["DeckMessageFieldFour"] = 2
        world.objects[world.editor]["companion"] = Obj(world.alloc("CardInDeck", Id=2, Quantity=1))
    else:
        world.objects[world.original]["CommanderCards"] = Obj(world.collection([600]))
        world.objects[world.editor]["commandZone"] = Obj(
            world.collection([Obj(world.alloc("CardInDeck", Id=600, Quantity=1))])
        )
        world.objects[world.constraints]["MaxCommanderSize"] = 1
    state = adapter.handle({"action": "get_sideboard_state"})
    assert state["ok"]
    assert adapter.handle(command(state))["ok"]
    sent = world.submitted_handles[0]
    assert (
        world.objects[sent]["DeckMessageFieldFour"] == world.objects[world.original]["DeckMessageFieldFour"]
    )
    assert world.ids(sent, "CommanderCards") == world.ids(world.original, "CommanderCards")


def test_incomplete_native_read_is_explicit_failure_without_submission():
    world = SideboardWorld()

    def send(command, timeout):
        result = world.send(command, timeout)
        if any(op.get("member") == "DeckConstraintInfo" for op in command["ops"]):
            result["results"] = result["results"][:-1]
        return result

    result = MacBridgeAdapter(send).handle({"action": "get_sideboard_state"})
    assert not result["ok"] and "Incomplete" in result["error"]
    assert not world.submitted


@pytest.mark.parametrize(
    "current,ended,expected",
    [
        (("match-318", 1), ("old-match", 3), [10]),
        (("match-318", 1), ("match-318", 1), [20]),
        (("match-318", 1), ("old-match", 3), []),
        ((None, None), ("match-318", 1), [20]),
        (("new-match", 1), ("old-match", 3), []),
    ],
)
def test_opponent_evidence_has_matching_game_provenance(current, ended, expected):
    import threading

    from arenamcp.sideboard_plan import opponent_ids_for_sideboard

    game = SimpleNamespace(
        _state_lock=threading.RLock(),
        match_id=current[0],
        game_number=current[1],
        get_opponent_played_cards=lambda: [10] if expected == [10] else [],
        last_ended_game=lambda: {"match_id": ended[0], "game_number": ended[1]},
        get_last_game_opponent_played_cards=lambda: [20],
    )
    assert opponent_ids_for_sideboard(game, ["match-318", 1, 40, 7, 4]) == expected


def test_event_driver_runs_plan_through_native_adapter_and_releases_for_gameplay():
    from tests.draft_fakes import driver_for, make_primer

    world, adapter, _ = setup()
    in_match = [False]
    actions = []

    def command_action(action, **kw):
        actions.append(action)
        if action == "get_screen":
            return {"ok": True, "sideboarding": True, "deck_builder": True}
        return adapter.handle({"action": action, **kw})

    advisor = SimpleNamespace(
        choose=lambda state, cs, opp: (
            apply_swaps(state, {"swaps": [{"out": 1, "in": 2, "count": 1}]}, cs),
            "Matchup swap",
        )
    )
    bridge = SimpleNamespace(connected=True, draft_command=command_action)
    driver = driver_for(
        bridge,
        make_primer(),
        in_match_fn=lambda: in_match[0],
        sideboard_advisor_fn=lambda: advisor,
        sideboard_context_fn=lambda s: {"cards": cards(), "opponent": [cards()[0]]},
    )
    driver._step()
    driver._step()
    assert driver.owns_ui and len(world.submitted) == 1
    assert world.submitted[0][0].count(2) == 1
    assert actions.count("submit_sideboard") == 1
    in_match[0] = True
    driver._step()
    assert not driver.owns_ui and not driver.paused_reason
