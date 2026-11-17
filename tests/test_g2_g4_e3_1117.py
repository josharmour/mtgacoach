"""Regression tests for bug_20261117_094407 (D4), G2, G4, and E3."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import _signature as board_signature
from arenamcp.card_db import CardInfo
from arenamcp.card_facts.client import CardFactsClient
from arenamcp.card_facts.prompt_render import candidate_fact_hashes
from arenamcp.coach import CoachEngine
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import HeadlessBooth
from arenamcp.deck_candidates import bo1_variant, compare_collection, load_candidates
from arenamcp.deck_strategy import PLAYBOOK_VERSION, DeckPlaybook, deck_identity, oracle_rules
from arenamcp.deck_study import (
    STUDY_VERSION,
    build_deck_study,
    reuse_deck_study_for_edit,
    validate_study,
    validate_study_chapters,
)
from arenamcp.desktop.deck_builder import DeckBuilderDialog
from arenamcp.match_context import (
    STRATEGIC_POLICY_CORE,
    prepare_match_context,
    strategic_policy,
)
from arenamcp.situational_rules import SUBJECTS as SITUATIONAL_SUBJECTS
from arenamcp.synergy_tracker import (
    entries_from_playbook,
    evaluate_synergies,
    player_synergy_facts,
    tutor_missing_piece_hints,
)


class _TinyCardDB:
    def __init__(self, cards: dict[int, CardInfo]):
        self._by_id = cards
        self._by_name = {c.name.casefold(): c for c in cards.values()}

    def get_card_by_arena_id(self, arena_id: int) -> CardInfo | None:
        return self._by_id.get(arena_id)

    def get_card_by_name(self, name: str) -> CardInfo | None:
        return self._by_name.get(name.casefold())

    def canonical(self, name: str) -> str:
        c = self.get_card_by_name(name)
        return c.name if c is not None else name


def _landfall_cards_db() -> _TinyCardDB:
    specs = [
        (1, "Forest", "common", "Basic Land — Forest", "{T}: Add {G}."),
        (2, "Llanowar Elves", "common", "Creature — Elf Druid", "{T}: Add {G}."),
        (3, "Surrak, Elusive Hunter", "rare", "Legendary Creature — Human Warrior", "Trample"),
        (4, "Sapling Nursery", "rare", "Enchantment", "Landfall — Create a Saproling."),
        (5, "Sandman, Shifting Scoundrel", "rare", "Creature — Human Villain", "Menace"),
        (6, "Mole Man, Moloid Master", "rare", "Creature — Moloid", "Reach"),
        (7, "Mightform Harmonizer", "rare", "Creature — Insect", "Landfall — Double power."),
    ]
    return _TinyCardDB(
        {
            gid: CardInfo(
                name=name,
                mana_cost="{G}" if "Land" not in tl else "",
                type_line=tl,
                oracle_text=oracle,
                cmc=1.0 if "Land" not in tl else 0.0,
                colors=["G"] if "Land" not in tl else [],
                arena_id=gid,
                rarity=rarity,
            )
            for gid, name, rarity, tl, oracle in specs
        }
    )


def _landfall_bo3_deck() -> dict:
    return {
        "name": "1. Mono-Green Landfall",
        "format": "Standard",
        "source_rank": 1,
        "meta_share": "11.6%",
        "source": "MTGGoldfish Standard (paper metagame)",
        "url": "https://www.mtggoldfish.com/archetype/standard-mono-green-landfall-woe",
        "checked_at": "2026-11-17T17:41:40+00:00",
        "mainboard": ["56 Forest", "4 Llanowar Elves"],
        "sideboard": [
            "2 Surrak, Elusive Hunter",
            "1 Sapling Nursery",
            "2 Sandman, Shifting Scoundrel",
            "1 Mole Man, Moloid Master",
            "1 Mightform Harmonizer",
        ],
    }


def _study_catalog() -> tuple[dict[int, dict], list[int]]:
    catalog = {
        81841: {
            "name": "Minsc & Boo, Timeless Heroes",
            "mana_cost": "{2}{R}{G}",
            "type_line": "Legendary Planeswalker — Minsc",
            "oracle_text": (
                "When Minsc & Boo enters and at the beginning of your upkeep, create Boo.\n"
                "[+1]: Put three +1/+1 counters on up to one target creature with trample or haste."
            ),
            "loyalty": 3,
        },
        92102: {
            "name": "Veteran Survivor",
            "mana_cost": "{W}",
            "type_line": "Creature — Human Survivor",
            "oracle_text": (
                "Survival — At the beginning of your second main phase, if this creature is tapped, "
                "exile up to one target card from a graveyard.\n"
                "As long as there are three or more cards exiled with this creature, it gets +3/+3 and has hexproof."
            ),
            "power": "2",
            "toughness": "1",
        },
        93958: {
            "name": "Ruby, Daring Tracker",
            "mana_cost": "{R}{G}",
            "type_line": "Legendary Creature — Human Scout",
            "oracle_text": "Haste\n{T}: Add {R} or {G}.",
            "power": "1",
            "toughness": "2",
        },
        75553: {
            "name": "Forest",
            "mana_cost": "",
            "type_line": "Basic Land — Forest",
            "oracle_text": "",
        },
    }
    return catalog, [81841]


class _FakeStudyBackend:
    def __init__(self, *, use_bad_92102: bool = False, hallucinate_chapter_attempt0: bool = False):
        self.calls = 0
        self.use_bad_92102 = use_bad_92102
        self.hallucinate_chapter_attempt0 = hallucinate_chapter_attempt0
        self.seq_attempts = 0
        self.sw_attempts = 0
        self.last_request_metrics = {"reasoning_tokens": 12}

    @staticmethod
    def _role_ids(user: str) -> list[int]:
        ids: list[int] = []
        in_roles = False
        for line in user.splitlines():
            if line.strip() == "CARD ROLES:":
                in_roles = True
                continue
            if in_roles:
                stripped = line.strip()
                if not stripped:
                    break
                head = stripped.split(":", 1)[0].strip()
                if head.isdigit():
                    ids.append(int(head))
        return ids or [81841]

    def complete(self, system: str, user: str, **kwargs) -> str:
        self.calls += 1
        if "DECK_STUDY_PASS: cards" in system:
            target_ids = []
            for line in user.splitlines():
                if line.startswith("CARD "):
                    target_ids.append(int(line.split(":", 1)[0].replace("CARD ", "").strip()))
            rows = []
            for gid in target_ids:
                if gid == 92102 and self.use_bad_92102:
                    rows.append(
                        {
                            "card": "92102",
                            "role": "graveyard hate threat",
                            "play": {
                                "advice": "Play turn 1 to pressure and exile graveyard threats.",
                                "conditions": {
                                    "pieces": [{"card": 0, "zone": "graveyard"}],
                                    "mana": {"W": 1},
                                    "first_turn": 1,
                                },
                            },
                            "hold": {
                                "advice": "Hold if sweeping soon.",
                                "conditions": {
                                    "pieces": [{"card": 92102, "zone": "hand"}],
                                    "mana": {"generic": 0},
                                    "first_turn": 1,
                                },
                            },
                            "avoid": {
                                "advice": "Avoid tapping into larger blockers.",
                                "conditions": {
                                    "pieces": [{"card": 92102, "zone": "battlefield"}],
                                    "mana": {"generic": 0},
                                    "first_turn": 1,
                                },
                            },
                            "evidence": [{"card": 92102, "rule": 99}],
                        }
                    )
                else:
                    rows.append(
                        {
                            "card": gid,
                            "role": "engine piece",
                            "play": {
                                "advice": f"Deploy {gid} cleanly.",
                                "conditions": {
                                    "pieces": [{"card": gid, "zone": "hand"}],
                                    "mana": {"generic": 1},
                                    "first_turn": 1,
                                },
                            },
                            "hold": {
                                "advice": f"Hold {gid} if board wipe threatened.",
                                "conditions": {
                                    "pieces": [{"card": gid, "zone": "hand"}],
                                    "mana": {"generic": 0},
                                    "first_turn": 1,
                                },
                            },
                            "avoid": {
                                "advice": f"Avoid trading {gid} cheaply.",
                                "conditions": {
                                    "pieces": [{"card": gid, "zone": "battlefield"}],
                                    "mana": {"generic": 0},
                                    "first_turn": 1,
                                },
                            },
                            "evidence": [{"card": gid, "rule": 1}],
                        }
                    )
            return json.dumps({"cards": rows})
        if "DECK_STUDY_PASS: discovery" in system:
            return json.dumps(
                {
                    "interactions": [
                        {
                            "id": "ruby-minsc-pump",
                            "kind": "synergy",
                            "conditions": {
                                "pieces": [
                                    {"card": 81841, "zone": "battlefield"},
                                    {"card": 93958, "zone": "battlefield"},
                                ],
                                "mana": {"generic": 0},
                                "first_turn": 3,
                            },
                            "outcome": "Minsc +1 immediately pumps hasty Ruby to 4/5 attacking.",
                            "stops": "Instant removal targeting Ruby on combat step.",
                            "evidence": [{"card": 81841, "rule": 1}, {"card": 93958, "rule": 1}],
                            "links": [],
                        }
                    ]
                }
            )
        if "DECK_STUDY_PASS: sequencing" in system:
            self.seq_attempts += 1
            role_ids = self._role_ids(user)
            first_id = role_ids[0]
            second_id = role_ids[-1]
            if self.hallucinate_chapter_attempt0 and self.seq_attempts == 1:
                return json.dumps(
                    {
                        "mulligan": "Keep 2-3 lands.",
                        "mana": "Hit R and G.",
                        "recovery": "Rebuild.",
                        "sequencing": [
                            {
                                "id": "seq_bad",
                                "before": [999999],
                                "after": [first_id],
                                "turns": "T1-T3",
                                "reason": "Hallucinated ID should trigger retry.",
                            }
                        ],
                    }
                )
            return json.dumps(
                {
                    "mulligan": "Keep 2-3 lands plus turn-1 drop or ramp.",
                    "mana": "Hit R and G plus four mana by turn 3.",
                    "recovery": "Rebuild around commander tax if swept.",
                    "sequencing": [
                        {
                            "id": "seq_ramp",
                            "before": [second_id],
                            "after": [first_id],
                            "turns": "T2-T4",
                            "reason": "Play haste ramp before planeswalker pump.",
                        }
                    ],
                }
            )
        if "DECK_STUDY_PASS: switches" in system:
            self.sw_attempts += 1
            role_ids = self._role_ids(user)
            lead_id = role_ids[0]
            tail_id = role_ids[-1]
            if self.hallucinate_chapter_attempt0 and self.sw_attempts == 1:
                return json.dumps(
                    {
                        "aggro_burn": {
                            "posture": "stabilize",
                            "prioritize": [999999],
                            "hold_up": [tail_id],
                            "trim_or_delay": [lead_id],
                            "rule": "Hallucinated ID and missing canonical keys must trigger retry.",
                        }
                    }
                )
            return json.dumps(
                {
                    "switches": {
                        "fast_combo": {
                            "posture": "race",
                            "prioritize": [lead_id],
                            "hold_up": [tail_id],
                            "trim_or_delay": [],
                            "rule": "Pressure fast combo immediately with hasty threats.",
                        },
                        "wide_board": {
                            "posture": "trade",
                            "prioritize": [lead_id],
                            "hold_up": [],
                            "trim_or_delay": [],
                            "rule": "Push trample damage over go-wide blockers.",
                        },
                        "counterspells": {
                            "posture": "bait",
                            "prioritize": [tail_id],
                            "hold_up": [tail_id],
                            "trim_or_delay": [lead_id],
                            "rule": "Lead cheap threats into open blue mana.",
                        },
                        "graveyard": {
                            "posture": "disrupt",
                            "prioritize": [tail_id],
                            "hold_up": [tail_id],
                            "trim_or_delay": [],
                            "rule": "Hold graveyard interaction for key recursion targets.",
                        },
                        "aggro_burn": {
                            "posture": "stabilize",
                            "prioritize": [tail_id],
                            "hold_up": [tail_id],
                            "trim_or_delay": [lead_id],
                            "rule": "Race safely with lifegain or cheap blockers against burn.",
                        },
                    }
                }
            )
        return json.dumps({})


def test_bug_20261117_094407_bo1_variant_and_shortfall_wildcards():
    db = _landfall_cards_db()
    deck = _landfall_bo3_deck()
    collection = {1: 56, 2: 4}
    wildcards = {"common": 174, "uncommon": 155, "rare": 4, "mythic": 6}

    bo1 = bo1_variant(deck, database=db)
    assert bo1 is not None
    assert bo1["sideboard"] == []
    assert bo1["mainboard"] == ["56 Forest", "4 Llanowar Elves"]

    full_cost = compare_collection(deck, collection, wildcards, db)
    assert full_cost["spend"]["rare"] == 7
    assert full_cost["affordable"] is False
    assert full_cost["shortfall_wildcards"] == {"rare": 3}

    bo1_cost = compare_collection(bo1, collection, wildcards, db)
    assert bo1_cost["spend"].get("rare", 0) == 0
    assert bo1_cost["affordable"] is True
    assert bo1_cost["shortfall_wildcards"] == {}


def test_bug_20261117_094407_deck_builder_dialog_default_bo1_balance_fallback_and_copy(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    monkeypatch.setattr("arenamcp.deck_candidates.pricing_database", _landfall_cards_db)
    monkeypatch.setattr(
        "arenamcp.mtggoldfish.MTGGoldfishClient",
        lambda: SimpleNamespace(
            get_metagame=lambda fmt: (
                [
                    {
                        "name": "Mono-Green Landfall",
                        "url": _landfall_bo3_deck()["url"],
                        "meta_share": "11.6%",
                        "source_rank": 1,
                    }
                ]
                if fmt == "standard"
                else []
            ),
            get_deck_list=lambda _url: {**_landfall_bo3_deck(), "retrieved_at": "2026-11-17T17:41:40+00:00"},
        ),
    )
    collection = {1: 56, 2: 4}
    wildcards = {"common": 174, "uncommon": 155, "rare": 4, "mythic": 6}
    from_cache = load_candidates(collection, wildcards, formats=("standard",))
    assert from_cache and from_cache[0]["bo1_cost"]["affordable"] is True
    assert from_cache[0]["cost"]["shortfall_wildcards"] == {"rare": 3}
    loaded = [{**from_cache[0], "arena": None, "arena_sample": None}]

    monkeypatch.setattr(
        "arenamcp.desktop.deck_builder.QThreadPool.globalInstance",
        lambda: SimpleNamespace(start=lambda _w: None),
    )
    dialog = DeckBuilderDialog(None)
    assert dialog.list_scope.currentText() == "Best-of-1 (60-card main deck)"
    dialog.refresh_button.setEnabled(False)
    dialog._on_collection({"ok": True, "cards": collection, "wildcards": wildcards})
    assert "4 rare" in dialog.balance_label.text()
    assert "6 mythic" in dialog.balance_label.text()

    dialog.mode.setCurrentIndex(1)  # Within my wildcard balance
    dialog._loaded(loaded, None)
    assert "1 deck matches this filter" in dialog.status.text()
    assert "(Bo1 main)" in dialog.table.item(0, 3).text()

    dialog.list_scope.setCurrentIndex(1)  # Full list (with Bo3 sideboard)
    assert "0 decks match 'Within my wildcard balance'" in dialog.status.text()
    assert "short 3 rare" in dialog.table.item(0, 3).text()

    dialog.table.selectRow(0)
    dialog._copy()
    assert "NOT ENOUGH WILDCARDS (short 3 rare)" in dialog.status.text()
    assert "Sideboard" in QApplication.clipboard().text()
    dialog.close()


def test_g2_pass1_normalizes_string_id_placeholder_and_bad_evidence():
    catalog, commanders = _study_catalog()
    backend = _FakeStudyBackend(use_bad_92102=True)
    res = build_deck_study(catalog, commanders, backend, format_name="Brawl", batch_size=2)

    assert validate_study(res.study, catalog)["structured_cards"] == 3
    chap_cov = validate_study_chapters(res.chapters, catalog)
    assert chap_cov["sequencing_rules"] == 1
    assert "aggro_burn" in chap_cov["switches"]

    card_92102 = next(row for row in res.study["cards"] if row["card"] == 92102)
    assert [p["card"] for p in card_92102["play"]["conditions"]["pieces"]] == [92102]
    assert card_92102["evidence"] == [
        {
            "card": 92102,
            "source": card_92102["evidence"][0]["source"],
            "rule": 1,
            "quote": oracle_rules(catalog[92102])[0],
        }
    ]


def test_g2_v4_export_restore_and_5card_edit_reuse(monkeypatch):
    catalog, commanders = _study_catalog()
    backend = _FakeStudyBackend()
    res = build_deck_study(catalog, commanders, backend, format_name="Brawl", batch_size=8)

    base_data = {
        "archetype": "Minsc aggro",
        "spoken_summary": "Ramp into Minsc and Boo.",
        "primary_plan": "Ramp into Minsc and Boo.",
        "backup_plan": "Attack with hasty creatures.",
        "card_roles": {"81841": "Commander", "93958": "Haste ramp", "92102": "Graveyard hate"},
        "phases": {"early": "Ramp", "mid": "Minsc", "late": "Burn", "recovery": "Recast"},
        "commanders": [
            {
                "card": 81841,
                "deployment": "T3",
                "ongoing_value": "Boo",
                "replay_value": "ETB Boo",
                "resource_comparison": {
                    "starting_resources": "M",
                    "preserve_original": "P",
                    "spend_and_recover": "R",
                    "decision_test": "T",
                },
                "preserve_or_reuse": "Protect Minsc when ahead.",
                "constraints": "+1 haste/trample",
            }
        ],
        "mechanisms": [
            {
                "id": "m1",
                "cards": [81841, 93958],
                "effect": "Haste pump",
                "requires": ["Haste body in play"],
                "avoid": ["Open instant removal"],
                "evidence": [{"card": 81841, "rule": 1}, {"card": 93958, "rule": 1}],
                "depends_on": ["Haste creature"],
            }
        ],
        "decision_rules": [],
    }
    monkeypatch.setattr("arenamcp.deck_study.current_study_sources_match", lambda *_a, **_k: True)
    ident = deck_identity({"deck_cards": sorted(catalog), "commander_grp_ids": commanders})
    book = DeckPlaybook.parse(json.dumps(base_data), catalog, commanders, ident)
    v4 = book.with_current_study(res.study, chapters=res.chapters)
    exported = v4.export()
    assert exported["version"] == PLAYBOOK_VERSION == 4

    restored = DeckPlaybook.restore(
        exported,
        {"deck_cards": sorted(catalog), "commander_grp_ids": commanders},
    )
    assert restored.version == 4
    assert "study" in restored.data and "study_chapters" in restored.data

    # Edit <= 5 cards cleanly: swap one nonland, add 4 basics -> reuse_deck_study_for_edit reuses unchanged cards
    edited_catalog = {k: v for k, v in catalog.items() if k != 92102}
    edited_catalog[99001] = {
        "name": "Llanowar Elves",
        "mana_cost": "{G}",
        "type_line": "Creature — Elf Druid",
        "oracle_text": "{T}: Add {G}.",
        "power": "1",
        "toughness": "1",
    }
    edit_backend = _FakeStudyBackend()
    reused = reuse_deck_study_for_edit(
        res.study,
        res.chapters,
        catalog,
        edited_catalog,
        commanders,
        edit_backend,
    )
    assert validate_study(reused.study, edited_catalog)["structured_cards"] == 3
    by_gid = {c["card"]: c for c in reused.study["cards"]}
    old_81841 = next(c for c in res.study["cards"] if c["card"] == 81841)
    assert by_gid[81841] == old_81841
    assert 99001 in by_gid

    # Nonland removal alone (dirty_cards == []) still refreshes stale chapters & uploads via study_client
    uploaded: list[tuple[str, int, str]] = []

    class _UploadClient:
        def upload_study(self, dhash, payload, *, version=STUDY_VERSION, model_id=""):
            uploaded.append((dhash, version, model_id))
            return True

    removed_92102_catalog = {k: v for k, v in catalog.items() if k != 92102}
    stale_chapters = json.loads(json.dumps(res.chapters))
    stale_chapters["switches"]["aggro_burn"]["prioritize"] = [92102]
    removed_backend = _FakeStudyBackend()
    removed_res = reuse_deck_study_for_edit(
        res.study,
        stale_chapters,
        catalog,
        removed_92102_catalog,
        commanders,
        removed_backend,
        study_client=_UploadClient(),
    )
    assert validate_study(removed_res.study, removed_92102_catalog)["structured_cards"] == 2
    assert validate_study_chapters(removed_res.chapters, removed_92102_catalog)["sequencing_rules"] == 1
    assert removed_res.reused_cards == 2
    assert removed_res.calls_made == 2  # only sequencing + switches refreshed despite dirty_cards == []
    assert uploaded and uploaded[0][0] == removed_res.deck_hash


def test_g2_chapters_reject_hallucinated_ids_and_missing_canonical_keys_on_attempt0():
    catalog, commanders = _study_catalog()
    backend = _FakeStudyBackend(hallucinate_chapter_attempt0=True)
    res = build_deck_study(catalog, commanders, backend, format_name="Brawl", batch_size=8)
    assert backend.seq_attempts == 2
    assert backend.sw_attempts == 2
    cov = validate_study_chapters(res.chapters, catalog)
    assert cov["sequencing_rules"] == 1
    assert {"fast_combo", "wide_board", "counterspells", "graveyard"} <= set(cov["switches"])


def test_g2_website_deck_studies_roundtrip_and_pii_rejection(tmp_path, monkeypatch):
    import tests.website_app as website_app_helper
    from fastapi.testclient import TestClient
    from tests.website_app import load_website_app

    monkeypatch.setattr(
        website_app_helper,
        "WEBSITE_MODULES",
        website_app_helper.WEBSITE_MODULES + ("card_facts",),
    )
    proxy_app, proxy_db, patreon = load_website_app(tmp_path, monkeypatch, "proxy_app_deck_studies_test")
    cf = sys.modules["card_facts"]
    monkeypatch.setattr(cf, "MIN_LOOKUP_INTERVAL_S", 0.0)
    monkeypatch.setattr(patreon, "_http", lambda: SimpleNamespace())

    customer_key = "sk-study-customer"
    with proxy_db.get_db() as conn:
        conn.execute(
            "INSERT INTO subscribers (license_key, email, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (customer_key, "p@example.com", "Patron", "active", time.time()),
        )

    deck_hash = hashlib.sha256(b"g2-test-deck").hexdigest()
    headers = {"Authorization": f"Bearer {customer_key}"}
    with TestClient(proxy_app.app) as client:
        miss = client.get(f"/v1/deck-studies/{deck_hash}?version={STUDY_VERSION}", headers=headers)
        assert miss.status_code == 200 and miss.json()["found"] is False

        pii_resp = client.post(
            "/v1/deck-studies",
            headers=headers,
            json={
                "deck_hash": deck_hash,
                "study_version": STUDY_VERSION,
                "study": {"player_name": "secret_user"},
            },
        )
        assert pii_resp.status_code == 400

        empty_resp = client.post(
            "/v1/deck-studies",
            headers=headers,
            json={"deck_hash": deck_hash, "study_version": STUDY_VERSION, "study": {}},
        )
        assert empty_resp.status_code == 422

        store_resp = client.post(
            "/v1/deck-studies",
            headers=headers,
            json={
                "deck_hash": deck_hash,
                "study_version": STUDY_VERSION,
                "model_id": "current",
                "study": {
                    "study": {"cards": [], "interactions": []},
                    "chapters": {"sequencing": {}, "switches": {}},
                },
            },
        )
        assert store_resp.status_code == 200 and store_resp.json()["stored"] is True

        hit = client.get(f"/v1/deck-studies/{deck_hash}?version={STUDY_VERSION}", headers=headers)
        assert hit.status_code == 200
        assert hit.json()["found"] is True
        assert hit.json()["model_id"] == "current"


def test_g4_synergy_tracker_decision_context_board_sig_and_commentary_privacy(monkeypatch):
    catalog, commanders = _study_catalog()
    backend = _FakeStudyBackend()
    res = build_deck_study(catalog, commanders, backend, format_name="Brawl", batch_size=8)
    long_plan = (
        "Ramp fast with mana dorks into Minsc and Boo to generate giant trample bodies while trading efficiently on every turn. "
        + "Extra secondary filler prose that definitely exceeds two hundred twenty characters when left completely uncapped across sixty consecutive planner prompts."
    )
    data = {
        "archetype": "Minsc",
        "spoken_summary": "Ramp fast with mana dorks into Minsc.",
        "primary_plan": long_plan,
        "backup_plan": "Short backup.",
        "card_roles": {"81841": "Commander", "93958": "Haste ramp", "92102": "Graveyard hate"},
        "phases": {"early": "Ramp", "mid": "Minsc", "late": "Burn", "recovery": "Recast"},
        "commanders": [
            {
                "card": 81841,
                "deployment": "T3",
                "ongoing_value": "Boo",
                "replay_value": "Boo",
                "resource_comparison": {
                    "starting_resources": "M",
                    "preserve_original": "P",
                    "spend_and_recover": "R",
                    "decision_test": "T",
                },
                "preserve_or_reuse": "Protect Minsc.",
                "constraints": "+1",
            }
        ],
        "mechanisms": [
            {
                "id": "m1",
                "cards": [81841, 93958],
                "effect": "Haste pump",
                "requires": ["Haste body in play"],
                "avoid": ["Open instant removal"],
                "evidence": [{"card": 81841, "rule": 1}, {"card": 93958, "rule": 1}],
                "depends_on": ["Haste creature"],
            }
        ],
        "decision_rules": [],
    }
    monkeypatch.setattr("arenamcp.deck_study.current_study_sources_match", lambda *_a, **_k: True)
    ident = deck_identity({"deck_cards": sorted(catalog), "commander_grp_ids": commanders})
    book = DeckPlaybook.parse(json.dumps(data), catalog, commanders, ident).with_current_study(
        res.study, chapters=res.chapters
    )

    state_live = {
        "local_seat_id": 1,
        "turn": {"turn_number": 4},
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "battlefield": [
            {
                "instance_id": 10,
                "grp_id": 81841,
                "name": "Minsc & Boo",
                "controller_seat_id": 1,
                "type_line": "Legendary Planeswalker",
            },
            {
                "instance_id": 11,
                "grp_id": 93958,
                "name": "Ruby, Daring Tracker",
                "controller_seat_id": 1,
                "type_line": "Creature",
            },
            {
                "instance_id": 12,
                "grp_id": 75553,
                "name": "Forest",
                "controller_seat_id": 1,
                "type_line": "Basic Land — Forest",
                "is_tapped": False,
            },
            {
                "instance_id": 20,
                "grp_id": 92102,
                "name": "Opp Aggro",
                "controller_seat_id": 2,
                "type_line": "Creature",
                "power": 3,
                "toughness": 2,
            },
        ],
        "hand": [],
        "deck_cards": [81841, 93958, 92102, 75553],
        "_option_grp_ids": [93958],
        "active_threat_classes": ["aggro_burn"],
    }
    entries = entries_from_playbook(book, deck_cards=state_live["deck_cards"])
    snap_live = evaluate_synergies(entries, state_live, catalog=catalog)
    assert [s["id"] for s in snap_live.live] == ["ruby-minsc-pump"]

    state_one_step = {
        **state_live,
        "battlefield": [c for c in state_live["battlefield"] if c["grp_id"] != 93958],
    }
    snap_step = evaluate_synergies(entries, state_one_step, catalog=catalog)
    assert [s["id"] for s in snap_step.one_step_away] == ["ruby-minsc-pump"]
    assert snap_step.one_step_away[0]["missing_names"] == ["Ruby, Daring Tracker"]
    assert snap_step.one_step_away[0]["copies_in_library"] == 1
    hints = tutor_missing_piece_hints(snap_step, state_one_step, request_type="SelectN")
    assert hints and "Ruby, Daring Tracker" in hints[0]

    ctx = book.decision_context(state_live, request_type="ActionsAvailable")
    assert "Extra secondary filler prose" not in ctx
    assert "Short backup." in ctx
    assert "Live synergies: ruby-minsc-pump" in ctx
    assert "Option role [grp:93958]" in ctx
    assert "Sequencing (T2-T4):" in ctx
    assert "Switch (aggro_burn" in ctx

    legacy_book = DeckPlaybook.parse(json.dumps(data), catalog, commanders, ident)
    legacy_ctx = legacy_book.decision_context(state_live, request_type="ActionsAvailable")
    assert "Extra secondary filler prose" in legacy_ctx

    sig_without = board_signature(state_live)
    sig_with = board_signature({**state_live, "_synergy_groups": [[81841, 93958]]})
    assert sig_without != sig_with

    coach_stub = SimpleNamespace(_coach=SimpleNamespace(_deck_playbook=book))
    facts = player_synergy_facts(coach_stub, state_live)
    assert facts is not None and facts["live"][0]["id"] == "ruby-minsc-pump"

    snap_commentary = {
        **state_live,
        "match_id": "match-1",
        "game_number": 1,
        "zones": {"battlefield": [], "my_hand": [], "graveyard": [], "exile": [], "stack": []},
        "events": [{"type": "damage", "amount": 2, "target_seat_id": 2}],
        "commentary_synergies": facts,
    }
    adapter = ObservationAdapter(clock=lambda: 100.0)
    obs_pub = adapter.project(snap_commentary, private=False)
    assert "synergies" not in obs_pub.private_state
    obs_priv = adapter.project(snap_commentary, private=True)
    assert "synergies" in obs_priv.private_state

    class _StopAfterPlayerCards(Exception):
        pass

    import types

    from arenamcp.commentary import audited_facts
    from arenamcp.commentary.context import compact_context
    from arenamcp.opponent_read import OpponentRead

    fake_aoa_pkg = types.ModuleType("arenaonair")
    fake_aoa_models = types.ModuleType("arenaonair.models")
    fake_aoa_models.Event = object
    monkeypatch.setitem(sys.modules, "arenaonair", fake_aoa_pkg)
    monkeypatch.setitem(sys.modules, "arenaonair.models", fake_aoa_models)

    for allow_priv in (True, False):
        booth = HeadlessBooth.__new__(HeadlessBooth)
        booth.settings = CommentaryConfig(mode="commentary", generative=True, private_information=allow_priv)
        booth.opponent_read = OpponentRead()
        booth.played_prints = {}
        booth.annotation_epoch = None
        booth._snapshot = lambda _obs: (_ for _ in ()).throw(_StopAfterPlayerCards())
        try:
            booth.generate(obs_priv)
        except _StopAfterPlayerCards:
            pass
        if allow_priv:
            assert "private:synergies" in booth.player_cards
            assert booth.player_cards["private:synergies"]["live"][0]["id"] == "ruby-minsc-pump"
        else:
            assert "private:synergies" not in booth.player_cards

    assert (
        audited_facts.binding(
            "snapshot",
            {
                "facts": {"private:synergies": facts},
                "deps": {"private:synergies": facts},
                "refs": {"private:synergies"},
            },
            {},
            obs_priv,
            "Synergy is live.",
        )
        is not None
    )
    assert (
        audited_facts.binding(
            "snapshot",
            {"facts": {"private:synergies": facts}, "deps": {}, "refs": {"private:synergies"}},
            {},
            obs_priv,
            "Synergy is live.",
        )
        is None
    )
    compacted_priv = compact_context(
        "sys",
        {"facts": {"state:game_read": {"turn": 4}, "private:synergies": facts}},
    )
    assert "private:synergies" in compacted_priv["facts"]


def test_g4_upgrade_metrics_synergy_execution(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "upgrade_metrics",
        "/private/tmp/fable-nov/batch-a-1117/tools/upgrade_metrics.py",
    )
    assert spec and spec.loader
    um = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(um)

    catalog, commanders = _study_catalog()
    res = build_deck_study(catalog, commanders, _FakeStudyBackend(), format_name="Brawl", batch_size=8)
    monkeypatch.setattr("arenamcp.deck_study.current_study_sources_match", lambda *_a, **_k: True)
    ident = deck_identity({"deck_cards": [81841, 93958, 92102, 75553], "commander_grp_ids": commanders})
    pb_payload = {
        "version": PLAYBOOK_VERSION,
        "identity": ident,
        "data": {
            "archetype": "Minsc",
            "primary_plan": "Ramp.",
            "backup_plan": "Burn.",
            "commanders": [],
            "mechanisms": [],
            "rules": [],
            "study": res.study,
            "study_chapters": res.chapters,
        },
        "catalog": catalog,
        "commanders": commanders,
    }
    pb_dir = tmp_path / "playbooks"
    pb_dir.mkdir()
    (pb_dir / f"{ident}.json").write_text(json.dumps({"playbook": pb_payload}), encoding="utf-8")
    monkeypatch.setenv("ARENAMCP_PLAYBOOK_DIRS", str(pb_dir))
    um._PLAYBOOK_SYNERGY_CACHE.clear()

    bundle_path = tmp_path / "m1.json"
    bundle = {
        "match_id": "m1",
        "packet": {
            "decisions": [
                {
                    "chosen_options": ["idx:1"],
                    "state_snapshot": {
                        "state": {
                            "local_seat_id": 1,
                            "turn": {"turn_number": 4, "phase": "Phase_Main1", "active_player": 1},
                            "players": [{"seat_id": 1, "is_local": True}],
                            "battlefield": [
                                {"instance_id": 10, "grp_id": 81841, "controller_seat_id": 1},
                                {"instance_id": 11, "grp_id": 93958, "controller_seat_id": 1},
                            ],
                            "hand": [
                                {"instance_id": 12, "grp_id": 92102, "controller_seat_id": 1},
                            ],
                            "deck_cards": [81841, 93958, 92102, 75553],
                            "commander_grp_ids": [81841],
                        }
                    },
                    "pending_decision": {
                        "request_type": "ActionsAvailable",
                        "options": [
                            {"option_id": "idx:1", "meta": {"grpId": 93958}, "payable": True},
                            {"option_id": "idx:2", "meta": {"grpId": 92102}, "payable": True},
                        ],
                    },
                    "trace": {
                        "lines": {"tags": {"idx:1": "[LINE: 12 after T5]", "idx:2": "[LINE: 8 after T5]"}}
                    },
                }
            ]
        },
    }
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    row = um.summarize_bundle(bundle)
    agg = um.aggregate([row])
    syn = agg["synergy_execution"]
    assert syn["live_synergy_decisions"] == 1
    assert syn["executed_live_synergy"] == 1
    assert syn["execution_rate_pct"] == 100.0


def test_e3_system_lengths_inert_gy_exile_and_board_cardfacts_summary(tmp_path, monkeypatch):
    assert len(strategic_policy(SITUATIONAL_SUBJECTS)) > 2000
    assert strategic_policy(SITUATIONAL_SUBJECTS).startswith(STRATEGIC_POLICY_CORE)
    full_sys = ActionPlanner._decision_system_prompt()
    core_sys = ActionPlanner._decision_system_prompt(frozenset())
    assert len(core_sys) < 0.65 * len(full_sys)

    # MatchContext E3 inert basic land & graveyard omission vs Flashback retention
    forest = {
        "grp_id": 1001,
        "name": "Forest",
        "type_line": "Basic Land — Forest",
        "oracle_text": "{T}: Add {G}.",
        "owner_seat_id": 1,
    }
    bear = {
        "grp_id": 1002,
        "name": "Grizzly Bears",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "oracle_text": "Plain bear text.",
        "owner_seat_id": 1,
    }
    flash = {
        "grp_id": 1003,
        "name": "Think Twice",
        "mana_cost": "{1}{U}",
        "type_line": "Instant",
        "oracle_text": "Draw a card.\nFlashback {2}{U}",
        "owner_seat_id": 1,
    }
    cat = {1001: forest, 1002: bear, 1003: flash}
    base_state = {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "deck_cards": [1001, 1002, 1003],
        "hand": [],
        "battlefield": [],
        "graveyard": [forest, bear, flash],
        "exile": [],
        "stack": [],
        "legal_actions_raw": [],
    }
    prep_inert = prepare_match_context(
        dict(base_state), compact_decision=True, source_ids_complete=True, card_lookup=cat.get
    )
    assert 1003 in prep_inert["deck_rules_grp_ids"]
    assert 1001 not in prep_inert["deck_rules_grp_ids"]
    assert 1002 not in prep_inert["deck_rules_grp_ids"]

    state_gy_care = {
        **base_state,
        "hand": [
            {
                "grp_id": 1004,
                "name": "Reanimate",
                "mana_cost": "{B}",
                "type_line": "Sorcery",
                "oracle_text": "Put target creature card from a graveyard onto the battlefield.",
            }
        ],
    }
    prep_care = prepare_match_context(
        state_gy_care,
        compact_decision=True,
        source_ids_complete=True,
        card_lookup={**cat, 1004: state_gy_care["hand"][0]}.get,
    )
    assert 1002 in prep_care["deck_rules_grp_ids"]

    # CoachEngine board CardFacts 1-line summary (bypassing conftest stub onto real memory cache)
    verbose_oracle = (
        "Flying\n"
        "Whenever Vault Skybreaker deals combat damage to a player, destroy target artifact that player controls."
    )
    board_card = {
        "instance_id": 50,
        "grp_id": 500,
        "name": "Vault Skybreaker",
        "mana_cost": "{3}{U}",
        "type_line": "Creature — Bird Rogue",
        "power": "3",
        "toughness": "2",
        "oracle_text": verbose_oracle,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    opt_card = {
        **board_card,
        "instance_id": 51,
        "grp_id": 501,
        "name": "Active Skybreaker",
    }
    cf_client = CardFactsClient(cache_dir=tmp_path / "cf", license_key=lambda: "")
    for card in (board_card, opt_card):
        digest = candidate_fact_hashes(card)[0]
        rec = {
            "rules_hash": digest,
            "name": card["name"],
            "type_line": card["type_line"],
            "mana_cost": card["mana_cost"],
            "schema_version": 1,
            "confidence": "high",
            "body": {"keywords": ["Flying"], "power": "3", "toughness": "2"},
            "modes": [],
            "mana": [],
            "effects": [
                {
                    "trigger": "combat_damage_player",
                    "effect": {"effect_type": "Destroy", "target": "artifact"},
                }
            ],
            "relationships": [],
        }
        cf_client._store(digest, rec)
    monkeypatch.setattr("arenamcp.card_facts.client.warm_memory_from_disk", cf_client.warm_memory_from_disk)
    monkeypatch.setattr(
        "arenamcp.card_facts.client.facts_for",
        lambda cards, *, memory_only=False: cf_client.fresh_memory_facts(cards),
    )

    ce = CoachEngine.__new__(CoachEngine)
    gs = {"local_seat_id": 1, "_option_grp_ids": [501], "_deck_reference_grp_ids": {777}}
    rendered_side = "\n".join(
        ce._format_board_side([board_card, opt_card], 1, 3, {}, gs, is_local=True, for_planner=True)
    )
    assert "Flying; [combat_damage_player] Destroy artifact" in rendered_side
    assert "that player controls" not in rendered_side.split("Active Skybreaker")[0]
    assert "that player controls" in rendered_side.split("Active Skybreaker")[1]

    # Stack dedup & GY identity preservation with inert oracle omission
    bolt = {
        "grp_id": 777,
        "name": "Lightning Bolt",
        "controller_seat_id": 1,
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    }
    zone_state = {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "deck_reference": "RULES\nLightning Bolt [grp:777] | Lightning Bolt deals 3 damage to any target.",
        "deck_catalog": {777: bolt},
        "deck_rules_grp_ids": [777],
        "stack": [bolt],
        "graveyard": [forest, bear, flash],
        "exile": [],
    }
    zones_txt = "\n".join(ce._format_zones_and_events(zone_state, 1, 2, for_planner=True))
    assert "Lightning Bolt" in zones_txt and "deals 3 damage to any target" not in zones_txt
    assert (
        "YOUR: Forest" in zones_txt
        and "YOUR: Grizzly Bears" in zones_txt
        and "Plain bear text." not in zones_txt
    )
    assert "Flashback {2}{U}" in zones_txt


def test_g4_synergy_tracker_dork_mana_library_and_activation_costs():
    from arenamcp.synergy_tracker import SynergyEntry

    catalog = {
        81841: {
            "name": "Minsc & Boo, Timeless Heroes",
            "mana_cost": "{2}{R}{G}",
            "type_line": "Legendary Planeswalker",
            "oracle_text": "Create Boo.",
        },
        93958: {
            "name": "Llanowar Elves",
            "mana_cost": "{G}",
            "type_line": "Creature — Elf Druid",
            "oracle_text": "{T}: Add {G}.",
        },
        777: {
            "name": "Goblin Bombardment",
            "mana_cost": "{1}{R}",
            "type_line": "Enchantment",
            "oracle_text": "Sacrifice a creature: Deal 1.",
        },
    }
    entry_with_cost = SynergyEntry(
        id="activated-combo",
        kind="combo",
        pieces=((81841, "battlefield"), (777, "battlefield")),
        mana_generic=1,
        mana_colors={"G": 1},
        first_turn=1,
        outcome="Activate ability cleanly.",
        piece_names={81841: "Minsc & Boo, Timeless Heroes", 777: "Goblin Bombardment"},
    )
    entry_lib_piece = SynergyEntry(
        id="tutor-library-only",
        kind="synergy",
        pieces=((81841, "battlefield"), (777, "library")),
        first_turn=1,
        outcome="Library tutor piece.",
        piece_names={81841: "Minsc & Boo, Timeless Heroes", 777: "Goblin Bombardment"},
    )
    base_state = {
        "local_seat_id": 1,
        "turn": {"turn_number": 4},
        "players": [{"seat_id": 1, "is_local": True}],
        "battlefield": [
            {"instance_id": 1, "grp_id": 81841, "controller_seat_id": 1, "type_line": "Planeswalker"},
            {"instance_id": 2, "grp_id": 777, "controller_seat_id": 1, "type_line": "Enchantment"},
            {
                "instance_id": 3,
                "grp_id": 1001,
                "name": "Forest",
                "controller_seat_id": 1,
                "type_line": "Basic Land — Forest",
                "oracle_text": "{T}: Add {G}.",
                "is_tapped": False,
            },
            {
                "instance_id": 4,
                "grp_id": 93958,
                "name": "Llanowar Elves",
                "controller_seat_id": 1,
                "type_line": "Creature — Elf Druid",
                "oracle_text": "{T}: Add {G}.",
                "is_tapped": False,
                "turn_entered_battlefield": 3,
            },
        ],
        "deck_cards": [81841, 777, 93958],
    }

    # Older untapped mana dork supplies the 2nd mana (1 generic + {G}) -> live;
    # library piece with 0 copies left in library -> out_of_reach (battlefield does not satisfy "library")
    snap_ok = evaluate_synergies([entry_with_cost, entry_lib_piece], base_state, catalog=catalog)
    assert [x["id"] for x in snap_ok.live] == ["activated-combo"]
    assert [x["id"] for x in snap_ok.out_of_reach] == ["tutor-library-only"]

    # Summoning-sick dork cannot tap -> only 1 mana available for {1}{G} activation -> one_step_away
    sick_state = {
        **base_state,
        "battlefield": [
            c if c["instance_id"] != 4 else {**c, "turn_entered_battlefield": 4}
            for c in base_state["battlefield"]
        ],
    }
    snap_short = evaluate_synergies([entry_with_cost], sick_state, catalog=catalog)
    assert snap_short.live == ()
    assert len(snap_short.one_step_away) == 1 and snap_short.one_step_away[0]["mana_shortfall"] == 1
