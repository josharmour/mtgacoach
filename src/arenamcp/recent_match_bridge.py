"""Arena's featured Recent Match tile, resolved from its live PlayBlade model.

LastPlayedBladeView uses RecentlyPlayed.LastOrDefault(), after BladeData has
removed unavailable entries. Never scan backwards for a more convenient deck.
All operations are on Unity's main thread, with no desktop input or focus change.
"""

from __future__ import annotations

from arenamcp.mac_bridge_adapter import AdapterError, H, I, _Ops, enum_name, field, guid, handle, items


def idle_ops(ops):
    home = ops.add("find", **{"class": "HomePageContentController"}, depth=0)
    obj = ops.get(home, "gameObject")
    ops.expect_member(obj, "activeInHierarchy", True)
    wrapper = ops.add("static", **{"class": "WrapperController"}, member="Instance", depth=0)
    scene = ops.get(wrapper, "SceneLoader")
    ops.expect_member(scene, "IsInDuelScene", False)
    ops.expect_member(scene, "IsLoading", False)
    loading = ops.get(scene, "_loadingIndicatorIds")
    ops.expect_member(loading, "Count", 0)
    matchmaking = ops.get(wrapper, "Matchmaking")
    ops.expect_member(matchmaking, "_waitingForMatch", False)
    ops.expect_member(matchmaking, "_cachedMatchConfig", None)
    blade = ops.get(home, "_playBladeV3")
    model = ops.get(blade, "_data")
    recent = ops.get(model, "RecentlyPlayed")
    return home, wrapper, recent


def recent_match(adapter, timeout):
    ops = _Ops()
    home, wrapper, recent = idle_ops(ops)
    count = ops.get(recent, "Count")
    values = adapter._run(ops, timeout)
    size = values[count["ref"]]
    if type(size) is not int or not 0 < size <= 10:
        raise AdapterError("Recent Match has no available entry; select a game in Arena first")
    ops = _Ops()
    entry = ops.call(H(handle(values[recent["ref"]])), "get_Item", I(size - 1))
    queue = ops.get(entry, "IsQueueEvent")
    info = ops.get(entry, "EventInfo", depth=1)
    deck = ops.get(entry, "SelectedDeckInfo")
    values2 = adapter._run(ops, timeout)
    if values2[queue["ref"]] is not True:
        raise AdapterError(
            "The first Recent Match entry opens an event page; no paid event or substitute was selected"
        )
    event = field(values2[info["ref"]], "EventName")
    format_name = field(values2[info["ref"]], "FormatName")
    if not isinstance(event, str) or not event or not isinstance(format_name, str) or not format_name:
        raise AdapterError("Recent Match event or format is unavailable")
    if field(values2[info["ref"]], "IsLimited") is not False:
        raise AdapterError("Recent Match is a limited or unknown event; automatic entry stopped")
    if not handle(values2[deck["ref"]]):
        raise AdapterError("The first Recent Match entry has no available deck")
    ops = _Ops()
    selected = H(handle(values2[deck["ref"]]))
    deck_id = ops.get(selected, "deckId", depth=1)
    name = ops.get(selected, "deckName")
    manager = ops.get(H(handle(values[wrapper["ref"]])), "EventManager")
    context = ops.call(manager, "GetEventContext", {"str": event})
    player_event = ops.get(context, "PlayerEvent")
    event_info = ops.get(player_event, "EventInfo")
    fees = ops.get(event_info, "EntryFees", depth=2)
    values3 = adapter._run(ops, timeout)
    free_index = next(
        (
            i
            for i, fee in enumerate(items(values3[fees["ref"]]))
            if enum_name(field(fee, "CurrencyType")) in {"None", "Free"}
            and field(fee, "Quantity") == 0
            and field(fee, "UsesRemaining") is True
        ),
        None,
    )
    if free_index is None:
        raise AdapterError("Recent Match has no verified free entry; no currency or entry token was spent")
    identifier = guid(values3[deck_id["ref"]])
    deck_name = values3[name["ref"]]
    if not identifier.strip("0-") or not isinstance(deck_name, str) or not deck_name:
        raise AdapterError("Recent Match deck identity is unavailable")
    return {
        "ok": True,
        "event_id": event,
        "deck_id": identifier,
        "deck_name": deck_name,
        "format": format_name,
        "recent_count": size,
        "free_fee_index": free_index,
        "free_currency": enum_name(field(items(values3[fees["ref"]])[free_index], "CurrencyType")),
    }


def queue_recent_match(adapter, command, timeout):
    target = recent_match(adapter, timeout)
    for key in ("event_id", "deck_id", "format", "recent_count"):
        if command.get(key) != target[key]:
            raise AdapterError("Recent Match changed before submission; no other entry was selected")
    ops = _Ops()
    home, wrapper, recent = idle_ops(ops)
    ops.expect_member(recent, "Count", target["recent_count"])
    entry = ops.call(recent, "get_Item", I(target["recent_count"] - 1))
    ops.expect_member(entry, "IsQueueEvent", True)
    info = ops.get(entry, "EventInfo")
    ops.expect_member(info, "EventName", target["event_id"])
    ops.expect_member(info, "FormatName", target["format"])
    ops.expect_member(info, "IsLimited", False)
    selected = ops.get(entry, "SelectedDeckInfo")
    identifier = ops.add("get", target=selected, member="deckId", depth=0, retain_value=True)
    adapter._expect_queue_guid(ops, identifier, target["deck_id"])
    deck = ops.get(selected, "deck")
    summary = ops.get(deck, "Summary")
    summary_id = ops.add("get", target=summary, member="DeckId", depth=0, retain_value=True)
    adapter._expect_queue_guid(ops, summary_id, target["deck_id"])
    # A fresh view avoids DeckViewInfo's cached validation after inventory changes.
    view = ops.new("Wizards.Mtga.Decks.DeckViewInfo")
    ops.set(view, "deck", deck)
    display = ops.call(view, "GetValidationForFormat", {"str": target["format"]})
    validation = ops.get(display, "ValidationResult")
    ops.expect_member(validation, "IsValid", True)
    manager = ops.get(wrapper, "EventManager")
    context = ops.call(manager, "GetEventContext", {"str": target["event_id"]})
    player_event = ops.get(context, "PlayerEvent")
    event_info = ops.get(player_event, "EventInfo")
    fees = ops.get(event_info, "EntryFees")
    fee = ops.call(fees, "get_Item", I(target["free_fee_index"]))
    ops.expect_member(fee, "CurrencyType", target["free_currency"])
    ops.expect_member(fee, "Quantity", 0)
    ops.expect_member(fee, "UsesRemaining", True)
    ops.call(home, "JoinMatchMaking", {"str": target["event_id"]}, identifier)
    adapter._run(ops, timeout)
    return {**target, "submitted_type": "RecentMatch"}
