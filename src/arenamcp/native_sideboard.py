"""Request-bound, match-local Traditional Limited submission via reflect-1.

Protocol evidence: re-output SharedClientCore/GreClient.Rules/SubmitDeckRequest.cs
and Core/Wotc.Mtga.DuelScene.Interactions/SubmitDeckWorkflow.cs (2026-08-26).
We clone the GRE deck, never mutate DeckBuilderModel or a collection/event deck.
"""

from __future__ import annotations

import threading
from collections import Counter

from arenamcp.mac_bridge_adapter import AdapterError, B, H, I, U, _Ops, enum_name, field, items
from arenamcp.sideboard_plan import MAX_POOL, card_ids, unchanged_plan, validate_plan

CONSTRAINTS = {
    "min_deck": "MinDeckSize",
    "max_deck": "MaxDeckSize",
    "min_sideboard": "MinSideboardSize",
    "max_sideboard": "MaxSideboardSize",
    "min_commander": "MinCommanderSize",
    "max_commander": "MaxCommanderSize",
}


def _complete_list(node: dict) -> list:
    values = items(node)
    if not isinstance(node, dict) or type(node.get("$n")) is not int or node["$n"] != len(values):
        raise AdapterError("Incomplete native sideboard collection")
    return values


def _reads(ops: _Ops, snapshot) -> dict:
    """Every identity/guard is repeated in the eventual mutation frame."""
    r = {}
    request = r["request"] = ops.pending(0)
    ops.expect_pending(H(snapshot.handle))
    ops.expect_class(request, "SubmitDeckRequest")
    message = ops.get(request, "OriginalMessage")
    ops.expect_member(message, "GameStateId", snapshot.game_state_id)
    ops.expect_member(message, "MsgId", snapshot.msg_id)
    # Submit() spends this delegate. A still-pending but answered request must not repeat.
    ops.expect_class(ops.get(request, "OnSubmit"), "Action`1")
    manager = ops.add("find", **{"class": "GameManager"}, depth=0)
    workflow = r["workflow"] = ops.get(ops.get(manager, "WorkflowController"), "CurrentWorkflow")
    ops.expect_class(workflow, "SubmitDeckWorkflow")
    ops.expect_pending(ops.get(workflow, "BaseRequest"))
    game = ops.get(ops.get(workflow, "_gameStateProvider"), "LatestGameState")
    info = r["info"] = ops.get(game, "GameInfo")
    r["match_id"] = ops.get(info, "MatchID")
    r["game_number"] = ops.get(info, "GameNumber")
    r["super_format"] = ops.get(info, "SuperFormat")
    constraints = r["constraint_object"] = ops.get(info, "DeckConstraintInfo")
    for key, member in CONSTRAINTS.items():
        r[key] = ops.get(constraints, member)
    timer = ops.get(workflow, "_sideboardTimer", optional=True)
    r["remaining"] = ops.get(timer, "RemainingTime", optional=True)
    widget = r["widget"] = ops.get(ops.get(workflow, "_sideboardInterface"), "_deckBuilder")
    ops.expect_member(widget, "_isActive", True)
    context = ops.get(widget, "Context")
    ops.expect_member(context, "IsSideboarding", True)
    ops.expect_member(context, "IsLimited", True)
    ops.expect_member(context, "IsReadOnly", False)
    model = ops.get(ops.get(widget, "ModelProvider"), "_model")
    ops.expect_member(model, "HasLoadedDeck", True)
    editor = r["editor"] = ops.call(model, "GetServerModel")
    r["editor_main"] = ops.get(editor, "mainDeck", depth=2)
    r["editor_commander"] = ops.get(editor, "commandZone", depth=2)
    r["editor_companion"] = ops.get(editor, "companion", depth=1)
    deck = r["deck"] = ops.get(request, "Deck")
    for key, member in (
        ("main", "DeckCards"),
        ("sideboard", "SideboardCards"),
        ("commander", "CommanderCards"),
    ):
        r[key] = ops.add("get", target=deck, member=member, depth=2, max_items=MAX_POOL)
    r["companion"] = ops.get(deck, "DeckMessageFieldFour")
    return r


def _editor_counts(node: dict) -> Counter:
    result = Counter()
    for row in _complete_list(node):
        gid, count = field(row, "Id"), field(row, "Quantity")
        if type(gid) is not int or gid <= 0 or type(count) is not int or count <= 0:
            raise AdapterError("Invalid native editor card count")
        result[gid] += count
    return result


def observe(adapter, timeout=None) -> tuple[dict, object, dict]:
    snapshot = adapter._require(timeout, "SubmitDeckRequest")
    if type(snapshot.game_state_id) is not int or snapshot.game_state_id < 0 or not snapshot.msg_id:
        raise AdapterError("Sideboard request has no exact GRE identity")
    ops = _Ops()
    refs = _reads(ops, snapshot)
    values = adapter._run(ops, timeout)
    if len(values) != len(ops.ops):
        raise AdapterError("Incomplete native sideboard reflection read")
    data = {k: values[ref["ref"]] for k, ref in refs.items()}
    match_id, game_number = data["match_id"], data["game_number"]
    if not isinstance(match_id, str) or not match_id or type(game_number) is not int or game_number <= 0:
        raise AdapterError("Sideboard match/game identity unavailable")
    try:
        deck = {k: sorted(card_ids(_complete_list(data[k]))) for k in ("main", "sideboard", "commander")}
        deck["companion"] = data["companion"]
        state = {
            "ok": True,
            "request_id": [match_id, game_number, snapshot.game_state_id, snapshot.msg_id, snapshot.handle],
            "deck": deck,
            "super_format": enum_name(data["super_format"]),
            "constraints": {k: data[k] for k in CONSTRAINTS},
            "remaining_seconds": data["remaining"] if type(data["remaining"]) in (int, float) else None,
        }
        unchanged_plan(state)
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError(str(exc)) from exc
    companion = (
        field(data["editor_companion"], "Id", 0) if field(data["editor_companion"], "Quantity", 0) else 0
    )
    if (
        _editor_counts(data["editor_main"]) != Counter(deck["main"])
        or _editor_counts(data["editor_commander"]) != Counter(deck["commander"])
        or companion != deck["companion"]
    ):
        raise AdapterError("Sideboard editor changed manually; leaving the player's deck untouched")
    # GetServerModel's limited sideboard includes the free basic-land pool. The
    # GRE deck is the registration authority. We never move/add those extra lands.
    return state, snapshot, {name: _complete_list(data["editor_" + name]) for name in ("main", "commander")}


def submit(adapter, command: dict, timeout=None) -> dict:
    state, snapshot, editor_rows = observe(adapter, timeout)
    if command.get("request_id") != state["request_id"] or command.get("source_deck") != state["deck"]:
        raise AdapterError("Stale sideboard request or registered deck changed")
    try:
        plan = validate_plan(state, command.get("plan") or {})
    except (TypeError, KeyError, ValueError) as exc:
        raise AdapterError(str(exc)) from exc
    key = tuple(state["request_id"][:2])
    attempted = adapter.__dict__.setdefault("_sideboard_attempted", set())
    if key in attempted:
        return {
            "ok": False,
            "already_attempted": True,
            "error": "Sideboard already attempted for this game; not repeating",
        }
    ops = _Ops()
    refs = _reads(ops, snapshot)
    info = refs["info"]
    ops.expect_member(info, "MatchID", key[0])
    ops.expect_member(info, "GameNumber", key[1])
    ops.expect_member(info, "SuperFormat", "Limited")
    for name, member in CONSTRAINTS.items():
        ops.expect_member(refs["constraint_object"], member, state["constraints"][name])
    # Detect a human swap made after observation, in the same frame as submission.
    for pile, rows in editor_rows.items():
        entries = refs["editor_" + pile]
        ops.expect_member(entries, "Count", len(rows))
        for i, row in enumerate(rows):
            entry = ops.call(entries, "get_Item", I(i))
            ops.expect_member(entry, "Id", field(row, "Id"))
            ops.expect_member(entry, "Quantity", field(row, "Quantity"))
    # Companion/commander swaps are rejected above. Guard manual companion edits too.
    editor = refs["editor"]
    if state["deck"]["companion"]:
        companion = ops.get(editor, "companion")
        ops.expect_member(companion, "Id", state["deck"]["companion"])
        ops.expect_member(companion, "Quantity", 1)
    else:
        ops.expect_member(editor, "companion", None)
    deck = ops.call(refs["deck"], "Clone")
    for pile, member in (("main", "DeckCards"), ("sideboard", "SideboardCards")):
        cards = ops.get(deck, member)
        ops.call(cards, "Clear")
        for gid in plan[pile]:
            ops.call(cards, "Add", U(gid))
        ops.expect_member(cards, "Count", len(plan[pile]))
    # Bind all post-submit state holders before the request can be sent.
    workflow = refs["workflow"]
    scene = ops.get(workflow, "_duelSceneStateController")
    local = ops.get(ops.get(workflow, "_matchManager"), "LocalPlayerInfo")
    styles = ops.call(ops.get(local, "CardStyles"), "ToArray")
    # SetDeckInfo clears its styles list, so pass a detached copy of CardStyles.
    # SetState(false) in C# is the THREE-argument Nullable<bool> overload. The
    # probe cannot construct nullable arguments. Use the exact one-argument
    # DuelSceneState overload with a fresh value preserving every other flag.
    # All retained values and construction are checked before SubmitDeck.
    current = ops.add("get", target=ops.get(scene, "CurrentState"), member="Value", retain_value=True)
    ops.expect_class(current, "DuelSceneState")
    members = (
        "MatchScene_SubScene",
        "MatchScene_SubSceneTransitioning",
        "FirstGameStatePlaybackComplete",
        "GameComplete",
    )
    kept = [ops.add("get", target=current, member=name, retain_value=True) for name in members]
    unlocked = ops.new("Wotc.Mtga.DuelScene.DuelSceneState", kept[0], kept[1], B(False), kept[2], kept[3])
    ops.expect_class(unlocked, "DuelSceneState")
    ops.expect_member(unlocked, "MatchScene_LockSceneTransitions", False)
    # These getters must have returned objects before the external side effect.
    ops.add("expect", target=scene)
    ops.add("expect", target=local)
    ops.add("expect", target=styles)
    sending = len(ops.ops)
    ops.call(refs["request"], "SubmitDeck", deck)
    # Same match-local updates as SubmitDeckWorkflow.OnDoneClicked.submitDeck().
    ops.call(scene, "SetState", unlocked)
    ops.call(local, "SetDeckInfo", ops.get(deck, "DeckCards"), ops.get(deck, "SideboardCards"), styles)
    lock = adapter.__dict__.setdefault("_sideboard_attempt_lock", threading.Lock())
    with lock:
        if key in attempted:
            return {"ok": False, "already_attempted": True, "error": "Sideboard submission already in flight"}
        attempted.add(key)  # Before I/O: timeout/lost replies must never resend this deck.
    try:
        adapter._run(ops, timeout)
    except Exception as exc:
        failed = getattr(exc, "failed_op", None)
        unknown = not isinstance(failed, int) or failed >= sending
        return {
            "ok": False,
            "outcome_unknown": unknown,
            "request_id": state["request_id"],
            "error": f"Sideboard submission {'outcome unknown' if unknown else 'rejected before send'}: {exc}",
        }
    return {
        "ok": True,
        "submitted_type": "SideboardDeck",
        "request_id": state["request_id"],
        "awaiting_acceptance": True,
    }
