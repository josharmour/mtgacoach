"""Client fetch and disk cache (Fable Nov S3).

Behaviour changes are tested against real logged cases:

- Deck analysis prefetch: standalone.log line 339840, 2026-10-04 10:46:41,
  "Starting deck playbook analysis for 99 cards" — the moment a Brawl deck
  list is learned and its card facts should start warming.
- Decks-window prefetch: the deck summaries Player.log records
  (DeckGetDeckSummariesV3 / EventGetCoursesV2 shapes from
  tests/test_deck_library.py) — every known deck warms before a match.
- Opponent reveal prefetch: match ef9761c6, Player.log 2026-10-06 lines
  54327/54590 (trimmed copies in tests/test_snapshot_opponent_facts.py) —
  an Island fetched with "reveal it" becomes a RevealedCardCreated /
  CardRevealed annotation pair.
- Card-facts records: Fury and Chrome Mox from their printed text
  (Scryfall oracle text fetched 2026-11-10, same source as
  tests/test_card_facts_schema.py; Fury's verified rules hash is
  16de70d3bc41dc6b54e586972e52ee768fd860813c36d6d6a23219915bf112f3).

Round-2 regressions (review findings):

- Concurrent re-asks during an in-flight lookup: deck analysis re-fires
  ``facts_for`` on every cycle after standalone.log 2026-10-04 10:46:41
  learns the deck; those re-asks must not requeue an in-flight hash at
  ``now`` and thereby cancel the failure/missing backoff.
- Warm prefetch freshness: a grp id whose record is already cached fresh
  must not be refetched (only stale or missing ones may hit the network).
- Late local-database readiness: startup resolves grp ids before the
  asynchronous card database finishes loading (the window match_context's
  rolling-epoch comment describes); resolution must retry on later epochs
  instead of latching ``Unknown(...)`` forever.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp.card_facts.client import (
    FAILURE_RETRY_S,
    MISSING_RETRY_S,
    REFRESH_AFTER_S,
    CardFactsClient,
    hash_for_card,
)

# ── printed-text cards (see module docstring) ────────────────────────────────

FURY_CARD = {
    "name": "Fury",
    "mana_cost": "{3}{R}{R}",
    "type_line": "Creature — Elemental Incarnation",
    "oracle_text": (
        "Double strike\n"
        "Evoke—Exile a red card from your hand.\n"
        "When Fury enters the battlefield or is evoked, it deals 4 damage "
        "divided among any number of target creatures and/or planeswalkers."
    ),
}

CHROME_MOX_CARD = {
    "name": "Chrome Mox",
    "mana_cost": "{G}",
    "type_line": "Artifact",
    "oracle_text": (
        "Imprint—When Chrome Mox enters the battlefield, you may exile a "
        "nonartifact, nonland card from your hand.\n"
        "{T}: Add one mana of any of the exiled card's colors."
    ),
}

_fury_hash = hash_for_card(FURY_CARD)
_mox_hash = hash_for_card(CHROME_MOX_CARD)
assert _fury_hash is not None and _mox_hash is not None
FURY_HASH: str = _fury_hash
MOX_HASH: str = _mox_hash


def _records(*cards: dict) -> dict[str, dict]:
    """A plausible server reply body mapping each card's hash to a record."""
    return {hash_for_card(card): {"name": card["name"], "effects": [], "confidence": "ok"} for card in cards}


class _Clock:
    """Frozen wall clock the tests advance by hand."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _client(opener, tmp_path: Path, clock: _Clock, resolver=None) -> CardFactsClient:
    kwargs = {} if resolver is None else {"resolver": resolver}
    return CardFactsClient(
        license_key="[REDACTED]",
        base_url="https://facts.example",
        opener=opener,
        cache_dir=tmp_path / "card_facts",
        clock=clock,
        **kwargs,
    )


def _eventually(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


# ── cold cache / warm cache / no waiting / server down / partial reply ───────


def test_cold_cache_fetches_in_background_then_serves_from_memory_and_disk(tmp_path):
    calls: list[list[str]] = []

    def opener(url, body, headers):
        calls.append(json.loads(body)["hashes"])
        assert url.endswith("/v1/card-facts/lookup")
        assert headers["Authorization"].startswith("Bearer ")
        return 200, json.dumps({"found": _records(FURY_CARD)})

    clock = _Clock()
    client = _client(opener, tmp_path, clock)

    # Cold cache: nothing yet, and the call returns without waiting.
    assert client.facts_for([FURY_CARD]) == {}

    def warmed():
        return client.facts_for([FURY_CARD]).get(FURY_HASH) is not None

    _eventually(warmed)
    assert calls == [[FURY_HASH]]
    envelope = json.loads((tmp_path / "card_facts" / f"{FURY_HASH}.json").read_text(encoding="utf-8"))
    assert envelope["record"]["name"] == "Fury"

    # Warm cache: a brand-new client over the same disk cache never opens a
    # socket — the record comes straight from disk.
    def refusing_opener(url, body, headers):
        raise AssertionError("warm cache must not touch the network")

    warm = _client(refusing_opener, tmp_path, clock)
    assert warm.facts_for([FURY_CARD]) == {FURY_HASH: envelope["record"]}


def test_no_call_path_waits_on_the_network(tmp_path):
    release = threading.Event()

    def hung_opener(url, body, headers):
        release.wait(10.0)
        return 200, json.dumps({"found": _records(FURY_CARD)})

    client = _client(hung_opener, tmp_path, _Clock())
    started = time.monotonic()
    result = client.facts_for([FURY_CARD])
    elapsed = time.monotonic() - started
    assert result == {}  # no record yet — and it did not wait for one
    assert elapsed < 2.0
    release.set()


def test_server_down_logs_once_at_info_and_retries_later(tmp_path, caplog):
    attempts: list[int] = []

    def down_opener(url, body, headers):
        attempts.append(1)
        return 0, ""

    clock = _Clock()
    client = _client(down_opener, tmp_path, clock)
    with caplog.at_level("INFO", logger="arenamcp.card_facts.client"):
        assert client.facts_for([FURY_CARD]) == {}
        _eventually(lambda: bool(attempts))
        # The coach plays exactly as today: no exception, simply no record.
        assert client.facts_for([FURY_CARD]) == {}
        # The miss is requeued for FAILURE_RETRY_S later, not spun on.
        with client._wake:
            assert client._want_fetch[FURY_HASH] == clock.now + FAILURE_RETRY_S
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 1
        assert "card facts lookup unavailable" in info_records[0].getMessage()


def test_partial_reply_stores_found_and_retries_the_missing_later(tmp_path):
    def partial_opener(url, body, headers):
        return 200, json.dumps({"found": _records(FURY_CARD)})

    clock = _Clock()
    client = _client(partial_opener, tmp_path, clock)
    both = [FURY_CARD, CHROME_MOX_CARD]
    mox_hash = hash_for_card(CHROME_MOX_CARD)

    assert client.facts_for(both) == {}
    _eventually(lambda: client.facts_for([FURY_CARD]).get(FURY_HASH) is not None)
    # The found half is cached; the missing half is scheduled ~10 min out.
    with client._wake:
        assert client._want_fetch[mox_hash] == clock.now + MISSING_RETRY_S


def test_cached_records_older_than_30_days_refresh_in_the_background(tmp_path):
    calls: list[list[str]] = []

    def opener(url, body, headers):
        calls.append(json.loads(body)["hashes"])
        return 200, json.dumps({"found": _records(FURY_CARD)})

    clock = _Clock()
    client = _client(opener, tmp_path, clock)
    client._store(FURY_HASH, {"name": "Fury", "effects": [], "confidence": "ok"})
    # Fresh record: served as-is, no fetch.
    assert client.facts_for([FURY_CARD])[FURY_HASH]["name"] == "Fury"
    time.sleep(0.05)
    assert calls == []
    # Older than REFRESH_AFTER_S (30 days): still served instantly (never
    # blocks), and a background refresh fetches a new copy.
    clock.now += 31 * 86400.0
    assert client.facts_for([FURY_CARD])[FURY_HASH]["name"] == "Fury"
    _eventually(lambda: calls == [[FURY_HASH]])


# ── hook: deck analysis warms the deck's grp ids ─────────────────────────────


def test_deck_analysis_prefetch_queues_deck_grp_ids_standalone_log_20261004_104641(
    monkeypatch,
):
    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine
    from arenamcp.standalone_deck import _DeckAnalysisMixin

    state, _catalog, _extra = deck_case()
    captured: list[list[int]] = []
    monkeypatch.setattr(
        "arenamcp.card_facts.client.prefetch_grp_ids",
        lambda grp_ids: captured.append(list(grp_ids)),
    )
    workers: list = []
    monkeypatch.setattr(
        "arenamcp.standalone_deck.threading.Thread",
        lambda **kw: Mock(start=lambda: workers.append(kw)),
    )
    runtime = _DeckAnalysisMixin()
    runtime._coach = CoachEngine(Mock())
    runtime._auto_deck_strategy = True
    runtime._is_mulligan_pending = lambda state: False

    assert runtime._maybe_analyze_deck(state)
    # standalone.log 2026-10-04 10:46:41 learns a full deck list here; every
    # distinct grp id plus the commanders must be queued for warming.
    queued = [gid for batch in captured for gid in batch]
    assert set(queued) == {1001, 1002}
    assert queued.count(1001) >= 1  # commander included


# ── hook: the Decks window warms every known deck ────────────────────────────


def test_collect_decks_prefetches_log_deck_grp_ids_player_log_deck_summaries(
    monkeypatch,
):
    from arenamcp.deck_library import DeckRecord
    from arenamcp.standalone_decks import _DeckLibraryMixin

    class _OfflineLibrary(_DeckLibraryMixin):
        def __init__(self, log_decks):
            self._log_decks = log_decks
            self._deck_library = {}

        def _deck_library_bridge(self):
            return None

    record = DeckRecord(
        "b7eda012-97b0-495e-beb9-41fd976eb679",
        "Mono Red",
        format="Standard",
        main=[(1001, 20), (1002, 4)],
        sideboard=[(1003, 2)],
        command_zone=[1004],
        source="log",
    )
    library = _OfflineLibrary({"b7eda012": record})
    monkeypatch.setattr("arenamcp.deck_library.load_log_decks", lambda paths=None: dict(library._log_decks))
    captured: list[list[int]] = []
    monkeypatch.setattr(
        "arenamcp.card_facts.client.prefetch_grp_ids",
        lambda grp_ids: captured.append(list(grp_ids)),
    )

    decks, note = library.collect_decks()
    assert set(decks) == {"b7eda012"} and "bridge offline" in note
    queued = [gid for batch in captured for gid in batch]
    # Maindeck and command zone warm; sideboard cards are not learned here.
    assert set(queued) == {1001, 1002, 1004}


# ── hook: revealed opponent cards warm as they are revealed ──────────────────


def _zone(zone_id: int, zone_type: str, owner: int, ids=None) -> dict:
    zone = {"zoneId": zone_id, "type": f"ZoneType_{zone_type}", "ownerSeatId": owner}
    if ids is not None:
        zone["objectInstanceIds"] = ids
    return zone


def _ann(ann_id: int, ann_type: str, affected: list[int], affector=None, **details) -> dict:
    ann = {"id": ann_id, "affectedIds": affected, "type": [f"AnnotationType_{ann_type}"]}
    if affector is not None:
        ann["affectorId"] = affector
    if details:
        ann["details"] = [
            {"key": key, "type": "KeyValuePairValueType_int32", "valueInt32": [value]}
            for key, value in details.items()
        ]
    return ann


def _event(gsm: dict, seat: int = 1) -> dict:
    return {
        "greToClientEvent": {
            "greToClientMessages": [
                {
                    "type": "GREMessageType_GameStateMessage",
                    "systemSeatIds": [seat],
                    "gameStateMessage": gsm,
                }
            ]
        }
    }


def _revealed_stand_in(instance_id: int, grp_id: int, zone_id: int, owner: int, viewer: int) -> dict:
    return {
        "instanceId": instance_id,
        "grpId": grp_id,
        "type": "GameObjectType_RevealedCard",
        "zoneId": zone_id,
        "visibility": "Visibility_Private",
        "ownerSeatId": owner,
        "controllerSeatId": owner,
        "viewers": [1],
        "overlayGrpId": grp_id,
    }


def test_reveal_annotation_prefetches_revealed_grp_ids_player_log_20261006_ef9761c6(
    monkeypatch,
):
    from arenamcp.gamestate import GameState, create_game_state_handler

    # Match ef9761c6 (Player.log 2026-10-06 lines 54327/54590): they fetch an
    # Island and reveal it; trimmed copies of both GRE messages.
    start_message = {
        "type": "GameStateType_Diff",
        "gameStateId": 1,
        "players": [{"seatId": 1, "penaltyScore": 0}],
        "turnInfo": {"activePlayer": 1, "decisionPlayer": 1},
        "zones": [
            _zone(28, "Hand", 1),
            _zone(30, "Library", 1),
            _zone(31, "Hand", 2),
            _zone(33, "Library", 2),
            _zone(35, "Battlefield", 2),
        ],
        "gameObjects": [],
        "annotations": [],
    }
    fetched_message = {
        "type": "GameStateType_Full",
        "gameStateId": 2,
        "players": [{"seatId": 1, "penaltyScore": 0}],
        "turnInfo": {"activePlayer": 1, "decisionPlayer": 1},
        "zones": [
            _zone(28, "Hand", 1),
            _zone(30, "Library", 1),
            _zone(31, "Hand", 2),
            _zone(33, "Library", 2),
            _zone(35, "Battlefield", 2, ids=[243]),
        ],
        "gameObjects": [_revealed_stand_in(243, 106529, 35, owner=2, viewer=1)],
        "annotations": [
            _ann(
                1334,
                "RevealedCardCreated",
                [245],
                affector=243,
                revealType=0,
                ownerSeatId=2,
            ),
            _ann(
                1335,
                "CardRevealed",
                [243],
                affector=244,
                revealType=0,
                ownerSeatId=2,
            ),
        ],
        "prevGameStateId": 1,
    }

    captured: list[list[int]] = []
    monkeypatch.setattr(
        "arenamcp.card_facts.client.prefetch_grp_ids",
        lambda grp_ids: captured.append(list(grp_ids)),
    )
    state = GameState()
    state._card_name_cache.update({106529: "Island"})
    monkeypatch.setattr(state, "prewarm_card_cache", Mock(return_value=0))

    handler = create_game_state_handler(state)
    for message in (start_message, fetched_message):
        handler(_event(message))

    assert state.revealed_instances[243]["grp_id"] == 106529
    queued = [gid for batch in captured for gid in batch]
    assert queued == [106529]


# ── round 2: in-flight dedup, backoff preservation, warm-prefetch freshness ──


def test_repeated_facts_for_during_in_flight_lookup_preserve_failure_backoff(
    tmp_path,
):
    """Regression (review round 2): deck analysis re-fires ``facts_for`` on
    every cycle after standalone.log 2026-10-04 10:46:41 learns the deck.
    While the first lookup is airborne against a down server, a re-ask used
    to requeue the same hash at ``now``, and the failure handler merged that
    schedule with ``min()``, so the second attempt went out ~1.1s later
    instead of after FAILURE_RETRY_S."""
    entered = threading.Event()
    release = threading.Event()
    attempts: list[int] = []

    def down_opener(url, body, headers):
        attempts.append(1)
        entered.set()
        release.wait(10.0)
        return 503, "service unavailable"

    clock = _Clock()
    client = _client(down_opener, tmp_path, clock)
    assert client.facts_for([FURY_CARD]) == {}
    _eventually(lambda: entered.is_set())

    # A later analysis cycle asks again while the request is airborne: the
    # hash must not be requeued at ``now`` behind the in-flight attempt.
    assert client.facts_for([FURY_CARD]) == {}
    with client._wake:
        assert FURY_HASH not in client._want_fetch

    release.set()
    _eventually(lambda: FURY_HASH in client._want_fetch and not client._in_flight)
    with client._wake:
        # The failure backoff survives the concurrent re-ask intact.
        assert client._want_fetch[FURY_HASH] == clock.now + FAILURE_RETRY_S

    # Less than one backoff interval later there must still be exactly one
    # attempt; the retry only goes out once the deadline passes.
    clock.now += 60.0
    time.sleep(0.1)
    assert len(attempts) == 1
    clock.now += FAILURE_RETRY_S
    # The idle worker sweeps its schedule at most every 30s of wall clock;
    # production retries ride that sweep (or the next notify). Poke it here
    # so the test asserts deadline honouring, not sweep latency.
    with client._wake:
        client._wake.notify()
    _eventually(lambda: len(attempts) == 2)


def test_warm_prefetch_respects_cache_freshness(tmp_path):
    """Regression (review round 2): warm prefetch used to queue every
    resolved hash unconditionally, refetching records that were already
    fresh in memory or on disk. Only stale or missing records may be sent."""
    calls: list[list[str]] = []
    resolved: list[int] = []

    def opener(url, body, headers):
        calls.append(json.loads(body)["hashes"])
        return 200, json.dumps({"found": _records(FURY_CARD)})

    def resolver(gid):
        resolved.append(gid)
        return FURY_CARD if gid == 1001 else CHROME_MOX_CARD

    clock = _Clock()
    client = _client(opener, tmp_path, clock, resolver=resolver)
    client._store(FURY_HASH, {"name": "Fury", "effects": [], "confidence": "ok"})

    # Fresh record already cached: resolution happens, the network does not.
    client.prefetch_grp_ids([1001])
    _eventually(lambda: resolved == [1001])
    time.sleep(0.05)
    assert calls == []

    # Stale record: still served from cache, refreshed exactly once.
    clock.now += 31 * 86400.0
    client.prefetch_grp_ids([1001])
    _eventually(lambda: calls == [[FURY_HASH]])

    # Missing record: fetched exactly once.
    client.prefetch_grp_ids([1002])
    _eventually(lambda: any(MOX_HASH in batch for batch in calls))
    flat = [digest for batch in calls for digest in batch]
    assert flat.count(FURY_HASH) == 1
    assert flat.count(MOX_HASH) == 1


def test_settled_success_clears_in_flight_so_aged_record_refreshes(tmp_path):
    """Regression (review round 2, cold-success-then-aged-refresh repro): a
    settled HTTP 200 used to leave the batch's hashes in ``_in_flight``
    forever, so once the record aged past REFRESH_AFTER_S neither
    ``queue_hashes`` nor ``_cached`` would queue its background refresh and
    the client stopped calling the server after its first success. Every
    settled outcome must clear the in-flight batch while failure and missing
    deadlines survive (the concurrent-backoff regression above covers those)."""
    calls: list[list[str]] = []

    def opener(url, body, headers):
        calls.append(json.loads(body)["hashes"])
        return 200, json.dumps({"found": _records(FURY_CARD)})

    clock = _Clock()
    client = _client(opener, tmp_path, clock)

    # Cold cache: the first lookup succeeds and serves the record.
    assert client.facts_for([FURY_CARD]) == {}
    _eventually(lambda: client.facts_for([FURY_CARD]).get(FURY_HASH) is not None)
    time.sleep(0.05)
    assert calls == [[FURY_HASH]]
    with client._wake:
        assert client._in_flight == []  # settled success cleared the batch

    # Thirty-one days later the record is stale. Nothing re-seeds the cache;
    # the refresh must still be queued and sent.
    clock.now += REFRESH_AFTER_S + 1.0
    assert client.facts_for([FURY_CARD])[FURY_HASH]["name"] == "Fury"
    _eventually(lambda: calls == [[FURY_HASH], [FURY_HASH]])


def test_default_resolver_recovers_when_local_db_becomes_ready_late(monkeypatch):
    """Regression (review round 2): startup resolves grp ids before the
    asynchronous card database has loaded. A fixed epoch latched
    ``Unknown(987654)`` forever; resolution must retry on later rolling
    epochs (match_context's own convention) and pick the card up once the
    database is ready."""
    from arenamcp import match_context
    from arenamcp.card_facts import client as client_module
    from arenamcp.card_facts.client import _default_resolver

    match_context._local_card.cache_clear()

    class _LateSource:
        def __init__(self) -> None:
            self.calls = 0

        def get_card_by_arena_id(self, grp_id):
            self.calls += 1
            if self.calls == 1:
                return None
            return SimpleNamespace(
                name="Fury",
                type_line="Creature — Elemental Incarnation",
                mana_cost="{3}{R}{R}",
                cmc=5.0,
                power="4",
                toughness="4",
                oracle_text=FURY_CARD["oracle_text"],
                related_faces=[],
            )

    class _LateDatabase:
        def __init__(self) -> None:
            self.source = _LateSource()

        @property
        def sources(self):
            return [self.source]

    database = _LateDatabase()
    monkeypatch.setattr("arenamcp.card_db.get_card_database", lambda: database)
    fake_now = [0.0]
    monkeypatch.setattr(
        client_module,
        "time",
        SimpleNamespace(monotonic=lambda: fake_now[0]),
    )

    try:
        # Database not ready yet: an Unknown stand-in, exactly as at startup.
        first = _default_resolver(987654)
        assert first == {"name": "Unknown(987654)"}
        # The database finishes loading; the next rolling epoch re-resolves.
        fake_now[0] = 31.0
        second = _default_resolver(987654)
        assert second is not None
        assert second["name"] == "Fury"
        assert second["oracle_text"] == FURY_CARD["oracle_text"]
        assert database.source.calls == 2
    finally:
        match_context._local_card.cache_clear()
