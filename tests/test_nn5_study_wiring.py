"""Tests for NN5: wiring G2 deck studies into standalone_decks, coach, synergy_tracker, and game_plan."""

from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_deck_study import native_catalog
from tests.test_g2_g4_e3_1117 import _FakeStudyBackend, _study_catalog

from arenamcp import standalone_decks
from arenamcp.action_planner import ActionPlanner
from arenamcp.coach import CoachEngine
from arenamcp.decisions import build_pending_decision
from arenamcp.deck_library import DeckRecord
from arenamcp.deck_strategy import (
    LEGACY_PLAYBOOK_VERSION,
    PLAYBOOK_VERSION,
    DeckPlaybook,
    commander_ids,
    deck_identity,
)
from arenamcp.deck_study import (
    STUDY_VERSION,
    build_deck_study,
    current_study_sources_match,
    deck_study_hash,
)
from arenamcp.match_packets import MatchPacket
from arenamcp.standalone_decks import _DeckLibraryMixin, _playbook_has_study
from arenamcp.synergy_tracker import (
    entries_from_playbook,
    evaluate_synergies,
    tutor_missing_piece_hints,
)


def _studied_fixture(monkeypatch, tmp_path):
    """Build a deterministic 4-card Brawl deck, legacy v2 response, and valid G2 study+chapters."""
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    raw_catalog, commanders = _study_catalog()
    # CardInfo from MTGADatabaseAdapter does not carry a separate loyalty attribute;
    # keep the catalog aligned with native_catalog so real source verification runs end-to-end.
    catalog = {gid: {k: v for k, v in card.items() if k != "loyalty"} for gid, card in raw_catalog.items()}
    native_catalog(monkeypatch, catalog)
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, _epoch=0: catalog.get(gid, {}))

    record = DeckRecord(
        deck_id="nn5-deck",
        name="Minsc NN5",
        format="Brawl",
        main=[(93958, 1), (92102, 1), (75553, 1)],
        command_zone=[81841],
        last_played="2026-11-18T10:00:00Z",
    )
    state = record.game_state()
    state["format_name"] = "Brawl"
    ident = record.identity()
    assert ident == deck_identity(state)

    legacy_response = {
        "archetype": "Minsc haste pump",
        "spoken_summary": "Ramp with Ruby into Minsc & Boo to push trample damage.",
        "primary_plan": "Ramp into Minsc & Boo and pump hasty creatures.",
        "backup_plan": "Attack with Veteran Survivor and exile opponent graveyard threats.",
        "card_roles": {
            "81841": "Designated commander and primary pump engine.",
            "93958": "Haste mana dork that immediately curves into Minsc.",
            "92102": "Early pressure and graveyard hate.",
        },
        "phases": {
            "early": "Play Ruby to accelerate mana.",
            "mid": "Deploy Minsc & Boo and pump hasty attackers.",
            "late": "Close with oversized trample combat.",
            "recovery": "Recast Minsc & Boo using surviving mana.",
        },
        "commanders": [
            {
                "card": 81841,
                "deployment": "Turn 3 off Ruby.",
                "ongoing_value": "Repeatable +1/+1 counters on haste creatures.",
                "replay_value": "New Boo token on entry.",
                "resource_comparison": {
                    "starting_resources": "Minsc plus Ruby and lands.",
                    "preserve_original": "Protect Minsc over chumping.",
                    "spend_and_recover": "Recast Minsc if swept.",
                    "decision_test": "Keep Minsc when pumping produces lethal.",
                },
                "preserve_or_reuse": "Preserve Minsc when Ruby is active.",
                "constraints": "+1 targets trample or haste creatures only.",
            }
        ],
        "mechanisms": [
            {
                "id": "engine_1",
                "cards": [81841, 93958],
                "effect": "Haste mana dork curves into Minsc +1 pump.",
                "requires": ["Ruby and Minsc on battlefield"],
                "avoid": ["Tapping Ruby into open instant removal before combat"],
                "evidence": [{"card": 81841, "rule": 1}, {"card": 93958, "rule": 1}],
            }
        ],
        "decision_rules": [],
    }
    legacy_book = DeckPlaybook.parse(json.dumps(legacy_response), catalog, commander_ids(state), ident)
    assert legacy_book.version == LEGACY_PLAYBOOK_VERSION

    study_backend = _FakeStudyBackend()
    study_res = build_deck_study(
        catalog,
        commanders,
        study_backend,
        format_name="Brawl",
        deck_counts={gid: 1 for gid in catalog},
        batch_size=8,
    )
    dhash = deck_study_hash(catalog, commanders, counts={gid: 1 for gid in catalog})
    return {
        "catalog": catalog,
        "commanders": commanders,
        "record": record,
        "state": state,
        "identity": ident,
        "legacy_response": legacy_response,
        "legacy_book": legacy_book,
        "study_res": study_res,
        "dhash": dhash,
    }


class _Library(_DeckLibraryMixin):
    def __init__(self, coach, record, *, study_client=None, backend_factory=None, in_match=None):
        self._coach = coach
        self._deck_library = {record.deck_id: record}
        self._study_client = study_client
        self._backend_factory = backend_factory
        self._in_match_fn = in_match or (lambda: False)
        self._auto_deck_strategy = True
        self._backend_name = "online"
        self.model_name = "test-model"
        self.ui = Mock()

    def collect_decks(self):
        return dict(self._deck_library), ""

    def _deck_library_in_match(self):
        return bool(self._in_match_fn())

    def _deck_library_backend(self):
        if self._backend_factory is not None:
            return self._backend_factory()
        raise AssertionError("_deck_library_backend should not be called here")


class _MemoryStudyClient:
    def __init__(self, hits: dict[str, dict] | None = None):
        self.hits = dict(hits or {})
        self.fetches: list[tuple[str, int]] = []
        self.uploads: list[tuple[str, int, str]] = []

    def fetch_study(self, deck_hash: str, *, version: int = STUDY_VERSION):
        self.fetches.append((deck_hash, version))
        return self.hits.get(deck_hash)

    def upload_study(
        self, deck_hash: str, payload: dict, *, version: int = STUDY_VERSION, model_id: str = ""
    ):
        self.uploads.append((deck_hash, version, model_id))
        self.hits[deck_hash] = payload
        return True


def test_nn5_server_study_hit_upgrades_legacy_idle_playbook_with_zero_llm_calls(monkeypatch, tmp_path):
    fx = _studied_fixture(monkeypatch, tmp_path)
    server_payload = {"study": fx["study_res"].study, "chapters": fx["study_res"].chapters}

    for entry_kind in ("selected", "sweep", "analyze_record", "analyze_all"):
        # Reset isolated sandbox dir for each entrypoint so cache/disk persistence is tested cleanly.
        sub_dir = tmp_path / entry_kind
        sub_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", sub_dir)

        coach = CoachEngine(backend=Mock())
        coach._store_playbook(fx["legacy_book"], mode="idle")
        if entry_kind in ("selected", "analyze_all"):
            # Also verify saved deck_strategies/<id>.json gets upgraded and not left stale.
            coach.save_deck_strategy(fx["legacy_book"])

        client = _MemoryStudyClient({fx["dhash"]: server_payload})
        backend_calls = 0

        def make_backend():
            nonlocal backend_calls
            backend_calls += 1
            return Mock()

        lib = _Library(coach, fx["record"], study_client=client, backend_factory=make_backend)

        if entry_kind == "selected":
            monkeypatch.setattr("arenamcp.server.get_last_queue_selection", lambda: {"deck_id": "nn5-deck"})
            lib._prepare_selected_deck(threading.Event())
        elif entry_kind == "sweep":
            lib._build_missing_playbooks(threading.Event())
        elif entry_kind == "analyze_record":
            res = lib.analyze_deck_record(fx["record"], refresh=False)
            assert res["status"] == "ready"
        else:
            monkeypatch.setattr(standalone_decks._DeckLibraryMixin, "_ANALYZE_ALL_MATCH_POLL_S", 0.001)
            assert lib.analyze_all_decks_async() is True
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and standalone_decks._ANALYZE_ALL_LOCK.locked():
                time.sleep(0.01)
            assert not standalone_decks._ANALYZE_ALL_LOCK.locked()

        assert backend_calls == 0
        assert coach._backend.complete.call_count == 0
        assert any(h == fx["dhash"] and v == STUDY_VERSION for h, v in client.fetches)

        # Fresh CoachEngine restores the upgraded v4 playbook from disk with zero LLM calls.
        restarted = CoachEngine(backend=Mock())
        restored = restarted._cached_playbook(fx["identity"], fx["state"])
        assert _playbook_has_study(restored)
        assert restored.version == PLAYBOOK_VERSION
        assert restored.data["study"] == fx["study_res"].study
        assert restored.data["study_chapters"] == fx["study_res"].chapters
        assert restarted._backend.complete.call_count == 0


def test_nn5_idle_study_build_on_server_miss_for_legacy_and_missing_playbook(monkeypatch, tmp_path):
    fx = _studied_fixture(monkeypatch, tmp_path)

    # Case 1: Existing legacy idle playbook + server miss -> builds study+chapters only (0 legacy passes), closes backend.
    dir_legacy = tmp_path / "legacy_miss"
    dir_legacy.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_legacy)
    coach1 = CoachEngine(backend=Mock())
    coach1._store_playbook(fx["legacy_book"], mode="idle")

    study_backend1 = _FakeStudyBackend()
    closed1: list[bool] = []
    study_backend1.close = lambda: closed1.append(True)
    client1 = _MemoryStudyClient()
    lib1 = _Library(coach1, fx["record"], study_client=client1, backend_factory=lambda: study_backend1)

    res1 = lib1.analyze_deck_record(fx["record"], refresh=False)
    assert res1["status"] == "ready"
    assert closed1 == [True]
    assert study_backend1.calls >= 4
    assert any(h == fx["dhash"] for h, _v, _m in client1.uploads)

    fresh1 = CoachEngine(backend=Mock())
    restored1 = fresh1._cached_playbook(fx["identity"], fx["state"])
    assert _playbook_has_study(restored1)
    assert restored1.version == PLAYBOOK_VERSION

    # Case 2: Missing playbook + server miss -> builds playbook AND full G2 study+chapters, closes backend.
    dir_missing = tmp_path / "missing_miss"
    dir_missing.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_missing)
    coach2 = CoachEngine(backend=Mock())

    closed2: list[bool] = []

    class _FullIdleBackend(_FakeStudyBackend):
        def complete(self, system: str, user: str, **kwargs) -> str:
            if "DECK_STUDY_PASS:" in system:
                return super().complete(system, user, **kwargs)
            if "AUDIT THE PROPOSED PLAYBOOK" in user:
                return json.dumps(fx["legacy_response"])
            return "Discovery notes for Minsc haste pump."

        def close(self):
            closed2.append(True)

    full_backend = _FullIdleBackend()
    client2 = _MemoryStudyClient()
    lib2 = _Library(coach2, fx["record"], study_client=client2, backend_factory=lambda: full_backend)
    lib2._build_missing_playbooks(threading.Event())
    assert closed2 == [True]
    assert any(h == fx["dhash"] for h, _v, _m in client2.uploads)

    fresh2 = CoachEngine(backend=Mock())
    restored2 = fresh2._cached_playbook(fx["identity"], fx["state"])
    assert _playbook_has_study(restored2)
    assert restored2.version == PLAYBOOK_VERSION

    # Case 3: Concurrent idle workers share single-flight lock so study build only runs once.
    dir_conc = tmp_path / "concurrent"
    dir_conc.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_conc)
    coach3 = CoachEngine(backend=Mock())
    coach3._store_playbook(fx["legacy_book"], mode="idle")

    entered = threading.Event()
    release = threading.Event()

    class _BlockingStudyBackend(_FakeStudyBackend):
        def complete(self, system: str, user: str, **kwargs) -> str:
            if not entered.is_set():
                entered.set()
                assert release.wait(3.0)
            return super().complete(system, user, **kwargs)

    conc_backend = _BlockingStudyBackend()
    client3 = _MemoryStudyClient()
    results3: list[DeckPlaybook] = []
    errors3: list[Exception] = []

    def worker():
        try:
            results3.append(
                coach3.prepare_playbook(
                    fx["state"],
                    backend=conc_backend,
                    study_client=client3,
                    build_full_study=True,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted empty below
            errors3.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert entered.wait(2.0)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join(3.0)
    t2.join(3.0)
    assert not errors3
    assert len(results3) == 2
    assert all(_playbook_has_study(pb) for pb in results3)
    # _FakeStudyBackend ran exactly once (cards + discovery + sequencing + switches = 4 calls).
    assert conc_backend.calls == 4


def test_nn5_match_start_cancels_idle_study_build_without_persisting_partial_study(monkeypatch, tmp_path):
    fx = _studied_fixture(monkeypatch, tmp_path)

    # 1. cancel.set() mid-study build aborts without persisting study.
    dir_cancel = tmp_path / "cancel_set"
    dir_cancel.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_cancel)
    coach1 = CoachEngine(backend=Mock())
    coach1._store_playbook(fx["legacy_book"], mode="idle")
    cancel1 = threading.Event()

    class _CancelMidStudyBackend(_FakeStudyBackend):
        def complete(self, system: str, user: str, **kwargs) -> str:
            out = super().complete(system, user, **kwargs)
            cancel1.set()
            return out

    be1 = _CancelMidStudyBackend()
    client1 = _MemoryStudyClient()
    with pytest.raises(ValueError, match="paused for active play"):
        coach1.prepare_playbook(
            fx["state"], backend=be1, cancel=cancel1, study_client=client1, build_full_study=True
        )
    assert be1.calls == 1  # Aborted right after Pass 1; Pass 2-4 never ran.
    assert client1.uploads == []
    assert not _playbook_has_study(CoachEngine(backend=Mock())._cached_playbook(fx["identity"], fx["state"]))

    # 2. in_match() -> True aborts both beforehand (0 backend/network) and mid-build.
    dir_match = tmp_path / "in_match"
    dir_match.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_match)
    coach2 = CoachEngine(backend=Mock())
    coach2._store_playbook(fx["legacy_book"], mode="idle")
    in_match_flag = [True]
    client2 = _MemoryStudyClient()
    backend_created: list[bool] = []
    lib2 = _Library(
        coach2,
        fx["record"],
        study_client=client2,
        backend_factory=lambda: backend_created.append(True) or _FakeStudyBackend(),
        in_match=lambda: in_match_flag[0],
    )
    assert lib2._prepare_playbook_with_study(fx["state"]) is None
    assert backend_created == [] and client2.fetches == []
    err_res = lib2.analyze_deck_record(fx["record"], refresh=False)
    assert err_res["status"] == "error" and "active play" in err_res["error"]

    # Flip in_match() to True inside the final study pass -> nothing partial stored even after build_deck_study returns.
    in_match_flag[0] = False

    class _FlipMatchOnLastPassBackend(_FakeStudyBackend):
        def complete(self, system: str, user: str, **kwargs) -> str:
            out = super().complete(system, user, **kwargs)
            if "DECK_STUDY_PASS: switches" in system:
                in_match_flag[0] = True
            return out

    closed2: list[bool] = []
    be2 = _FlipMatchOnLastPassBackend()
    be2.close = lambda: closed2.append(True)
    lib2_mid = _Library(
        coach2,
        fx["record"],
        study_client=_MemoryStudyClient(),
        backend_factory=lambda: be2,
        in_match=lambda: in_match_flag[0],
    )
    mid_res = lib2_mid.analyze_deck_record(fx["record"], refresh=False)
    assert mid_res["status"] == "error" and "paused for active play" in mid_res["error"]
    assert closed2 == [True]
    assert not _playbook_has_study(CoachEngine(backend=Mock())._cached_playbook(fx["identity"], fx["state"]))

    # 3. _poll_deck_preparation(idle=False) cancels an active idle study build immediately.
    dir_poll = tmp_path / "poll_cancel"
    dir_poll.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_poll)
    coach3 = CoachEngine(backend=Mock())
    coach3._store_playbook(fx["legacy_book"], mode="idle")
    entered = threading.Event()
    release = threading.Event()

    class _SlowPassBackend(_FakeStudyBackend):
        def complete(self, system: str, user: str, **kwargs) -> str:
            entered.set()
            release.wait(3.0)
            return super().complete(system, user, **kwargs)

    be3 = _SlowPassBackend()
    closed3: list[bool] = []
    be3.close = lambda: closed3.append(True)
    lib3 = _Library(coach3, fx["record"], study_client=_MemoryStudyClient(), backend_factory=lambda: be3)
    monkeypatch.setattr("arenamcp.server.get_last_queue_selection", lambda: {"deck_id": "nn5-deck"})
    lib3._poll_deck_preparation(True)
    assert entered.wait(2.0)

    lib3._poll_deck_preparation(False, fx["state"])
    assert lib3._deck_prep_cancel.is_set()
    release.set()
    lib3._deck_prep_thread.join(3.0)
    assert not lib3._deck_prep_thread.is_alive()
    assert closed3 == [True]
    assert be3.calls == 1
    assert not _playbook_has_study(CoachEngine(backend=Mock())._cached_playbook(fx["identity"], fx["state"]))


def test_nn5_live_match_analyze_deck_never_builds_study_and_restores_cached_study(monkeypatch, tmp_path):
    fx = _studied_fixture(monkeypatch, tmp_path)

    # 1. Cold cache: live match analyze_deck runs only the 2-pass playbook build, never build_deck_study.
    monkeypatch.setattr(
        "arenamcp.deck_study.build_deck_study",
        lambda *a, **kw: pytest.fail("analyze_deck must never build a full G2 study in-match"),
    )
    live_backend = Mock()
    live_backend.complete.side_effect = ["live discovery notes", json.dumps(fx["legacy_response"])]
    live_coach = CoachEngine(backend=live_backend)
    strategy = live_coach.analyze_deck(fx["state"])
    assert strategy is not None
    assert live_backend.complete.call_count == 2
    assert live_coach._deck_playbook is not None
    assert not _playbook_has_study(live_coach._deck_playbook)

    # 2. Warm idle-studied cache: fresh CoachEngine.analyze_deck loads v4 study with 0 LLM calls.
    studied_dir = tmp_path / "warm_studied"
    studied_dir.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", studied_dir)

    idle_coach = CoachEngine(backend=Mock())
    studied = fx["legacy_book"].with_current_study(fx["study_res"].study, fx["study_res"].chapters)
    idle_coach._store_playbook(studied, mode="idle")

    match_backend = Mock()
    match_coach = CoachEngine(backend=match_backend)
    restored_strategy = match_coach.analyze_deck(fx["state"])
    assert restored_strategy is not None
    assert match_backend.complete.call_count == 0
    assert _playbook_has_study(match_coach._deck_playbook)
    assert match_coach._ensure_game_plan_mgr()._playbook_source() is match_coach._deck_playbook


def test_nn5_synergy_tracker_live_one_step_tutor_and_out_of_reach_controls(monkeypatch, tmp_path):
    fx = _studied_fixture(monkeypatch, tmp_path)
    studied = fx["legacy_book"].with_current_study(fx["study_res"].study, fx["study_res"].chapters)
    entries = entries_from_playbook(studied, deck_cards=fx["state"]["deck_cards"])
    assert [e.id for e in entries] == ["ruby-minsc-pump"]

    base = {
        **fx["state"],
        "turn": {"turn_number": 4, "phase": "Phase_Main1", "active_player": 1},
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "command": [],
        "battlefield": [
            {
                "instance_id": 10,
                "grp_id": 81841,
                "name": "Minsc & Boo, Timeless Heroes",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Legendary Planeswalker — Minsc",
            },
            {
                "instance_id": 11,
                "grp_id": 93958,
                "name": "Ruby, Daring Tracker",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Legendary Creature — Human Scout",
            },
        ],
    }

    # Live control: both pieces on battlefield, turn >= first_turn (3), mana 0 -> live.
    snap_live = evaluate_synergies(entries, base, catalog=fx["catalog"])
    assert [s["id"] for s in snap_live.live] == ["ruby-minsc-pump"]
    assert snap_live.one_step_away == ()
    assert snap_live.out_of_reach == ()

    # One-step-away control: Ruby still in library (removed from battlefield, remains in deck_cards).
    one_step_state = {
        **base,
        "battlefield": [c for c in base["battlefield"] if c["grp_id"] != 93958],
    }
    snap_step = evaluate_synergies(entries, one_step_state, catalog=fx["catalog"])
    assert snap_step.live == ()
    assert len(snap_step.one_step_away) == 1
    assert snap_step.one_step_away[0]["missing_names"] == ["Ruby, Daring Tracker"]
    assert snap_step.one_step_away[0]["copies_in_library"] == 1

    # Tutor hints fire on Search and discard windows, and stay quiet on unrelated ActionsAvailable.
    search_hints = tutor_missing_piece_hints(snap_step, one_step_state, request_type="Search")
    assert len(search_hints) == 1
    assert "Ruby, Daring Tracker" in search_hints[0] and "1 copy/copies in library" in search_hints[0]

    discard_hints = tutor_missing_piece_hints(
        snap_step,
        {**one_step_state, "decision_context": {"type": "discard"}},
        request_type="SelectN",
    )
    assert any("Ruby, Daring Tracker" in h for h in discard_hints)
    assert any("Protect held synergy piece(s)" in h and "Minsc & Boo" in h for h in discard_hints)

    assert tutor_missing_piece_hints(snap_step, one_step_state, request_type="ActionsAvailable") == []

    # Out-of-reach opposite control: Ruby exiled (0 copies left in library) -> neither live nor 1-step.
    exiled_state = {
        **one_step_state,
        "exile": [
            {
                "instance_id": 11,
                "grp_id": 93958,
                "name": "Ruby, Daring Tracker",
                "owner_seat_id": 1,
                "type_line": "Legendary Creature — Human Scout",
            }
        ],
    }
    snap_exiled = evaluate_synergies(entries, exiled_state, catalog=fx["catalog"])
    assert snap_exiled.live == ()
    assert snap_exiled.one_step_away == ()
    assert [s["id"] for s in snap_exiled.out_of_reach] == ["ruby-minsc-pump"]
    assert snap_exiled.out_of_reach[0]["reason"] == "piece_gone"


def test_nn5_end_to_end_game_plan_render_to_dict_packet_and_exile_opposite_control(monkeypatch, tmp_path):
    fx = _studied_fixture(monkeypatch, tmp_path)
    studied = fx["legacy_book"].with_current_study(fx["study_res"].study, fx["study_res"].chapters)

    coach = CoachEngine(backend=Mock())
    coach._store_playbook(studied, mode="idle")
    assert coach.analyze_deck(fx["state"]) is not None
    assert _playbook_has_study(coach._deck_playbook)

    mgr = coach._ensure_game_plan_mgr()
    plan_json = json.dumps(
        {
            "role": "aggressor",
            "win_conditions": ["Pump Ruby with Minsc & Boo"],
            "path": "Curve Ruby into Minsc & Boo and push lethal trample",
            "threat": "Instant removal",
            "develop_next": "Activate Minsc +1 on Ruby",
            "turns": [
                {"turn": "T", "land": "", "cast": [], "attack": "Ruby", "hold": ""},
                {"turn": "T+1", "land": "", "cast": [], "attack": "Ruby", "hold": ""},
                {"turn": "T+2", "land": "", "cast": [], "attack": "Ruby", "hold": ""},
            ],
            "switch_if": ["Minsc removed -> beat down with Survivor"],
        }
    )
    mgr._backend = SimpleNamespace(complete=lambda *a, **kw: plan_json, model="local")

    live_state = {
        **fx["state"],
        "match_id": "nn5-match-1",
        "turn": {"turn_number": 4, "phase": "Phase_Main1", "step": "Step_Main1", "active_player": 1},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "hand_size": 0},
            {"seat_id": 2, "is_local": False, "life_total": 20, "hand_size": 3},
        ],
        "battlefield": [
            {
                "instance_id": 10,
                "grp_id": 81841,
                "name": "Minsc & Boo, Timeless Heroes",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Legendary Planeswalker — Minsc",
                "oracle_text": fx["catalog"][81841]["oracle_text"],
            },
            {
                "instance_id": 11,
                "grp_id": 93958,
                "name": "Ruby, Daring Tracker",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Legendary Creature — Human Scout",
                "mana_cost": "{R}{G}",
                "oracle_text": fx["catalog"][93958]["oracle_text"],
                "power": "1",
                "toughness": "2",
            },
            {
                "instance_id": 12,
                "grp_id": 75553,
                "name": "Forest",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Basic Land — Forest",
                "is_tapped": False,
            },
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "command": [],
        "stack": [],
        "legal_actions_raw": [],
        "active_threat_classes": ["aggro_burn"],
    }

    plan = mgr.maybe_reform(live_state)
    assert plan is not None
    assert [s["id"] for s in plan.live_synergies] == ["ruby-minsc-pump"]
    assert plan.one_step_synergies == []
    assert [sw["threat_class"] for sw in plan.active_switches] == ["aggro_burn"]

    rendered = plan.render()
    assert "Live synergies:" in rendered
    assert "Minsc & Boo, Timeless Heroes + Ruby, Daring Tracker" in rendered
    assert "Active matchup switch: aggro_burn:" in rendered

    exported = plan.to_dict()
    assert [s["id"] for s in exported["live_synergies"]] == ["ruby-minsc-pump"]
    assert exported["one_step_synergies"] == []
    json.dumps(exported)

    # Wire ActionPlanner + MatchPacket and confirm live synergy reaches _synergy_groups & packet trace.
    planner_backend = Mock()
    planner_backend.complete.return_value = '{"option_ids":["pass"],"reasoning":"Hold mana."}'
    planner = ActionPlanner(planner_backend, deck_playbook_fn=lambda: coach._deck_playbook)
    planner.set_game_plan_source(mgr.strategy_block, step_source=mgr.current_step)

    enriched = planner._with_live_synergies(live_state)
    assert enriched.get("_synergy_groups") == [[81841, 93958]]

    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [{"actionType": "ActionType_Pass"}],
        }
    )
    assert planner.plan_decision_options(decision, deepcopy(live_state)) == ["pass"]
    trace = planner.get_last_decision_trace()
    assert "Live synergies:" in trace.get("plan_block", "")

    packet = MatchPacket("nn5-match-1")
    packet.add_decision(decision, ["pass"], trace=trace, game_state=live_state)
    saved_path = packet.save(packets_dir=tmp_path / "packets")
    assert saved_path is not None
    saved_packet = json.loads(saved_path.read_text(encoding="utf-8"))
    assert "Live synergies:" in saved_packet["decisions"][0]["trace"]["plan_block"]

    # Also verify 1-step synergy rendering on GamePlan when Ruby is still in library.
    one_step_state = {
        **live_state,
        "turn": {"turn_number": 5, "phase": "Phase_Main1", "step": "Step_Main1", "active_player": 1},
        "battlefield": [c for c in live_state["battlefield"] if c["grp_id"] != 93958],
    }
    mgr.observe(one_step_state)
    assert plan.live_synergies == []
    assert [s["id"] for s in plan.one_step_synergies] == ["ruby-minsc-pump"]
    assert "One step away:" in plan.render()
    assert "missing: Ruby, Daring Tracker (1 in library)" in plan.render()
    assert [s["id"] for s in plan.to_dict()["one_step_synergies"]] == ["ruby-minsc-pump"]

    # Opposite control: Ruby is exiled -> disappears from live_synergies, one_step_synergies, render, and to_dict.
    exiled_state = {
        **one_step_state,
        "turn": {"turn_number": 6, "phase": "Phase_Main1", "step": "Step_Main1", "active_player": 1},
        "exile": [
            {
                "instance_id": 11,
                "grp_id": 93958,
                "name": "Ruby, Daring Tracker",
                "owner_seat_id": 1,
                "type_line": "Legendary Creature — Human Scout",
            }
        ],
    }
    mgr.observe(exiled_state)
    assert plan.live_synergies == []
    assert plan.one_step_synergies == []
    assert "Live synergies:" not in plan.render()
    assert "One step away:" not in plan.render()
    assert plan.to_dict()["live_synergies"] == []
    assert plan.to_dict()["one_step_synergies"] == []
    assert "_synergy_groups" not in planner._with_live_synergies(exiled_state)


def test_nn5_study_withheld_not_stored_and_fetch_deduplicated_and_registered(monkeypatch, tmp_path):
    fx = _studied_fixture(monkeypatch, tmp_path)

    # 1. study_withheld=True is NOT written to cache/disk and clean attachment is stored once.
    dir_withheld = tmp_path / "withheld"
    dir_withheld.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_withheld)
    coach1 = CoachEngine(backend=Mock())
    stored_calls: list[tuple[str, bool]] = []
    orig_store = coach1._store_playbook

    def spy_store(pb, mode="idle"):
        stored_calls.append((mode, bool(getattr(pb, "study_withheld", False))))
        orig_store(pb, mode=mode)

    monkeypatch.setattr(coach1, "_store_playbook", spy_store)

    class _TwoPassBackend:
        def complete(self, system: str, user: str, **kwargs) -> str:
            if "AUDIT THE PROPOSED PLAYBOOK" in user:
                return json.dumps(fx["legacy_response"])
            return "Discovery notes for Minsc haste pump."

    withheld_pb = fx["legacy_book"].with_current_study({"bad": True}, {})
    assert withheld_pb.study_withheld is True
    with monkeypatch.context() as mp:
        mp.setattr(coach1, "_attach_study_with_idle_cancel", lambda *a, **kw: withheld_pb)
        returned = coach1.prepare_playbook(
            fx["state"], backend=_TwoPassBackend(), study_client=_MemoryStudyClient(), build_full_study=True
        )
    assert returned.study_withheld is True
    assert stored_calls == []
    assert coach1._cached_playbook(fx["identity"], fx["state"], modes=("idle",)) is None

    # Clean attach via real attach_shared_study stores exactly once (no double store).
    server_payload = {"study": fx["study_res"].study, "chapters": fx["study_res"].chapters}
    attached = coach1.prepare_playbook(
        fx["state"],
        backend=_TwoPassBackend(),
        study_client=_MemoryStudyClient({fx["dhash"]: server_payload}),
        build_full_study=True,
    )
    assert _playbook_has_study(attached)
    assert stored_calls == [("idle", False)]

    # 2. Server miss across _prepare_playbook_with_study executes at most ONE fetch_study() request.
    dir_miss = tmp_path / "single_fetch"
    dir_miss.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_miss)
    coach2 = CoachEngine(backend=Mock())
    coach2._store_playbook(fx["legacy_book"], mode="idle")
    client2 = _MemoryStudyClient()
    lib2 = _Library(coach2, fx["record"], study_client=client2, backend_factory=_FakeStudyBackend)
    res2 = lib2._prepare_playbook_with_study(fx["state"])
    assert _playbook_has_study(res2)
    assert len(client2.fetches) == 1

    # 3. Initial build_missing=False lookup in _ensure_cached_study registers cancel_event in _playbook_cancels.
    dir_reg = tmp_path / "reg_cancel"
    dir_reg.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_reg)
    coach3 = CoachEngine(backend=Mock())
    coach3._store_playbook(fx["legacy_book"], mode="idle")
    observed_registered: list[bool] = []
    cancel3 = threading.Event()

    class _CancelSpyClient(_MemoryStudyClient):
        def fetch_study(self, deck_hash: str, *, version: int = STUDY_VERSION):
            observed_registered.append(coach3._playbook_cancels.get(fx["identity"]) is cancel3)
            return super().fetch_study(deck_hash, version=version)

    coach3.prepare_playbook(
        fx["state"],
        backend=_FakeStudyBackend(),
        cancel=cancel3,
        study_client=_CancelSpyClient({fx["dhash"]: server_payload}),
        build_full_study=False,
    )
    assert observed_registered == [True]
    assert fx["identity"] not in coach3._playbook_cancels

    # 4. tutor_missing_piece_hints differentiates empty library inventory from known-empty/mana bottleneck.
    one_step_state = {
        "turn": {"turn_number": 4, "phase": "Phase_Main1", "active_player": 1},
        "battlefield": [
            {
                "instance_id": 10,
                "grp_id": 81841,
                "name": "Minsc & Boo, Timeless Heroes",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
            }
        ],
    }
    fake_snap = SimpleNamespace(
        live=(),
        one_step_away=(
            {
                "names": ("Minsc & Boo, Timeless Heroes", "Ruby, Daring Tracker"),
                "missing_names": ["Ruby, Daring Tracker"],
                "copies_in_library": 0,
                "outcome": "Curve pump",
            },
        ),
    )
    unknown_lib_hints = tutor_missing_piece_hints(fake_snap, one_step_state, request_type="Search")
    known_lib_hints = tutor_missing_piece_hints(
        fake_snap, {**one_step_state, "deck_cards": [81841]}, request_type="Search"
    )
    assert "not in known library" in unknown_lib_hints[0]
    assert "need mana/source" in known_lib_hints[0]


def test_nn5_basic_land_reminder_text_matches_native_db_and_withhold_breaks_idle_loop(
    monkeypatch, tmp_path, caplog
):
    # 1. Live MTGA shape: Raw_CardDatabase has oracle_text == "" on basic lands, whereas
    # UnifiedCardDatabase merges intrinsic Scryfall reminder text ("({T}: Add {R}.)").
    cat_sources = {
        90001: {
            "name": "Mountain",
            "type_line": "Basic Land — Mountain",
            "mana_cost": "",
            "oracle_text": "({T}: Add {R}.)",
            "power": "",
            "toughness": "",
        },
        90002: {
            "name": "Snow-Covered Island",
            "type_line": "Basic Snow Land — Island",
            "mana_cost": "",
            "oracle_text": "({T}: Add {U}.)",
            "power": "",
            "toughness": "",
        },
        90003: {
            "name": "Wastes",
            "type_line": "Basic Land",
            "mana_cost": "",
            "oracle_text": "{T}: Add {C}.",
            "power": "",
            "toughness": "",
        },
        90004: {
            "name": "Grizzly Bears",
            "type_line": "Creature — Bear",
            "mana_cost": "{1}{G}",
            "oracle_text": "",
            "power": "2",
            "toughness": "2",
        },
    }
    native_cards = native_catalog(monkeypatch, cat_sources)
    for gid in (90001, 90002, 90003):
        native_cards[gid].oracle_text = ""

    assert current_study_sources_match({gid: cat_sources[gid] for gid in (90001, 90002, 90003)}) is True
    # Negative controls must stay strict (never accept non-intrinsic text or non-basic cards).
    assert (
        current_study_sources_match(
            {90001: {**cat_sources[90001], "oracle_text": "({T}: Add {R}.) Deal 3 damage."}}
        )
        is False
    )
    assert (
        current_study_sources_match({90001: {**cat_sources[90001], "oracle_text": "({T}: Add {G}.)"}})
        is False
    )
    assert (
        current_study_sources_match({90004: {**cat_sources[90004], "oracle_text": "({T}: Add {R}.)"}})
        is False
    )

    # 2. End-to-end prepare_playbook with native Forest oracle_text == "" still persists v4 study.
    fx = _studied_fixture(monkeypatch, tmp_path)
    dir_ok = tmp_path / "basic_ok"
    dir_ok.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_ok)

    study_catalog_with_reminder = {
        gid: ({**card, "oracle_text": "({T}: Add {G}.)"} if gid == 75553 else card)
        for gid, card in fx["catalog"].items()
    }
    native_fixture_cards = native_catalog(monkeypatch, study_catalog_with_reminder)
    native_fixture_cards[75553].oracle_text = ""
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda gid, _epoch=0: study_catalog_with_reminder.get(gid, {}),
    )

    legacy_with_reminder = replace(fx["legacy_book"], catalog=study_catalog_with_reminder)
    coach = CoachEngine(backend=Mock())
    coach._store_playbook(legacy_with_reminder, mode="idle")
    client = _MemoryStudyClient()
    pb = coach.prepare_playbook(
        fx["state"],
        backend=_FakeStudyBackend(),
        study_client=client,
        build_full_study=True,
    )
    assert pb.study_withheld is False
    assert _playbook_has_study(pb)
    restored = CoachEngine(backend=Mock())._cached_playbook(fx["identity"], fx["state"])
    assert restored is not None
    assert restored.version == PLAYBOOK_VERSION
    assert _playbook_has_study(restored) and restored.study_withheld is False

    # 3. Real source mismatch still withholds cleanly, logs warning, skips overwrite, and breaks idle retry loop.
    dir_wh = tmp_path / "withheld_loop"
    dir_wh.mkdir()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", dir_wh)
    native_fixture_cards[75553].oracle_text = "Conflicting non-empty native text"
    coach_wh = CoachEngine(backend=Mock())
    coach_wh._store_playbook(fx["legacy_book"], mode="idle")
    server_payload = {"study": fx["study_res"].study, "chapters": fx["study_res"].chapters}
    client_wh = _MemoryStudyClient({fx["dhash"]: server_payload})

    backend_calls = 0

    def fail_if_called():
        nonlocal backend_calls
        backend_calls += 1
        return _FakeStudyBackend()

    lib = _Library(coach_wh, fx["record"], study_client=client_wh, backend_factory=fail_if_called)
    with caplog.at_level("WARNING", logger="arenamcp.coach"):
        out = coach_wh.attach_shared_study(
            fx["legacy_book"], fx["state"], study_client=client_wh, build_missing=False
        )
    assert out.study_withheld is True
    assert any("Deck study withheld" in r.message for r in caplog.records)
    assert fx["identity"] in coach_wh._study_withheld_identities
    # Cache on disk stayed legacy v2 without persisting withheld study.
    disk_pb = CoachEngine(backend=Mock())._cached_playbook(fx["identity"], fx["state"])
    assert disk_pb is not None and disk_pb.study_withheld is False and not _playbook_has_study(disk_pb)
    # Idle sweep now sees the session withhold marker and skips repeating build_deck_study.
    assert lib._has_complete_idle_playbook(fx["record"]) is True
    lib._build_missing_playbooks(threading.Event())
    assert backend_calls == 0
