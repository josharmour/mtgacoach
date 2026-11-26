"""Regression and control tests for live-draft fixes (bugs #1-#9)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from tests.draft_fakes import PICK_STATE, FakeBridge

from arenamcp.draft_advisor import _text, splash_supported
from arenamcp.draft_autopick import Lane, PickScore
from arenamcp.draft_event import DraftEventDriver, DraftRun, spoken_draft_line
from arenamcp.draft_narration import DraftNarrator
from arenamcp.event_queue import EventQueueConfig
from arenamcp.limited_deck import _plan_text, _produces
from arenamcp.limited_rules import synergy_evidence


# 1. Robotic TTS disclaimer stripping
def test_spoken_draft_line_strips_audit_boilerplate_and_keeps_rationale():
    raw = (
        "Choosing Temporal Anomaly: a utility land. "
        "Rating trade-off: Temporal Anomaly is weak; Eject the Warp Core is good in the supplied 17Lands bands. "
        "Coach's rationale: Takes a colorless fixing utility land over removal. (judgment, not proven advantage)."
    )
    spoken = spoken_draft_line(raw)
    assert "proven advantage" not in spoken.lower()
    assert "supplied 17lands bands" not in spoken.lower()
    assert "Choosing Temporal Anomaly: a utility land." in spoken
    assert "Over Eject the Warp Core: Takes a colorless fixing utility land over removal." in spoken

    unverified = (
        "Choosing Bird: flies. Rating trade-off: Bird is average; Xenobotanist is strong "
        "in the supplied 17Lands bands. No comparison with this higher-rated alternative was supplied; "
        "this choice is unverified."
    )
    assert spoken_draft_line(unverified) == "Choosing Bird: flies. Note: Xenobotanist is rated higher here."

    # Period-containing card names (e.g. Dr. Madison Li, U.S.S. Enterprise) stay intact.
    abbrev = (
        "Choosing Counterspell: clean answer. "
        "Alternative: Dr. Madison Li. Costs too much right now. "
        "(Coach's judgment, not proven advantage.)"
    )
    assert (
        spoken_draft_line(abbrev)
        == "Choosing Counterspell: clean answer. Over Dr. Madison Li: Costs too much right now."
    )

    # Opposite control: clean plain line passes through unchanged.
    plain = "Choosing Cetacean Ops: it works with our noncreature spells."
    assert spoken_draft_line(plain) == plain


# 2. Queued matchmaking status vs Home screen prompt
def test_queued_status_reports_waiting_for_match_instead_of_home_prompt(monkeypatch):
    statuses: list[str] = []
    bridge = FakeBridge({"get_screen": {"ok": True, "module": ""}})
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: None,
        primer_service=Mock(),
        status_fn=statuses.append,
        queue_fn=lambda: True,
        event_mode_fn=lambda: EventQueueConfig(
            event_name="QuickDraft_TRK_20261119", display_name="Quick Draft: Star Trek"
        ),
    )
    driver._step_for_event = lambda: None  # type: ignore[assignment]
    driver.set_enabled(True)

    driver.run = DraftRun(queued_at=100.0)
    monkeypatch.setattr("arenamcp.draft_event.time.monotonic", lambda: 105.0)
    driver._step()
    assert statuses[-1] == "Queued for Quick Draft: Star Trek — waiting for a match…"

    # Opposite control: not queued (or timed out) asks to click Home in Arena.
    driver.run = DraftRun(queued_at=0.0)
    driver._step()
    assert "click Home in Arena" in statuses[-1]


# 3. Shocklands / typed duals recognized as splash & color sources
def test_splash_supported_and_produces_recognize_typed_duals_and_arena_mana():
    watery_grave_typed = {
        "name": "Watery Grave",
        "type_line": "Land — Island Swamp",
        "oracle_text": "As this land enters, you may pay 2 life. If you don't, it enters tapped.",
    }
    watery_grave_arena = {
        "name": "Watery Grave",
        "type_line": "Land",
        "oracle_text": "{oT}: Add {oU}.\n{oT}: Add {oB}.",
    }
    single_clause_dual = {
        "name": "Dimir Guildgate",
        "type_line": "Land — Gate",
        "oracle_text": "{oT}: Add {oU} or {oB}.",
    }
    assert splash_supported([watery_grave_typed], "B") is True
    assert splash_supported([watery_grave_arena], "B") is True
    assert splash_supported([single_clause_dual], "B") is True
    assert _produces(watery_grave_typed) == {"U", "B"}
    assert _produces(watery_grave_arena) == {"U", "B"}

    # Opposite controls: basic Swamp or off-color dual do not fix Black nonbasically in splash_supported.
    basic_swamp = {
        "name": "Swamp",
        "type_line": "Basic Land — Swamp",
        "oracle_text": "",
        "produced_mana": ["B"],
    }
    stomping_ground = {"name": "Stomping Ground", "type_line": "Land — Mountain Forest", "oracle_text": ""}
    assert splash_supported([basic_swamp], "B") is False
    assert splash_supported([stomping_ground], "B") is False


# 4. Deck summary deduplicates repeated bomb names
def test_plan_text_deduplicates_duplicate_bombs():
    text = _plan_text(
        ("U", "R"),
        {
            "archetype": "Spells",
            "avg_gih": 56.4,
            "creatures": 12,
            "removal": 5,
            "bombs": ["Khan, Despot by Design", "Khan, Despot by Design", "Sky Drake"],
        },
    )
    assert "bombs 2x Khan, Despot by Design, Sky Drake" in text

    # Control: distinct bombs stay listed cleanly without multiplier prefix.
    distinct = _plan_text(
        ("U", "R"),
        {
            "creatures": 12,
            "removal": 5,
            "bombs": ["Khan, Despot by Design", "Eject the Warp Core"],
        },
    )
    assert "bombs Khan, Despot by Design, Eject the Warp Core" in distinct


# 5. Model clause never attributes Alternative text to the picked card
def test_model_clause_strips_alternative_before_extracting_reason():
    reason = (
        "Flexible blue flyer. "
        "Alternative: Xenobotanist. Xenobotanist is also good — a 1/3 mana dork that fixes and ramps our splash."
    )
    assert DraftNarrator._model_clause(reason, 20, ["Bird", "Xenobotanist"]) == "flexible blue flyer"

    only_alt = "Bad card. Alternative: Xenobotanist. Great mana dork for our deck."
    assert DraftNarrator._model_clause(only_alt, 20, []) == ""


# 6. Contradictory "best card" vs higher-WR pack land
def test_narrator_says_top_rated_spell_when_a_pack_land_has_higher_gih():
    spell = SimpleNamespace(
        grp_id=1,
        name="Eject the Warp Core",
        colors="B",
        gih_wr=0.58,
        types="Instant",
        cmc=2.0,
        oracle="-2/-2",
    )
    land = SimpleNamespace(
        grp_id=2, name="Temporal Anomaly", colors="", gih_wr=0.61, types="Land", cmc=0.0, oracle="Add {C}."
    )
    other = SimpleNamespace(
        grp_id=3, name="Bear", colors="G", gih_wr=0.53, types="Creature", cmc=2.0, oracle=""
    )
    primer = SimpleNamespace(
        cards={1: spell, 2: land, 3: other},
        card=lambda g: {1: spell, 2: land, 3: other}.get(g),
        archetypes=[],
        pair_stats={},
        card_roles=lambda _n: [],
    )
    ranking = [
        PickScore(2, "Temporal Anomaly", 1.0),
        PickScore(1, "Eject the Warp Core", 0.9),
        PickScore(3, "Bear", 0.1),
    ]
    narrator = DraftNarrator()
    with_higher_land = narrator.pick_line(
        names=["Eject the Warp Core"],
        chosen=[ranking[1]],
        ranking=ranking,
        pool=[],
        lane_before=Lane("", {}, 0.0),
        lane_after=Lane("", {}, 0.0),
        primer=primer,
        pack_number=1,
        pick_number=2,
        pack_size=14,
    )
    assert "top-rated spell" in with_higher_land
    assert "best card" not in with_higher_land

    # Opposite control: without a higher-WR land, says "best card".
    primer_no_land = SimpleNamespace(
        cards={1: spell, 3: other},
        card=lambda g: {1: spell, 3: other}.get(g),
        archetypes=[],
        pair_stats={},
        card_roles=lambda _n: [],
    )
    no_land = narrator.pick_line(
        names=["Eject the Warp Core"],
        chosen=[PickScore(1, "Eject the Warp Core", 0.9)],
        ranking=[PickScore(1, "Eject the Warp Core", 0.9), PickScore(3, "Bear", 0.1)],
        pool=[],
        lane_before=Lane("", {}, 0.0),
        lane_after=Lane("", {}, 0.0),
        primer=primer_no_land,
        pack_number=1,
        pick_number=2,
        pack_size=14,
    )
    assert "best card" in no_land


# 7. Skip LLM call on 1-card forced packs
def test_one_card_pack_skips_llm_advisor_call():
    cards = {
        1: SimpleNamespace(name="Last Pick Card", mana_cost="{2}{U}", type_line="Creature", rarity="common"),
        2: SimpleNamespace(name="Other Spell", mana_cost="{1}{R}", type_line="Instant", rarity="common"),
    }
    advisor = Mock()
    advisor.recommend.return_value = {
        "reasoning_source": "model",
        "recommendations": [{"grp_id": 2, "reason": "Removal."}],
        "plan": "Open.",
    }
    bridge = FakeBridge(
        {
            "get_draft_state": {**PICK_STATE, "pack_cards": [1], "pick_seconds_remaining": 45},
            "submit_draft_pick": {"ok": True},
        }
    )
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: None,
        primer_service=Mock(),
        card_db=SimpleNamespace(get_card=cards.get),
        picked_fn=list,
        pick_advisor_fn=lambda: advisor,
        pack_fn=lambda: {"cards": [{"grp_id": 1}], "pack_number": 1, "pick_number": 14},
    )
    driver.set_enabled(True)
    driver._step_pick()
    advisor.recommend.assert_not_called()
    submits = [f for a, f in bridge.sent if a == "submit_draft_pick"]
    assert submits[0]["cards"][0]["grp_id"] == 1

    # Opposite control: >1 card in pack invokes advisor.
    bridge.replies["get_draft_state"]["pack_cards"] = [1, 2]
    driver._pack_fn = lambda: {"cards": [{"grp_id": 1}, {"grp_id": 2}], "pack_number": 1, "pick_number": 1}
    driver._step_pick()
    advisor.recommend.assert_called_once()


# 8. Sentence-aware _text trimming avoids dangling trailing fragments
def test_text_trim_avoids_dangling_sentence_fragment():
    long = (
        "This creature generates massive value by creating artifact tokens every turn "
        "and adding its counters to threats on board. It also synergizes loosely "
        "while still fitting our curve cleanly if the table dries up later."
    )
    trimmed = _text(long, words=20)
    assert trimmed.endswith("on board.")
    assert not trimmed.endswith(" It")

    no_period = " ".join(["token"] * 18 + ["while", "still", "more", "cards"])
    clipped = _text(no_period, words=20)
    assert not clipped.endswith("still…") and not clipped.endswith("while…")

    # Control: short text under limit is untouched.
    short = "Adds black fixing and triggers prowess cleanly."
    assert _text(short, words=40) == short


# 9. Expanded limited rules: artifact/creature sac + self dies + noncreature condition
def test_limited_rules_detect_trk_death_and_noncreature_cast_synergies():
    eject = {
        "grp_id": 1,
        "name": "Eject the Warp Core",
        "type_line": "Instant",
        "oracle_text": "Kicker—Sacrifice an artifact or creature.\nTarget creature gets -2/-2 until end of turn.",
    }
    drone = {
        "grp_id": 2,
        "name": "Tenacious Drone",
        "type_line": "Artifact Creature — Borg",
        "oracle_text": "When this creature dies, create a 1/1 colorless Borg artifact creature token.",
    }
    bird = {
        "grp_id": 3,
        "name": "Romulan Bird-of-Prey",
        "type_line": "Artifact — Spacecraft",
        "oracle_text": "Whenever you cast a noncreature spell, if this Spacecraft isn't a creature, put two charge counters on it.",
    }
    jockey = {
        "grp_id": 4,
        "name": "Starfleet Spanner Jockey",
        "type_line": "Creature — Human Artificer",
        "oracle_text": "Defender\nAs long as you've cast a noncreature spell this turn, this creature can attack as though it didn't have defender.",
    }
    vanilla = {
        "grp_id": 5,
        "name": "Vanilla Bear",
        "type_line": "Creature — Bear",
        "oracle_text": "",
    }
    assert any(e["mechanic"] == "dies" for e in synergy_evidence(eject, drone))
    assert any(e["mechanic"] == "noncreature_spell" for e in synergy_evidence(bird, jockey))
    # Opposite control: vanilla creature does not falsely enable either payoff.
    assert synergy_evidence(eject, vanilla) == []
    assert synergy_evidence(jockey, vanilla) == []
