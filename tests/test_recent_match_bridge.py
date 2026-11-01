"""Synthetic live-model replay. No socket, account, currency, or desktop input."""

from copy import deepcopy

import pytest
from tests.test_quest_queue_bridge import DECK_B_GUID, QueueArena

from arenamcp.mac_bridge_adapter import MacBridgeAdapter


class RecentArena(QueueArena):
    def __init__(self, event="Ladder", format_name="Standard"):
        super().__init__()
        self.validation_format = format_name
        self.recent = self._obj(901, "List", Count=2)
        self.info = self._obj(902, "BladeEventInfo", EventName=event, FormatName=format_name, IsLimited=False)
        self.selected = self._obj(
            903,
            "DeckViewInfo",
            deckId=self.deck_b["Summary"]["DeckId"],
            deckName="Latest deck",
            deck=self.deck_b,
        )
        self.entry = self._obj(
            904, "RecentlyPlayedInfo", IsQueueEvent=True, EventInfo=self.info, SelectedDeckInfo=self.selected
        )
        old = self._obj(
            905,
            "RecentlyPlayedInfo",
            IsQueueEvent=True,
            EventInfo=self._obj(906, "BladeEventInfo", EventName="Old event"),
        )
        self.recent["$items"] = [old, self.entry]
        self.home["_playBladeV3"] = self._obj(
            907, "PlayBladeV3", _data=self._obj(908, "BladeData", RecentlyPlayed=self.recent)
        )
        self.matchmaking = self._obj(909, "Matchmaking", _waitingForMatch=False, _cachedMatchConfig=None)
        self.scene = self._obj(
            910,
            "SceneLoader",
            IsInDuelScene=False,
            IsLoading=False,
            _loadingIndicatorIds=self._obj(911, "List", Count=0),
        )
        self.fee = self._obj(912, "EventEntryFeeInfo", CurrencyType="Free", Quantity=0, UsesRemaining=True)
        self.fees = self._obj(913, "List", Count=1, **{"$items": [self.fee]})
        self.context = self._obj(
            914,
            "EventContext",
            PlayerEvent=self._obj(
                915, "PlayerEvent", EventInfo=self._obj(916, "EventInfo", EntryFees=self.fees)
            ),
        )
        self.event_manager = self._obj(917, "EventManager")
        self.wrapper.update(
            Matchmaking=self.matchmaking, SceneLoader=self.scene, EventManager=self.event_manager
        )

    def _invoke(self, target, method, args):
        if method == "get_Item":
            return target["$items"][args[0]["int"]]
        if method == "GetEventContext":
            assert args == [{"str": self.info["EventName"]}]
            return self.context
        return super()._invoke(target, method, args)


def read(arena):
    return MacBridgeAdapter(arena.send).handle({"action": "get_recent_match"})


def submit(arena, target):
    return MacBridgeAdapter(arena.send).handle({**target, "action": "queue_recent_match"})


@pytest.mark.parametrize(
    "event,format_name",
    [
        ("Ladder", "Standard"),
        ("Play", "Standard"),
        ("Traditional_Explorer", "Explorer"),
        ("Brawl_Ladder", "Brawl"),
        ("AIBotMatch", "Standard"),
    ],
)
def test_featured_last_entry_exact_deck_event_and_validation_format(event, format_name):
    arena = RecentArena(event, format_name)
    target = read(arena)
    assert target["ok"] and target["deck_id"] == DECK_B_GUID
    assert target["event_id"] == event and target["format"] == format_name
    assert not arena.calls  # Reading cannot queue.
    assert submit(arena, target)["ok"]
    assert len(arena.calls) == 1
    assert arena.calls[0][1][0] == {"str": event}
    assert arena.calls[0][1][1] is arena.selected["deckId"]


@pytest.mark.parametrize(
    "kind",
    ["empty", "paid", "event_page", "missing_deck", "limited", "loading", "duel", "queue", "match_ready"],
)
def test_unavailable_first_entry_never_selects_an_older_alternative(kind):
    arena = RecentArena()
    if kind == "empty":
        arena.recent["Count"] = 0
    if kind == "paid":
        arena.fee.update(CurrencyType="Gems", Quantity=1500)
    if kind == "event_page":
        arena.entry["IsQueueEvent"] = False
    if kind == "missing_deck":
        arena.entry["SelectedDeckInfo"] = None
    if kind == "limited":
        arena.info["IsLimited"] = True
    if kind == "loading":
        arena.scene["IsLoading"] = True
    if kind == "duel":
        arena.scene["IsInDuelScene"] = True
    if kind == "queue":
        arena.matchmaking["_waitingForMatch"] = True
    if kind == "match_ready":
        arena.matchmaking["_cachedMatchConfig"] = {}
    assert not read(arena)["ok"]
    assert not arena.calls


@pytest.mark.parametrize(
    "kind", ["changed_event", "changed_deck", "changed_count", "paid", "invalid", "queue"]
)
def test_submission_frame_revalidates_target_and_safety(kind):
    arena = RecentArena()
    target = read(arena)

    def change():
        if kind == "changed_event":
            arena.info["EventName"] = "Another queue"
        if kind == "changed_deck":
            arena.selected["deckId"] = deepcopy(arena.deck_a["Summary"]["DeckId"])
        if kind == "changed_count":
            arena.recent["Count"] += 1
        if kind == "paid":
            arena.fee["Quantity"] = 1
        if kind == "invalid":
            arena.validation_results[110]["IsValid"] = False
        if kind == "queue":
            arena.matchmaking["_waitingForMatch"] = True

    arena.before_submit = change
    assert not submit(arena, target)["ok"]
    assert not arena.calls


def test_changed_recent_entry_between_read_and_request_is_rejected():
    arena = RecentArena()
    target = read(arena)
    arena.info["EventName"] = "Play"
    assert not submit(arena, target)["ok"]
    assert not arena.calls
