"""Shared fakes for the draft-autoplay tests: a small 17lands-shaped set, the
driver's bridge and card database, the pick state Arena reports, a scripted
draft screen, and a fake clock for the desktop speech handshake."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from tests.mac_world import Obj, World

from arenamcp.draft_event import DraftEventDriver
from arenamcp.event_course import CourseTracker
from arenamcp.set_primer import SetPrimer, data_primer

# ---------------------------------------------------------------------------
# A small 17lands-shaped set: two good blue cards, two good red, filler, a trap.
# ---------------------------------------------------------------------------


def rating(grp_id, name, color, rarity="C", gih=0.55, ata=6.0, games=1000):
    return {
        "mtga_id": grp_id,
        "name": name,
        "color": color,
        "rarity": {"C": "common", "U": "uncommon", "R": "rare", "M": "mythic"}[rarity],
        "types": ["Creature"],
        "ever_drawn_win_rate": gih,
        "drawn_improvement_win_rate": gih - 0.55,
        "avg_seen": ata + 1,
        "avg_pick": ata,
        "opening_hand_win_rate": gih,
        "ever_drawn_game_count": games,
    }


RATINGS = [
    rating(1, "Blue Ace", "U", "U", 0.62, 2.0),
    rating(2, "Blue Common", "U", "C", 0.58, 4.0),
    rating(3, "Red Ace", "R", "U", 0.61, 2.0),
    rating(4, "Red Common", "R", "C", 0.57, 4.0),
    rating(5, "Izzet Signpost", "UR", "U", 0.60, 3.0),
    rating(6, "Shiny Trap", "B", "R", 0.45, 1.5),
    *[rating(10 + i, f"Filler {i}", "WUBRG"[i % 5], "C", 0.50 + (i % 4) * 0.01, 8.0) for i in range(20)],
]
PAIRS = {
    "UR": SimpleNamespace(win_rate=0.58, games=9000),
    "WB": SimpleNamespace(win_rate=0.52, games=7000),
    "BG": SimpleNamespace(win_rate=0.55, games=8000),
}


def make_primer() -> SetPrimer:
    return data_primer("TST", RATINGS, PAIRS, lambda grp, name: {"oracle_text": f"{name} text", "cmc": 3.0})


def draft_world(ok=True, reserved=0, human=True) -> World:
    world = World()
    cards = [world.add(200 + i, "CardData", GrpId=grp, TitleId=grp * 10) for i, grp in enumerate((1, 3, 12))]
    views = [world.add(300 + i, "DraftPackCardView", Card=Obj(card)) for i, card in enumerate(cards)]
    holder = world.add(400, "DraftPackHolder", IsAnimating=False, CardViews=[Obj(v) for v in views])
    manager = world.add(500, "DraftDeckManager")
    world.results[(manager, "ReservedCardCount")] = reserved
    info = world.add(610, "PickInfo", SelfPack=1, SelfPick=4)
    pod_class = "Wotc.Mtga.Wrapper.Draft.HumanDraftPod" if human else "Wotc.Mtga.Wrapper.Draft.BotDraftPod"
    pod = world.add(
        600,
        pod_class,
        PickNumCardsToTake=1,
        _currentPickInfo=Obj(info),
        PickSecondsRemaining=50,
        _currentPack=0,
        _currentPick=3,
    )
    world.finds["Wotc.Mtga.Wrapper.Draft.DraftContentController"] = world.add(
        100,
        "DraftContentController",
        _okToPickCard=ok,
        _draftPackHolder=Obj(holder),
        DraftPod=Obj(pod),
        _draftDeckManager=Obj(manager),
    )
    return world


class FakeBridge:
    connected = True

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.sent: list[tuple[str, dict]] = []

    def draft_command(self, action, **fields):
        self.sent.append((action, fields))
        reply = self.replies.get(action, {"ok": False, "error": "unscripted"})
        return reply(fields) if callable(reply) else reply


class FakeDB:
    names = {1: "Blue Ace", 3: "Red Ace", 12: "Filler 2", 2: "Blue Common"}

    def get_card(self, grp_id):
        return SimpleNamespace(name=self.names.get(grp_id, f"Card {grp_id}"), expansion_code="TST")


def driver_for(bridge, primer, **kwargs) -> DraftEventDriver:
    service = SimpleNamespace(get=lambda code: primer, ensure=lambda code: None, quick=lambda code: primer)
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: CourseTracker(),
        primer_service=service,
        card_db=FakeDB(),
        **kwargs,
    )
    driver.set_enabled(True)
    return driver


PICK_STATE = {
    "ok": True,
    "is_open": True,
    "ok_to_pick": True,
    "animating": False,
    "reserved_count": 0,
    "pick_num_cards_to_take": 1,
    "pack_number": 1,
    "pick_number": 1,
    "pick_seconds_remaining": 10,
    "pack_cards": [12, 1, 3],
    "pack_views": [{"grp_id": g, "title_id": g * 10} for g in (12, 1, 3)],
}


class FakeTime:
    """A clock that only moves when a wait or sleep advances it.

    ``at(delay, fn)`` schedules ``fn`` (e.g. a desktop speech acknowledgment)
    to run once the clock reaches ``now + delay``. Pass ``advance`` as
    ``SpeechCompletion(idle=...)`` and as ``sleep=``.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self._due: list[tuple[float, int, Callable[[], Any]]] = []
        self._seq = 0

    def __call__(self) -> float:
        return self.now

    def at(self, delay: float, fn: Callable[[], Any]) -> None:
        self._seq += 1
        self._due.append((self.now + delay, self._seq, fn))

    def advance(self, seconds: float) -> None:
        self.now += seconds
        while self._due and min(self._due)[0] <= self.now + 1e-9:
            entry = min(self._due)
            self._due.remove(entry)
            entry[2]()
