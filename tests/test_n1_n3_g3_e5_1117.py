"""Regression and acceptance tests for Batch B (N1, N2, N3, G3, E5)."""

from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace

from arenamcp.ability_synthesizer import Draw
from arenamcp.card_facts.derived import derive_threat
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.reviewed_notes import corpus
from arenamcp.card_facts.schema import CardFacts, EffectEntry
from arenamcp.card_facts.validate import PrintedCard
from arenamcp.commentary.audited_facts import binding
from arenamcp.commentary.card_annotations import observed_annotations
from arenamcp.commentary.contracts import Epoch, Evidence, Observation
from arenamcp.commentary.private_outs import private_removal_fact, private_removal_line
from arenamcp.game_plan import GamePlanManager, _is_engine
from arenamcp.gamestate import GameState
from arenamcp.match_context import library_outs_summary
from arenamcp.opponent_read import OpponentRead


def test_n1_four_headline_cards_and_derived_threat():
    by_gid, records, sources, _ = corpus()
    for name in (
        "Griselbrand",
        "Sheoldred, the Apocalypse",
        "Winota, Joiner of Forces",
        "Thassa's Oracle",
    ):
        rec = records[name]
        assert rec["threat"]["plan"], f"{name} missing plan claim"
        assert rec["threat"]["clock"], f"{name} missing clock claim"
        assert rec["threat"]["answers"], f"{name} missing answers claim"
        assert rec["relationships"]["links"], f"{name} missing relationships links"
        for field in ("plan", "clock", "answers"):
            for claim in rec["threat"][field]:
                assert claim["origin"] == "model"
                assert claim["evidence"]
                for item in claim["evidence"]:
                    assert item["quote"] in sources[item["card"]].oracle_text
        for link in rec["relationships"]["links"]:
            assert link["card"] != name
            assert link["claim"]["origin"] == "model"

    # Preserve #347 / AoA contract: rebalanced Winota print 96294 stays excluded.
    assert 71283 in by_gid
    assert 96294 not in by_gid

    printed = PrintedCard("Divination", "{2}{U}", "Sorcery", "Draw two cards.")
    cf = CardFacts(
        name="Divination",
        rules_hash=rules_hash("Divination", "{2}{U}", "Sorcery", "Draw two cards."),
        type_line="Sorcery",
        mana_cost="{2}{U}",
        effects=(EffectEntry(trigger="", effect=Draw(count=2)),),
        confidence="high",
    )
    derived = derive_threat(cf, printed)
    assert derived["plan"][0]["origin"] == "derived"
    assert derived["plan"][0]["evidence"][0]["quote"] == "Draw two cards."


def test_n2_recorded_sequence_threat_on_cast_combo_on_entry_and_missing_silence():
    _, _, sources, _ = corpus()
    oracle_printed = sources["Thassa's Oracle"]
    pact_printed = sources["Tainted Pact"]

    epoch = Epoch(match_id="m-n2-test", game_number=1)
    pact_card = {
        "instance_id": 101,
        "grp_id": 77512,
        "name": pact_printed.name,
        "mana_cost": pact_printed.mana_cost,
        "type_line": pact_printed.type_line,
        "oracle_text": pact_printed.oracle_text,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    oracle_card = {
        "instance_id": 102,
        "grp_id": 70584,
        "name": oracle_printed.name,
        "mana_cost": oracle_printed.mana_cost,
        "type_line": oracle_printed.type_line,
        "oracle_text": oracle_printed.oracle_text,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    unknown_card = {
        "instance_id": 199,
        "grp_id": 99999999,
        "name": "Missing Record Card",
        "mana_cost": "{1}{U}",
        "type_line": "Instant",
        "oracle_text": "Draw a card.",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }

    # 1. Opponent casts Thassa's Oracle on the stack -> threat layer facts emitted.
    cast_event = Evidence(
        event_id="ev-cast-oracle",
        kind="action_observed",
        source_sequence=1,
        snapshot_id="s-1",
        seat=2,
        visibility="public",
        payload={"instance_id": 102, "grp_id": 70584, "name": "Thassa's Oracle"},
        turn_number=3,
        phase="Phase_Main1",
        active_player=2,
    )
    pub_base = {
        "local_seat_id": 1,
        "turn_info": {"turn_number": 3, "active_player": 2, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
    }
    obs_cast = Observation(
        epoch=epoch,
        source_sequence=1,
        snapshot_id="s-1",
        arrived_at=10.0,
        events=(cast_event,),
        public_state={
            **pub_base,
            "zones": {"stack": [oracle_card], "battlefield": [], "graveyard": [], "exile": []},
        },
        private_state={},
    )
    cast_facts = list(observed_annotations(obs_cast, cast_event).values())
    assert [f["scope"] for f in cast_facts] == ["plan", "clock", "answers"]
    assert all(f["kind"] == "card_threat" for f in cast_facts)

    # 2. Tainted Pact resolved to graveyard earlier, then Thassa's Oracle enters battlefield -> link!
    entry_event = Evidence(
        event_id="ev-enter-oracle",
        kind="spell_resolved",
        source_sequence=2,
        snapshot_id="s-2",
        seat=2,
        visibility="public",
        payload={
            "instance_id": 102,
            "grp_id": 70584,
            "name": "Thassa's Oracle",
            "to_zone": "battlefield",
        },
        turn_number=3,
        phase="Phase_Main1",
        active_player=2,
    )
    obs_entry = Observation(
        epoch=epoch,
        source_sequence=2,
        snapshot_id="s-2",
        arrived_at=11.0,
        events=(entry_event,),
        public_state={
            **pub_base,
            "zones": {
                "stack": [],
                "battlefield": [oracle_card],
                "graveyard": [pact_card],
                "exile": [],
            },
        },
        private_state={},
    )
    entry_facts = list(observed_annotations(obs_entry, entry_event).values())
    rel = [f for f in entry_facts if f["kind"] == "card_relationship"]
    assert len(rel) == 1
    assert rel[0]["observed_partner"]["name"] == "Tainted Pact"
    assert rel[0]["scope"] == "possibility"

    # 3. Missing record -> zero annotation facts (plus booth silence for missing card).
    missing_event = Evidence(
        event_id="ev-cast-missing",
        kind="action_observed",
        source_sequence=3,
        snapshot_id="s-3",
        seat=2,
        visibility="public",
        payload={"instance_id": 199, "grp_id": 99999999, "name": "Missing Record Card"},
        turn_number=3,
        phase="Phase_Main1",
        active_player=2,
    )
    obs_missing = Observation(
        epoch=epoch,
        source_sequence=3,
        snapshot_id="s-3",
        arrived_at=12.0,
        events=(missing_event,),
        public_state={
            **pub_base,
            "zones": {"stack": [unknown_card], "battlefield": [], "graveyard": [], "exile": []},
        },
        private_state={},
    )
    assert observed_annotations(obs_missing, missing_event) == {}

    # 4. HeadlessBooth integrates all three observations cleanly without skipping on CI.
    from arenamcp.commentary.config import CommentaryConfig
    from arenamcp.commentary.headless import HeadlessBooth

    saved_aoa = {name: sys.modules[name] for name in list(sys.modules) if name.startswith("arenaonair")}
    for name in list(saved_aoa):
        sys.modules.pop(name, None)

    class _DummyGenerativeBooth:
        def __init__(self, *a, **k):
            self.lock = SimpleNamespace(__enter__=lambda: None, __exit__=lambda *_: False)
            self.epoch = 1
            self.seq = 0
            self.pending = []
            self.recent = []
            self.batch_since = None
            self.counts = {"context_bounds": 0}
            self.backoff_until = 0

        def update_read(self, *_):
            pass

        def observe(self, *_):
            pass

        def submit(self, *_):
            self.seq += 1

        def turn_started(self, *_):
            pass

        def take_work(self):
            return None

        def status(self):
            return {"state": "ok"}

    cfg_mod = SimpleNamespace(
        Config=lambda **kw: SimpleNamespace(**kw),
        resolve_booth=lambda *a, **k: {},
        STYLES={"technical": "technical"},
    )
    llm_mod = SimpleNamespace(
        GenerativeBooth=_DummyGenerativeBooth,
        JsonClient=type("JsonClient", (), {}),
        ModelError=RuntimeError,
        AUDIT_SYSTEM="audit",
        spoken_word_count=lambda text: len(text.split()),
    )
    story_mod = SimpleNamespace(
        StoryModel=lambda: SimpleNamespace(update=lambda *_: None, read=lambda *_: {})
    )
    models_mod = SimpleNamespace(
        CardRef=lambda *a, **k: SimpleNamespace(),
        GameState=lambda *a, **k: SimpleNamespace(),
        MatchMeta=lambda *a, **k: SimpleNamespace(),
        PlayerView=lambda *a, **k: SimpleNamespace(),
        SeatKnowledge=lambda *a, **k: SimpleNamespace(),
        TurnInfo=lambda *a, **k: SimpleNamespace(),
        ZoneView=lambda *a, **k: SimpleNamespace(),
        Event=lambda *a, **k: SimpleNamespace(),
    )
    sys.modules["arenaonair"] = SimpleNamespace(
        config=cfg_mod, llm_booth=llm_mod, story=story_mod, models=models_mod
    )
    sys.modules["arenaonair.config"] = cfg_mod
    sys.modules["arenaonair.llm_booth"] = llm_mod
    sys.modules["arenaonair.story"] = story_mod
    sys.modules["arenaonair.models"] = models_mod
    try:
        booth = HeadlessBooth(
            CommentaryConfig(mode="commentary", generative=True), lambda *_: {}, lambda: 10.0
        )
        booth.generate(obs_cast)
        assert [f["kind"] for f in booth.annotation_facts.values()] == [
            "card_threat",
            "card_threat",
            "card_threat",
        ]
        booth.generate(obs_entry)
        assert any(
            f["kind"] == "card_relationship" and f["observed_partner"]["name"] == "Tainted Pact"
            for f in booth.annotation_facts.values()
        )
        booth.generate(obs_missing)
        assert booth.annotation_facts == {}
    finally:
        for name in [n for n in list(sys.modules) if n.startswith("arenaonair")]:
            sys.modules.pop(name, None)
        sys.modules.update(saved_aoa)


def test_n3_flyer_outs_count_controls_and_privacy():
    flyer_threat = {
        "instance_id": 500,
        "grp_id": 9001,
        "name": "Serra Angel",
        "mana_cost": "{3}{W}{W}",
        "type_line": "Creature — Angel",
        "oracle_text": "Flying\nVigilance",
        "power": 4,
        "toughness": 4,
        "keywords": ["Flying"],
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "visibility": "Visibility_Public",
    }
    ground_threat = copy.deepcopy(flyer_threat)
    ground_threat.update(
        {
            "name": "Grizzly Bears",
            "mana_cost": "{1}{G}",
            "type_line": "Creature — Bear",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "keywords": [],
        }
    )

    catalog = {
        77304: {
            "name": "Plummet",
            "mana_cost": "{1}{G}",
            "type_line": "Instant",
            "oracle_text": "Destroy target creature with flying.",
        },
        80863: {
            "name": "Swords to Plowshares",
            "mana_cost": "{W}",
            "type_line": "Instant",
            "oracle_text": "Exile target creature. Its controller gains life equal to its power.",
        },
        10001: {
            "name": "Forest",
            "mana_cost": "",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
        },
    }

    gs = GameState()
    gs.set_local_seat_id(1, source=2)
    gs.deck_cards = [77304] * 2 + [80863] * 1 + [10001] * 57
    gs._awaiting_connection_match_id = "m-n3-flyer"
    gs.capture_connection_deck()
    lib_ids = list(range(101, 161))
    gs.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameStateId": 1,
            "gameInfo": {"matchID": "m-n3-flyer", "gameNumber": 1, "stage": "GameStage_Start"},
            "zones": [
                {"zoneId": 31, "ownerSeatId": 1, "type": "ZoneType_Hand"},
                {"zoneId": 32, "ownerSeatId": 1, "type": "ZoneType_Library", "objectInstanceIds": lib_ids},
            ],
        }
    )
    drawn = [77304] + [10001] * 6
    gs.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameStateId": 2,
            "prevGameStateId": 1,
            "zones": [
                {
                    "zoneId": 32,
                    "ownerSeatId": 1,
                    "type": "ZoneType_Library",
                    "objectInstanceIds": lib_ids[7:],
                },
                {"zoneId": 31, "ownerSeatId": 1, "type": "ZoneType_Hand", "objectInstanceIds": lib_ids[:7]},
                {"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [500]},
            ],
            "gameObjects": [
                *[
                    {
                        "instanceId": iid,
                        "grpId": gid,
                        "overlayGrpId": gid,
                        "zoneId": 31,
                        "ownerSeatId": 1,
                        "controllerSeatId": 1,
                        "type": "GameObjectType_Card",
                        "visibility": "Visibility_Private",
                        "viewers": [1],
                    }
                    for iid, gid in zip(lib_ids[:7], drawn, strict=True)
                ],
                {
                    "instanceId": 500,
                    "grpId": 9001,
                    "zoneId": 28,
                    "ownerSeatId": 2,
                    "controllerSeatId": 2,
                    "type": "GameObjectType_Card",
                    "visibility": "Visibility_Public",
                    "cardTypes": ["CardType_Creature"],
                },
            ],
        }
    )

    def snap(threat):
        return {
            "match_id": "m-n3-flyer",
            "game_number": 1,
            "local_seat_id": 1,
            "opponent_seat_id": 2,
            "_log_game_state_id": 2,
            "turn": {"turn_number": 3},
            "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2}],
            "zones": {"battlefield": [threat], "stack": []},
        }

    # Opposing flyer -> both 1 Plummet + 1 Swords remaining out of 53 cards.
    fact_flyer = private_removal_fact(snap(flyer_threat), gs, allow_private=True, card_lookup=catalog.get)
    assert fact_flyer is not None
    assert fact_flyer["remaining"] == 53
    assert fact_flyer["answers"] == {"Plummet": 1, "Swords to Plowshares": 1}
    iid, line = private_removal_line(fact_flyer, snap(flyer_threat))
    assert iid == 500
    assert "53 left" in line and "2 cards" in line and "Plummet" in line and "Swords to Plowshares" in line

    # Opposing non-flyer -> Plummet does NOT count; only 1 Swords out of 53.
    fact_ground = private_removal_fact(snap(ground_threat), gs, allow_private=True, card_lookup=catalog.get)
    assert fact_ground is not None
    assert fact_ground["remaining"] == 53
    assert fact_ground["answers"] == {"Swords to Plowshares": 1}

    # Privacy controls: allow_private=False via both entry points withholds cleanly; never reads opp library.
    assert private_removal_fact(snap(flyer_threat), gs, allow_private=False, card_lookup=catalog.get) is None
    assert library_outs_summary(snap(flyer_threat), gs, allow_private=False, card_lookup=catalog.get) is None
    assert library_outs_summary(snap(flyer_threat), gs, allow_private=True, card_lookup=catalog.get)[
        "answers"
    ] == {"Plummet": 1, "Swords to Plowshares": 1}


def test_g3_reveal_sequence_updates_read_with_card_facts(monkeypatch, tmp_path):
    from arenamcp.card_facts.client import CardFactsClient

    oracle_printed = {
        "name": "Custom Combo Engine",
        "mana_cost": "{1}{U}",
        "type_line": "Creature — Wizard",
        "oracle_text": "Whenever you cast an instant spell, draw a card.",
    }
    digest = rules_hash(
        oracle_printed["name"],
        oracle_printed["mana_cost"],
        oracle_printed["type_line"],
        oracle_printed["oracle_text"],
    )
    record_dict = {
        "name": "Custom Combo Engine",
        "rules_hash": digest,
        "type_line": oracle_printed["type_line"],
        "mana_cost": oracle_printed["mana_cost"],
        "threat": {
            "version": 1,
            "plan": [{"text": "Generates repeated card draw from instant spells.", "origin": "model"}],
            "answers": [{"text": "Destroy it before combat or chain spells.", "origin": "model"}],
        },
        "relationships": {
            "version": 1,
            "links": [
                {
                    "card": "Unseen Payoff",
                    "role": "payoff",
                    "claim": {"text": "Benefits from repeated instant casts."},
                }
            ],
        },
        "confidence": "high",
    }
    now = 1_000_000.0
    client = CardFactsClient(cache_dir=tmp_path / "cf", clock=lambda: now)
    client._memory[digest] = (record_dict, now)
    monkeypatch.setattr("arenamcp.card_facts.client._shared", client)
    monkeypatch.setattr(
        "arenamcp.card_facts.client.facts_for",
        lambda cards, memory_only=False: (
            client.fresh_memory_facts(cards) if memory_only else client.facts_for(cards)
        ),
    )

    _, _, sources, _ = corpus()
    thassa = sources["Thassa's Oracle"]
    tp = sources["Tainted Pact"]
    reader = OpponentRead()

    state1 = {
        "match_id": "m-g3",
        "game_number": 1,
        "local_seat_id": 1,
        "commentary_source_sequence": 1,
        "zones": {
            "command": [
                {
                    "instance_id": 201,
                    "grp_id": 99001,
                    "name": oracle_printed["name"],
                    "mana_cost": oracle_printed["mana_cost"],
                    "type_line": oracle_printed["type_line"],
                    "oracle_text": oracle_printed["oracle_text"],
                    "public_native_print": f"99001:{digest}",
                    "object_kind": "CARD",
                    "controller_seat_id": 2,
                    "owner_seat_id": 2,
                    "visibility": "Visibility_Public",
                }
            ],
            "battlefield": [],
            "graveyard": [],
            "exile": [],
            "stack": [],
            "hand": [{"name": "HiddenOpponentCard", "controller_seat_id": 2, "owner_seat_id": 2}],
        },
    }
    obs1 = Observation(
        epoch=Epoch("m-g3", 1),
        source_sequence=1,
        snapshot_id="s-1",
        arrived_at=1.0,
        public_state=state1,
    )
    facts1 = reader.observation_facts(obs1)
    assert "HiddenOpponentCard" not in json.dumps(facts1)
    assert any("Unseen Payoff" in f["text"] and "Inference" in f["text"] for f in facts1.values())
    assert "Unseen Payoff" in reader.plan_block(state1)
    cf_facts = {k: v for k, v in facts1.items() if k.startswith("state:opponent_read:cf_")}
    assert cf_facts
    # Verify audited_facts admits all cached CardFacts opponent_read facts for commentary.
    assert binding(
        "snapshot", {"facts": cf_facts, "deps": cf_facts, "refs": list(cf_facts)}, {}, obs1, "Inference"
    ) == (
        "snapshot",
        "",
    )

    # Reveal Thassa's Oracle then Tainted Pact across sequence numbers -> read updates deterministically.
    state2 = copy.deepcopy(state1)
    state2["commentary_source_sequence"] = 2
    state2["zones"]["battlefield"] = [
        {
            "instance_id": 202,
            "grp_id": 70584,
            "name": thassa.name,
            "mana_cost": thassa.mana_cost,
            "type_line": thassa.type_line,
            "oracle_text": thassa.oracle_text,
            "object_kind": "CARD",
            "controller_seat_id": 2,
            "owner_seat_id": 2,
            "visibility": "Visibility_Public",
        }
    ]
    facts2 = reader.observation_facts(
        Observation(
            epoch=Epoch("m-g3", 1),
            source_sequence=2,
            snapshot_id="s-2",
            arrived_at=2.0,
            public_state=state2,
        )
    )
    assert any("Tainted Pact" in f["text"] and "no confirmed sighting" in f["text"] for f in facts2.values())

    state3 = copy.deepcopy(state2)
    state3["commentary_source_sequence"] = 3
    state3["zones"]["graveyard"] = [
        {
            "instance_id": 203,
            "grp_id": 77512,
            "name": tp.name,
            "mana_cost": tp.mana_cost,
            "type_line": tp.type_line,
            "oracle_text": tp.oracle_text,
            "object_kind": "CARD",
            "controller_seat_id": 2,
            "owner_seat_id": 2,
            "visibility": "Visibility_Public",
        }
    ]
    facts3 = reader.observation_facts(
        Observation(
            epoch=Epoch("m-g3", 1),
            source_sequence=3,
            snapshot_id="s-3",
            arrived_at=3.0,
            public_state=state3,
        )
    )
    assert any(
        "Both" in f["text"] and "Tainted Pact" in f["text"] and "Thassa's Oracle" in f["text"]
        for f in facts3.values()
    )
    # Plummet mentions "flying" but is not a flying creature.
    plummet = sources["Plummet"]
    state3["zones"]["graveyard"].append(
        {
            "instance_id": 204,
            "grp_id": 77304,
            "name": plummet.name,
            "mana_cost": plummet.mana_cost,
            "type_line": plummet.type_line,
            "oracle_text": plummet.oracle_text,
            "object_kind": "CARD",
            "controller_seat_id": 2,
            "owner_seat_id": 2,
            "visibility": "Visibility_Public",
        }
    )
    classes = reader.threat_classes(state3)
    assert "fast_combo" in classes
    assert "flyers" not in classes


def test_e5_mana_rock_role_flip_switch_if_and_lethal_clear_vs_appear(monkeypatch):
    class FakeBackend:
        def __init__(self, responses):
            self._responses = list(responses)
            self.calls = 0

        def complete(self, system, user, max_tokens, **kwargs):
            self.calls += 1
            return self._responses.pop(0) if self._responses else "{}"

    monkeypatch.setattr(
        "arenamcp.game_plan.threading.Thread",
        lambda target=None, **kwargs: SimpleNamespace(start=target),
    )

    plan_base = json.dumps(
        {
            "role": "aggressor",
            "win_conditions": ["aggro beatdown"],
            "path": "pressure early",
            "threat": "opposing flyers",
            "develop_next": "play a threat",
            "switch_if": ["Synthesizer destroyed -> pivot to control"],
        }
    )
    plan_switch = json.dumps(
        {
            "role": "defender",
            "win_conditions": ["grind value"],
            "path": "pivot after switch_if",
            "threat": "opposing lethal",
            "develop_next": "stabilize",
            "switch_if": ["Synthesizer destroyed -> pivot to control"],
        }
    )
    be = FakeBackend([plan_base, plan_switch, plan_switch])
    key_state = {"val": ("aggressor", False, False, False)}
    mgr = GamePlanManager(be)
    mgr._REFRESH_INTERVAL_S = 0.0
    orig_strat = mgr._strategic_key

    def fake_key(game_state, sig=None):
        _, c1, c2, eng = orig_strat(game_state, sig)
        return (key_state["val"], c1, c2, eng)

    monkeypatch.setattr(mgr, "_strategic_key", fake_key)

    def mkstate(turn=2, active=1, extra_opp=None, our_synth=True):
        bf = [
            {
                "instance_id": 10,
                "name": "Synthesizer",
                "type_line": "Artifact",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
            }
        ]
        gy = []
        if not our_synth:
            bf = []
            gy = [{"instance_id": 10, "name": "Synthesizer"}]
        for card in extra_opp or []:
            bf.append(card)
        return {
            "match_id": "m-e5-unit",
            "local_seat_id": 1,
            "opponent_seat_id": 2,
            "players": [
                {"is_local": True, "seat_id": 1, "life_total": 20, "hand_size": 5},
                {"is_local": False, "seat_id": 2, "life_total": 20, "hand_size": 5},
            ],
            "turn": {"turn_number": turn, "active_player": active, "phase": "Phase_Main1"},
            "battlefield": bf,
            "graveyard": gy,
            "hand": [],
            "exile": [],
            "command": [],
        }

    # 1. First plan.
    assert mgr.request_reform(mkstate(turn=1)) is True
    assert be.calls == 1

    # 2. Pure mana rock on opponent's turn -> NO reform, folded in code.
    mox = {
        "instance_id": 50,
        "name": "Mox Amber",
        "type_line": "Legendary Artifact",
        "oracle_text": "{T}: Add one mana of any color among legendary creatures and planeswalkers you control.",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    assert mgr.request_reform(mkstate(turn=2, active=2, extra_opp=[mox])) is False
    assert be.calls == 1

    # 3. Role flip -> folded in code, NO reform.
    key_state["val"] = ("defender", False, False, False)
    assert mgr.request_reform(mkstate(turn=2, active=2, extra_opp=[mox])) is False
    assert be.calls == 1 and mgr.current.role == "defender"

    # 4. Lethal flag appears -> immediate reform!
    key_state["val"] = ("defender", False, True, False)
    assert mgr.request_reform(mkstate(turn=3, active=1, extra_opp=[mox])) is True
    assert be.calls == 2

    # 5. Lethal flag clears -> NO reform, updated in code!
    key_state["val"] = ("defender", False, False, False)
    assert mgr.request_reform(mkstate(turn=4, active=2, extra_opp=[mox])) is False
    assert be.calls == 2

    # 6. switch_if met (our Synthesizer destroyed) -> DOES replan!
    assert mgr.request_reform(mkstate(turn=4, active=2, extra_opp=[mox], our_synth=False)) is True
    assert be.calls == 3

    # 7. Sacrifice-for-mana combo engines (e.g. Ashnod's Altar) remain engines (not pure mana rocks).
    altar = {
        "instance_id": 51,
        "name": "Ashnod's Altar",
        "type_line": "Artifact",
        "oracle_text": "Sacrifice a creature: Add {C}{C}.",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    assert _is_engine(altar) is True
    assert _is_engine(mox) is False

    # 8. Same-turn first plan must NOT suppress a genuinely new dead_soon flag appearing later that turn.
    be2 = FakeBackend([plan_base, plan_switch, plan_switch, plan_switch])
    key_state2 = {"val": ("aggressor", False, False, False)}
    mgr2 = GamePlanManager(be2)
    mgr2._REFRESH_INTERVAL_S = 0.0
    orig_strat2 = mgr2._strategic_key
    monkeypatch.setattr(
        mgr2, "_strategic_key", lambda gs, sig=None: (key_state2["val"], *orig_strat2(gs, sig)[1:])
    )
    assert mgr2.request_reform(mkstate(turn=1)) is True
    assert be2.calls == 1
    key_state2["val"] = ("defender", False, False, True)
    assert mgr2.request_reform(mkstate(turn=1)) is True
    assert be2.calls == 2

    # 9. A major threat suppressed inside [T+1..T+5] is NOT erased from plan.basis and triggers at T+6.
    mgr2._threat_turn = 1
    griselbrand = {
        "instance_id": 60,
        "name": "Griselbrand",
        "type_line": "Legendary Creature — Demon",
        "power": 7,
        "toughness": 7,
        "keywords": ["Flying", "Lifelink"],
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    assert mgr2.request_reform(mkstate(turn=3, active=2, extra_opp=[griselbrand])) is False
    assert mgr2.request_reform(mkstate(turn=7, active=2, extra_opp=[griselbrand])) is True
    assert be2.calls == 3
