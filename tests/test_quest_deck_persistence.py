"""Server acknowledgements, never bridge cache state, establish persistence."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from arenamcp import quest_deck_persistence as persistence

DECK_ID = "1543b787-1438-460c-bd4a-e61aa4ea185e"
REQUEST_ID = "1c71b2a0-9a66-4c01-b934-85cf00b4a576"
OTHER_ID = "00000000-0000-0000-0000-000000000001"
NAME = "Mono-Red Burn"
MAIN = [(65385, 20), (75525, 4)]
SUMMARY = {
    "DeckId": DECK_ID,
    "Name": NAME,
    "Attributes": [{"name": "Format", "value": "Standard"}],
}


def request() -> dict:
    return {
        "Summary": copy.deepcopy(SUMMARY),
        "Deck": {
            "MainDeck": [{"cardId": card, "quantity": quantity} for card, quantity in MAIN],
            "Sideboard": [],
            "CommandZone": [],
            "Companions": [],
            "CardSkins": [],
        },
        "ActionType": "Updated",
    }


def outgoing(payload: dict | None = None, identifier: str = REQUEST_ID) -> bytes:
    envelope = {"id": identifier, "request": json.dumps(request() if payload is None else payload)}
    return ("[UnityCrossThreadLogger]==> DeckUpsertDeckV3 " + json.dumps(envelope) + "\n").encode()


def incoming(summary: dict | None = None, identifier: str = REQUEST_ID) -> bytes:
    return (
        f"<== DeckUpsertDeckV3({identifier})\n" + json.dumps(SUMMARY if summary is None else summary) + "\n"
    ).encode()


def append(path: Path, data: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(data)


def read(path: Path, checkpoint: persistence.DeckSaveOffset, **kwargs) -> bool:
    expected = dict(deck_id=DECK_ID, name=NAME, main=MAIN, deck_format="Standard")
    expected.update(kwargs)
    return persistence.read_deck_save(path, after_offset=checkpoint, **expected)


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "Player.log"
    path.write_bytes(b"old log contents\n")
    checkpoint = persistence.capture_deck_save_offset(path)
    assert checkpoint is not None
    return path, checkpoint


def test_real_fixture_round_trip_after_checkpoint(tmp_path):
    fixture = Path(__file__).parent / "fixtures/player_log_idle_after_match_20261019.log"
    data = fixture.read_bytes()
    offset = data.index(b"[UnityCrossThreadLogger]==> DeckUpsertDeckV3 ")
    path = tmp_path / "Player.log"
    path.write_bytes(data[:offset])
    checkpoint = persistence.capture_deck_save_offset(path)
    assert checkpoint is not None
    append(path, data[offset:])
    main = [
        (65385, 20),
        (75525, 4),
        (97417, 4),
        (93905, 4),
        (97423, 1),
        (66263, 3),
        (87323, 4),
        (90492, 4),
        (91668, 4),
        (91676, 4),
        (93792, 4),
        (95628, 2),
        (94052, 2),
    ]
    assert read(path, checkpoint, main=main)
    assert not read(path, checkpoint, main=MAIN)
    after_save = persistence.capture_deck_save_offset(path)
    assert not read(path, after_save, main=main)


def test_waits_for_complete_correlated_response(log):
    path, checkpoint = log
    assert not read(path, checkpoint)
    append(path, outgoing())
    assert not read(path, checkpoint)
    append(path, incoming(identifier=OTHER_ID))
    assert not read(path, checkpoint)
    response = incoming()
    append(path, response[:-3])
    assert not read(path, checkpoint)
    append(path, response[-3:])
    assert read(path, checkpoint)
    assert read(path, checkpoint)  # Retrying has no mutable reader state.


def test_cache_or_response_without_request_cannot_confirm(log):
    path, checkpoint = log
    append(path, json.dumps({"CachedDecks": [request()]}).encode() + b"\n" + incoming())
    assert not read(path, checkpoint)


def test_old_request_cannot_match_new_response(tmp_path):
    path = tmp_path / "Player.log"
    path.write_bytes(outgoing())
    checkpoint = persistence.capture_deck_save_offset(path)
    append(path, incoming())
    assert not read(path, checkpoint)


@pytest.mark.parametrize(
    "field,value",
    [
        ("DeckId", OTHER_ID),
        ("Name", "Wrong name"),
        ("Attributes", []),
        ("Attributes", [{"name": "Format", "value": "Historic"}]),
        ("Attributes", [{"name": "Format", "value": "Standard"}] * 2),
    ],
)
@pytest.mark.parametrize("side", ["request", "response"])
def test_identity_and_format_must_match(log, field, value, side):
    path, checkpoint = log
    payload, response = request(), copy.deepcopy(SUMMARY)
    (payload["Summary"] if side == "request" else response)[field] = value
    append(path, outgoing(payload) + incoming(response))
    assert not read(path, checkpoint)


@pytest.mark.parametrize("pile", ["MainDeck", "Sideboard", "CommandZone", "Companions"])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_every_pile_must_match(log, pile, change):
    path, checkpoint = log
    payload = request()
    if change == "missing":
        del payload["Deck"][pile]
    else:
        payload["Deck"][pile].append({"cardId": 75525, "quantity": 1})
    append(path, outgoing(payload) + incoming())
    assert not read(path, checkpoint)


def test_preserved_cosmetics_are_not_gameplay_piles(log):
    path, checkpoint = log
    payload = request()
    payload["Deck"]["CardSkins"] = [{"cardId": 65385, "skinId": "cosmetic"}]
    append(path, outgoing(payload) + incoming())
    assert read(path, checkpoint)


def test_checkpoint_can_round_trip_through_json_journal(log):
    path, checkpoint = log
    restored = persistence.DeckSaveOffset(**json.loads(json.dumps(asdict(checkpoint))))
    append(path, outgoing() + incoming())
    assert read(path, restored)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"error": "save failed"},
        {"ErrorCode": "Timeout"},
        {**SUMMARY, "Error": "failed"},
        {**SUMMARY, "success": False},
        {**SUMMARY, "status": "Failed"},
        {**SUMMARY, "Errors": ["invalid deck"]},
    ],
)
def test_error_response_never_succeeds(log, response):
    path, checkpoint = log
    append(path, outgoing() + incoming(response))
    assert not read(path, checkpoint)


@pytest.mark.parametrize("data", [b"null\n", b"{}\n", b"{broken\n", b"[]\n", b"stack trace\n"])
def test_malformed_response_does_not_borrow_later_json(log, data):
    path, checkpoint = log
    append(path, outgoing() + f"<== DeckUpsertDeckV3({REQUEST_ID})\n".encode())
    append(path, data + json.dumps(SUMMARY).encode() + b"\n")
    assert not read(path, checkpoint)


def test_main_order_and_aggregated_counts_are_semantic(log):
    path, checkpoint = log
    payload = request()
    payload["Deck"]["MainDeck"] = [
        {"cardId": 75525, "quantity": 2},
        {"cardId": 65385, "quantity": 20},
        {"cardId": 75525, "quantity": 2},
    ]
    append(path, outgoing(payload) + incoming())
    assert read(path, checkpoint, main=list(reversed(MAIN)))


@pytest.mark.parametrize("value", [True, "4", 4.0, 0, -4, None])
def test_invalid_quantities_fail_closed(log, value):
    path, checkpoint = log
    payload = request()
    payload["Deck"]["MainDeck"][1]["quantity"] = value
    append(path, outgoing(payload) + incoming())
    assert not read(path, checkpoint)


def test_later_update_supersedes_earlier_success(log):
    path, checkpoint = log
    append(path, outgoing() + incoming())
    assert read(path, checkpoint)
    payload = request()
    payload["Deck"]["Sideboard"] = [{"cardId": 1, "quantity": 1}]
    append(path, outgoing(payload, OTHER_ID) + incoming(identifier=OTHER_ID))
    assert not read(path, checkpoint)


def test_unrelated_deck_does_not_hide_success(log):
    path, checkpoint = log
    payload = request()
    payload["Summary"]["DeckId"] = OTHER_ID
    append(path, outgoing() + outgoing(payload, OTHER_ID) + incoming())
    assert read(path, checkpoint)


def test_duplicate_request_id_is_ambiguous(log):
    path, checkpoint = log
    append(path, outgoing() + outgoing() + incoming())
    assert not read(path, checkpoint)


def test_duplicate_json_keys_fail_closed(log):
    path, checkpoint = log
    response = json.dumps(SUMMARY).replace('"DeckId":', f'"DeckId":"{OTHER_ID}","DeckId":')
    append(path, outgoing() + f"<== DeckUpsertDeckV3({REQUEST_ID})\n{response}\n".encode())
    assert not read(path, checkpoint)


def test_rotation_before_first_read_even_when_replacement_is_larger(log):
    path, checkpoint = log
    path.rename(path.with_suffix(".old"))
    path.write_bytes(b"replacement\n" * 100 + outgoing() + incoming())
    assert not read(path, checkpoint)


def test_rotation_during_verification_fails_closed(log, monkeypatch):
    path, checkpoint = log
    append(path, outgoing() + incoming())
    original_parse = persistence._has_save

    def rotate_after_parse(*args):
        result = original_parse(*args)
        path.rename(path.with_suffix(".old"))
        path.write_bytes(b"rotated\n")
        return result

    monkeypatch.setattr(persistence, "_has_save", rotate_after_parse)
    assert not read(path, checkpoint)


def test_truncation_and_regrowth_fail_closed(log):
    path, checkpoint = log
    path.write_bytes(b"")
    assert not read(path, checkpoint)
    path.write_bytes(b"different prefix\n" + outgoing() + incoming())
    assert not read(path, checkpoint)


def test_missing_log_or_integer_offset_fail_closed(log):
    path, checkpoint = log
    append(path, outgoing() + incoming())
    assert not read(path, checkpoint.offset)
    path.unlink()
    assert not read(path, checkpoint)
    assert persistence.capture_deck_save_offset(path) is None


def test_bounded_window_does_not_search_tail(log, monkeypatch):
    path, checkpoint = log
    monkeypatch.setattr(persistence, "MAX_SCAN_BYTES", 128)
    append(path, b"noise\n" * 100 + outgoing() + incoming())
    assert not read(path, checkpoint)


def test_oversized_line_fails_closed(log, monkeypatch):
    path, checkpoint = log
    monkeypatch.setattr(persistence, "MAX_RECORD_BYTES", 128)
    append(path, outgoing() + incoming())
    assert not read(path, checkpoint)


def test_checkpoint_inside_old_line_cannot_adopt_request(tmp_path):
    path = tmp_path / "Player.log"
    path.write_bytes(b"old partial record")
    checkpoint = persistence.capture_deck_save_offset(path)
    append(path, outgoing() + incoming())
    assert not read(path, checkpoint)


def test_crlf_logs(log):
    path, checkpoint = log
    append(path, (outgoing() + incoming()).replace(b"\n", b"\r\n"))
    assert read(path, checkpoint)


def test_large_old_log_only_reads_bounded_anchors_and_new_window(tmp_path, monkeypatch):
    path = tmp_path / "Player.log"
    path.write_bytes(b"old log\n" * (1024 * 1024))
    original_open = Path.open
    reads = []

    class CountingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __getattr__(self, key):
            return getattr(self.stream, key)

        def read(self, size=-1):
            assert size >= 0, "unbounded read"
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(
        Path, "open", lambda self, *args, **kw: CountingStream(original_open(self, *args, **kw))
    )
    checkpoint = persistence.capture_deck_save_offset(path)
    assert sum(reads) <= 16 * 1024
    append(path, outgoing() + incoming())
    reads.clear()
    assert read(path, checkpoint)
    assert sum(reads) <= len(outgoing() + incoming()) + 32 * 1024
