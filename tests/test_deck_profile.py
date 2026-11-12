"""G1 regression tests: instant deck profile from card facts.

Every deck-level fixture below is a COMPLETE real logged list (the case id is
in the test name), stored verbatim in tests/fixtures/g1_complete_deck_lists.json:

- bug_reports/bug_20261109_212434.json ("minsc"): Minsc & Beluna RG
  legendary-matters Brawl deck, 99 cards.
- bug_reports/bug_20261109_231712.json ("mono99"): mono-red 99 with
  91 Mountains plus Hidden Volcano.
- match_bundles/00651f77-a4b5-41b7-bfe6-089c5cbcf090.json.gz ("sixty"):
  60-card mono-red aggro.

The grp_ids were resolved against the repo card database; name, mana cost,
type line and oracle text are verbatim. Card names appear only as fixture
data - the code under test keys on generic fields (type lines, mana costs,
oracle text, effect tags).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from arenamcp import deck_profile
from arenamcp.card_facts.client import hash_for_card
from arenamcp.coach import CoachEngine
from arenamcp.deck_profile import (
    _produced,
    _production_segments,
    build_profile,
    profile_for_state,
)
from arenamcp.deck_strategy import deck_identity
from arenamcp.game_plan import GamePlanManager
from arenamcp.land_sequence import _profile_colour_priority, _variants, _wanted_kinds
from arenamcp.mulligan_policy import _cached_playbook_present, _deck_keep_guide
from arenamcp.rules_engine import _normalize_mana_symbols

_FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "g1_complete_deck_lists.json").read_text())
LISTS = _FIXTURES["lists"]


def _cards(key):
    return [dict(card) for card in LISTS[key]]


def _fixture_card(key, name):
    return dict(next(card for card in LISTS[key] if card["name"] == name))


def _produced_of(card):
    """_produced on a fixture card after the ingestion-time normalization."""
    text = _normalize_mana_symbols(card["oracle_text"])
    return _produced(card["type_line"], text, [])


def _segments_of(card):
    return _production_segments(_normalize_mana_symbols(card["oracle_text"]))


def _expanded_deck_cards(key):
    deck_cards = []
    for card in LISTS[key]:
        deck_cards.extend([card["grp_id"]] * card["count"])
    return deck_cards


def _fake_resolve(mapping):
    def resolve(grp_id):
        card = dict(mapping.get(int(grp_id), {}))
        card.setdefault("name", f"Unknown({grp_id})")
        return card

    return resolve


def _stub_facts(monkeypatch):
    import arenamcp.card_facts.client as facts_client

    monkeypatch.setattr(facts_client, "facts_for", lambda cards: {})


# --- bug_20261109_212434 (Minsc & Beluna, complete 99-card list) --------------


def test_complete_minsc_list_bug_20261109_212434():
    """The full logged 99-card list profiles with honest copy counts."""
    profile = build_profile(_cards("minsc"))
    assert profile.card_count == 99
    assert profile.land_count == 37
    assert ("land", 37) in profile.roles
    assert ("ramp", 17) in profile.roles
    assert ("threat", 22) in profile.roles
    assert profile.unknown_cards == 2
    # Green is needed first (one-drop elves), red joins from turn 2 onward.
    assert profile.criteria.colour_priority == ("G", "R")
    # Real mana values across the whole list, copies counted.
    assert dict(profile.curve)[0] == 3
    assert dict(profile.curve)[1] == 16
    assert dict(profile.curve)[2] == 18
    assert dict(profile.curve)[3] == 15
    assert dict(profile.curve)[4] == 7
    # Sources: the 37 lands plus eleven nonland producers ({T}: Add ...);
    # colours include the deck's G/R core plus every colour its any-colour
    # producers (Birds of Paradise, Mana Confluence, ...) can make.
    assert profile.sources.total == 48
    assert {"G", "R"} <= set(profile.sources.colours)
    # Drawbacks counted from real oracle text across the full list.
    assert profile.sources.enters_tapped == 8
    assert profile.sources.restricted == 16
    assert profile.seed_text.startswith("DECK PROFILE")


def test_activation_input_pips_are_not_outputs_bug_20261109_212434():
    """Pips in an activation cost are inputs; only 'add'-clause tails produce.

    Real logged cards: Arena of Glory charges {R} before 'Add {R}{R}' and
    Hidden Volcano charges {4}{R} for its Discover activation.
    """
    arena = _fixture_card("minsc", "Arena of Glory")
    # Both producing clauses survive; the charged {R}, prefix does not.
    assert _segments_of(arena) == [" {R}", " {R}{R}"]
    assert _produced_of(arena) == ({"R"}, False)

    volcano = _fixture_card("mono99", "Hidden Volcano")
    # The {4}{R} Discover activation contributes no production segment.
    assert _segments_of(volcano) == [" {R}"]
    assert _produced_of(volcano) == ({"R"}, False)


def test_any_colour_prose_yields_every_colour_bug_20261109_212434():
    """'Add one mana of any color' is flexible production, not a letter scan.

    Real logged cards: Birds of Paradise and Mana Confluence in the Minsc
    list. The old behaviour collapsed this to whichever letters happened to
    appear in the sentence.
    """
    birds = _fixture_card("minsc", "Birds of Paradise")
    colours, flexible = _produced_of(birds)
    assert flexible is True
    assert colours == set("WUBRG")

    confluence = _fixture_card("minsc", "Mana Confluence")
    colours, flexible = _produced_of(confluence)
    assert flexible is True
    assert colours == set("WUBRG")


def test_production_conditions_retained_bug_20261109_212434():
    """Stated requirements on a producer survive into the profile.

    Real logged cards: Thornspire Verge ('Activate only if you control a
    Mountain or a Forest.') and Delighted Halfling ('Spend this mana only to
    cast a legendary spell').
    """
    verge = _fixture_card("minsc", "Thornspire Verge")
    assert _produced_of(verge) == ({"R", "G"}, False)

    profile = build_profile(_cards("minsc"))
    joined = " | ".join(profile.sources.conditions)
    assert "Activate only if you control a Mountain or a Forest" in joined
    assert "Spend this mana only to cast a legendary spell" in joined
    # Five distinct requirements across the list's producers.
    assert profile.sources.conditional >= 5


def test_spend_as_any_colour_is_not_production_bug_20261109_212434():
    """Spending mana as any colour does not make a card a producer.

    Reviewer probe (bug_20261109_212434): Agatha's Soul Cauldron (real card,
    Scryfall oracle text, {2} Legendary Artifact) says 'You may spend mana as
    though it were mana of any color to activate abilities of creatures you
    control.' It produces no mana, so it must not be counted as a flexible
    WUBRG source in the profile.
    """
    cauldron = {
        "name": "Agatha's Soul Cauldron",
        "type_line": "Legendary Artifact",
        "mana_cost": "{2}",
        "oracle_text": (
            "You may spend mana as though it were mana of any color to activate "
            "abilities of creatures you control.\n"
            "Creatures you control with +1/+1 counters on them have all activated "
            "abilities of all creature cards exiled with Agatha's Soul Cauldron.\n"
            "{T}: Exile target card from a graveyard. When a creature card is exiled "
            "this way, put a +1/+1 counter on target creature you control."
        ),
        "count": 1,
    }
    assert _produced_of(cauldron) == (set(), False)

    profile = build_profile([cauldron])
    assert profile.sources.total == 0
    assert profile.sources.flexible == 0
    assert not profile.sources.colours


# --- bug_20261109_231712 (mono-red 99, complete list) -------------------------


def test_complete_mono99_list_bug_20261109_231712():
    """The logged degenerate deck profiles honestly: 92 of 99 cards are lands."""
    profile = build_profile(_cards("mono99"))
    assert profile.card_count == 99
    assert profile.land_count == 92
    assert profile.sources.total == 92  # all lands; Hidden Volcano is one of them
    assert profile.sources.colours == frozenset({"R"})
    assert profile.sources.enters_tapped == 1  # Hidden Volcano
    assert profile.criteria.colour_priority == ("R",)
    # Real mana values of the seven spell copies: Etali's Favor {2}{R}=MV3,
    # Geological Appraiser {2}{R}{R}=MV4, Throes of Chaos {3}{R}=MV4,
    # Daring Discovery {4}{R}=MV5, Volcanic Torrent {4}{R}=MV5, Meteoric Mace
    # and Trumpeting Carnosaur {4}{R}{R}=MV6.
    assert dict(profile.curve)[3] == 1
    assert dict(profile.curve)[4] == 2
    assert dict(profile.curve)[5] == 2
    assert dict(profile.curve)[6] == 2
    # Seven spell copies: ceiling lands on turn 4.
    assert profile.criteria.early_play_ceiling == 4


# --- match_bundles/00651f77 (60-card mono-red aggro, complete list) -----------


def test_complete_sixty_list_match_bundle_00651f77():
    """Counts are copies across the whole logged 60-card list."""
    profile = build_profile(_cards("sixty"))
    assert profile.card_count == 60
    assert profile.land_count == 20
    assert ("land", 20) in profile.roles
    assert ("removal", 16) in profile.roles
    assert ("threat", 24) in profile.roles
    # Twenty-two MV1 copies, sixteen MV2 copies, two MV3 Cindermaws.
    assert dict(profile.curve)[1] == 22
    assert dict(profile.curve)[2] == 16
    assert dict(profile.curve)[3] == 2
    assert profile.criteria.colour_priority == ("R",)
    # Enough one-drops that the early-play ceiling is MV <= 1.
    assert profile.criteria.early_play_ceiling == 1
    # Projected lands on board by turn 3 from a 20/60 land ratio.
    assert dict(profile.criteria.lands_by_turn)[3] == 3


# --- choice lands --------------------------------------------------------------


def test_multiversal_passage_choice_land_bug_20261109_212434():
    """The choose-a-basic-land-type branch: drawbacks counted; variants ordered."""
    passage = _fixture_card("minsc", "Multiversal Passage")
    profile = build_profile([dict(passage)])
    assert profile.land_count == 1
    assert profile.sources.enters_tapped == 1
    assert profile.sources.restricted == 1

    # land_sequence honours the requested basic-type order for choice lands...
    ordered = list(
        _variants(dict(passage), [], life=20, kinds=("Forest", "Mountain", "Plains", "Island", "Swamp"))
    )
    kinds_seen = [kind for _, _, _, kind in ordered]
    assert kinds_seen[0] == "Forest"
    assert set(kinds_seen) == {"Forest", "Mountain", "Plains", "Island", "Swamp"}
    # ...and ignores it for lands without the choice ability.
    crag = _fixture_card("minsc", "Rootbound Crag")
    plain = list(_variants(dict(crag), [], life=20, kinds=("Forest",)))
    assert len(plain) == 1 and plain[0][3] == ""


def test_wanted_kinds_follow_hand_costs_bug_20261109_212434():
    """Choice-land variant order follows what the hand and command zone need."""
    hand = [
        {"mana_cost": "{2}{R}"},  # Bonecrusher Giant // Stomp
        {"mana_cost": "{3}{R}{R}"},  # Fury
        {"mana_cost": "{2}{G}{G}"},  # Questing Beast
        {"mana_cost": "{0}"},  # Chrome Mox
    ]
    commanders = [types.SimpleNamespace(cost="{2}{R}{G}")]  # Minsc & Beluna
    kinds = _wanted_kinds(hand, commanders)
    # Red is demanded by four pips ({2}{R} + {3}{R}{R} + commander {R})
    # against three green pips ({G}{G} + commander {G}): Mountain first.
    assert kinds[0] == "Mountain" and kinds[1] == "Forest"
    mono_hand = [{"mana_cost": "{R}"}, {"mana_cost": "{1}{R}"}]
    assert _wanted_kinds(mono_hand, [types.SimpleNamespace(cost="{4}{R}{R}")])[0] == ("Mountain")


def test_profile_priority_leads_wanted_kinds_bug_20261109_212434(monkeypatch):
    """The deck profile's colour priority leads the choice-land ranking.

    The hand alone demands red first (four red pips vs three green), but the
    full logged Minsc list needs green earlier (one-drop elves), so the
    profile's ('G', 'R') priority must flip Forest ahead of Mountain.
    """
    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)

    state = {"deck_cards": _expanded_deck_cards("minsc")}
    priority = _profile_colour_priority(state)
    assert priority == ("G", "R")

    hand = [
        {"mana_cost": "{2}{R}"},  # Bonecrusher Giant // Stomp
        {"mana_cost": "{3}{R}{R}"},  # Fury
        {"mana_cost": "{2}{G}{G}"},  # Questing Beast
    ]
    commanders = [types.SimpleNamespace(cost="{2}{R}{G}")]
    with_priority = _wanted_kinds(hand, commanders, priority)
    assert with_priority[0] == "Forest" and with_priority[1] == "Mountain"
    # Without the profile the same hand ranks Mountain first - the priority,
    # not the hand, drives the change.
    without_priority = _wanted_kinds(hand, commanders)
    assert without_priority[0] == "Mountain"


def test_profile_priority_yields_to_cached_playbook_bug_20261109_212434(monkeypatch, tmp_path):
    """Consumer regression: a cached playbook suppresses the profile priority.

    The reviewer's probe (bug_20261109_212434): with a temporary cached
    playbook the mulligan keep guide is suppressed, yet the choice-land
    ordering still followed the profile's ('G', 'R') priority. Plan rule Do4
    makes the playbook win everywhere, so with a cached playbook the profile
    contributes no priority at all and the hand/commander need decides.
    """
    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)

    state = {"deck_cards": _expanded_deck_cards("minsc")}
    identity = deck_identity(state)
    cache_dir = tmp_path / "playbooks"
    cache_dir.mkdir()
    monkeypatch.setattr(CoachEngine, "_playbook_cache_path", lambda ident: cache_dir / f"{identity}.json")

    # No cached playbook: the profile priority is available.
    assert _profile_colour_priority(state) == ("G", "R")

    # Cached playbook: the profile priority is withheld entirely.
    (cache_dir / f"{identity}.json").write_text(json.dumps({"plays": ["hold up interaction"]}))
    assert _profile_colour_priority(state) == ()

    # And the choice-land ranking falls back to hand/commander need.
    hand = [
        {"mana_cost": "{2}{R}"},  # Bonecrusher Giant // Stomp
        {"mana_cost": "{3}{R}{R}"},  # Fury
        {"mana_cost": "{2}{G}{G}"},  # Questing Beast
    ]
    commanders = [types.SimpleNamespace(cost="{2}{R}{G}")]
    kinds = _wanted_kinds(hand, commanders, _profile_colour_priority(state))
    assert kinds[0] == "Mountain" and kinds[1] == "Forest"


# --- card-facts overrides ------------------------------------------------------


def test_facts_override_effect_tags_and_productions():
    """A card-facts record overrides role classification and adds productions."""
    card = {
        "name": "Sanctum Prism",
        "type_line": "Enchantment",
        "mana_cost": "{2}{U}",
        "oracle_text": "At the beginning of your upkeep, draw a card.",
        "count": 2,
    }
    # Without facts: the draw clause makes it card advantage.
    plain = build_profile([dict(card)])
    assert ("card_advantage", 2) in plain.roles

    record = {
        "name": card["name"],
        "type_line": card["type_line"],
        "mana_cost": card["mana_cost"],
        "oracle_text": card["oracle_text"],
        "effects": [{"effect": {"effect_type": "Destroy"}}],
    }
    profile = build_profile([dict(card)], facts={hash_for_card(card): record})
    # The destroy tag outranks the draw clause: removal, twice (copies count).
    assert ("removal", 2) in profile.roles

    land = {
        "name": "Prism Vale",
        "type_line": "Land",
        "mana_cost": "",
        "oracle_text": "{T}: Add {U}.",
        "count": 1,
    }
    land_record = {
        "name": land["name"],
        "type_line": land["type_line"],
        "mana_cost": "",
        "oracle_text": land["oracle_text"],
        "mana": [{"mana": "{U}"}, {"mana": "{W}"}],
    }
    land_profile = build_profile([dict(land)], facts={hash_for_card(land): land_record})
    # Productions from facts extend the source colours beyond the text pips.
    assert land_profile.sources.colours == frozenset({"U", "W"})


def test_engines_require_both_ends_in_deck():
    """Relationship links only become engines when both endpoints are members."""
    a = {
        "name": "Gearsmith Producer",
        "type_line": "Artifact Creature",
        "mana_cost": "{3}",
        "oracle_text": "",
        "count": 1,
    }
    b = {
        "name": "Clockwork Servant",
        "type_line": "Artifact Creature",
        "mana_cost": "{2}",
        "oracle_text": "",
        "count": 1,
    }
    record = {
        "name": a["name"],
        "type_line": a["type_line"],
        "mana_cost": a["mana_cost"],
        "oracle_text": "",
        "relationships": {
            "links": [
                {"from": a["name"], "to": b["name"], "kind": "ramps into"},
                {"from": a["name"], "to": "Absent Card", "kind": "ramps into"},
                {"from": a["name"], "to": a["name"], "kind": "self"},
            ]
        },
    }
    facts = {hash_for_card(a): record}
    facts[hash_for_card(b)] = {
        "name": b["name"],
        "type_line": b["type_line"],
        "mana_cost": b["mana_cost"],
        "oracle_text": "",
    }
    profile = build_profile([dict(a), dict(b)], facts=facts)
    # Only the a->b link has both endpoints in the deck.
    assert [(engine.source, engine.target, engine.kind) for engine in profile.engines] == [
        ("Gearsmith Producer", "Clockwork Servant", "ramps into")
    ]


# --- state plumbing ------------------------------------------------------------


def test_profile_for_state_resolves_deck_cards_bug_20261109_212434(monkeypatch):
    """profile_for_state expands the logged deck_cards into a full profile."""
    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)

    state = {"deck_cards": _expanded_deck_cards("minsc")}
    profile = profile_for_state(state)
    assert profile.card_count == 99
    assert profile.land_count == 37
    assert profile.criteria.colour_priority == ("G", "R")


def test_deck_keep_guide_quoted_in_mulligan_bug_20261109_212434(monkeypatch):
    """The mulligan keep guide quotes the profile's land goal and priorities."""
    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)

    state = {"deck_cards": _expanded_deck_cards("minsc")}
    guide = _deck_keep_guide(state)
    assert guide, "expected a profile-derived keep guide"
    assert guide[0] == (
        "DECK PROFILE KEEP GUIDE: this deck puts ~3 lands in play by turn 3 "
        "(~4 expected drawn) and has cheap plays at MV<=1; colour priority G, R. "
        "Extra land drops are potential only: Exploration (MV 1) grants an extra "
        "land drop on each of your turns once drawn and cast; Icetill Explorer "
        "(MV 4) grants an extra land drop on each of your turns once drawn and cast."
    )
    # No deck list -> no guide line, quietly.
    assert _deck_keep_guide({}) == []


def test_deck_keep_guide_separates_draw_expectation_from_goal_bug_20261109_231712(monkeypatch):
    """Expected lands drawn are availability, not the achievable keep goal.

    The real logged 92-land mono-red Commander list (bug_20261109_231712)
    draws ~9 lands by turn 3 but can only put ~3 in play (one drop per turn
    plus cheap ramp). The keep guide must quote the achievable number as the
    goal and label the draw expectation separately, never ask for 9.
    """
    mapping = {card["grp_id"]: card for card in LISTS["mono99"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)

    state = {"deck_cards": _expanded_deck_cards("mono99")}
    profile = profile_for_state(state)
    assert profile is not None
    drawn = dict(profile.criteria.lands_by_turn)[3]
    in_play = dict(profile.criteria.lands_in_play_by_turn)[3]
    assert drawn == 9
    assert in_play == 3

    guide = _deck_keep_guide(state)
    assert guide, "expected a profile-derived keep guide"
    assert "puts ~3 lands in play by turn 3" in guide[0]
    assert "(~9 expected drawn)" in guide[0]
    assert "wants ~9 lands" not in guide[0]

    # The sixty-card list keeps its own achievable goal (3 lands, 3 drawn).
    mapping = {card["grp_id"]: card for card in LISTS["sixty"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    sixty = profile_for_state({"deck_cards": _expanded_deck_cards("sixty")})
    assert sixty is not None
    assert dict(sixty.criteria.lands_in_play_by_turn)[3] == 3
    assert dict(sixty.criteria.lands_by_turn)[3] == 3


def test_lands_in_play_counts_legal_drops_not_ramp_bug_20261109_212434():
    """Cheap ramp creatures are mana capacity, not extra land drops.

    Round-3 rejection probe, built from real logged cards in
    bug_20261109_212434 (Llanowar Elves, fixture text verbatim): a
    56-Forest + 4-Llanowar-Elves list has no extra-land effect anywhere, so
    its achievable lands in play stay at the ordinary one-drop-per-turn
    baseline (3 by turn 3, 4 by turn 4) even though four cheap ramp copies
    exist. The earlier formula predicted 5 by turn 3 and 7 by turn 4 by
    counting every cheap ramp copy as an extra land drop it never made.
    """
    forest = _fixture_card("minsc", "Forest")
    elves = _fixture_card("minsc", "Llanowar Elves")
    profile = build_profile([dict(forest, count=56), dict(elves, count=4)])
    in_play = dict(profile.criteria.lands_in_play_by_turn)
    drawn = dict(profile.criteria.lands_by_turn)
    assert in_play[3] == 3
    assert in_play[4] == 4
    # Every turn: the ordinary baseline, capped by expected draws.
    assert all(in_play[turn] == min(drawn[turn], turn) for turn in range(1, 6))


def test_extra_land_drop_needs_own_rules_text_bug_20261109_212434():
    """Extra drops are conditional potential from the list's own rules text.

    Real logged cards from bug_20261109_212434: Exploration and Icetill
    Explorer literally grant an additional land drop each turn; Llanowar
    Elves (mana dork), Elvish Mystic (mana dork) and Crop Rotation
    (sacrifice-a-land tutor) do not. The permission never raises the
    achievable baseline — it is reported as labelled conditional potential
    (drawn and cast required) — so the same 56-Forest shell stays on the
    ordinary one-drop-per-turn curve whether it runs Exploration or
    Llanowar Elves.
    """
    assert deck_profile._grants_extra_land_drop(_fixture_card("minsc", "Exploration")["oracle_text"])
    assert deck_profile._grants_extra_land_drop(_fixture_card("minsc", "Icetill Explorer")["oracle_text"])
    for name in ("Llanowar Elves", "Elvish Mystic", "Crop Rotation"):
        assert not deck_profile._grants_extra_land_drop(_fixture_card("minsc", name)["oracle_text"])

    forest = _fixture_card("minsc", "Forest")
    exploration = _fixture_card("minsc", "Exploration")
    profile = build_profile([dict(forest, count=56), dict(exploration, count=4)])
    in_play = dict(profile.criteria.lands_in_play_by_turn)
    drawn = dict(profile.criteria.lands_by_turn)
    # Ordinary baseline everywhere; the permission is potential, not active.
    assert all(in_play[turn] == min(drawn[turn], turn) for turn in range(1, 6))
    assert profile.criteria.extra_land_drop_potential == (
        "Exploration (MV 1) grants an extra land drop on each of your turns once drawn and cast",
    )


def test_icetill_cast_condition_gates_extra_drop_bug_20261109_212434():
    """An uncastable-by-then extra-drop card never lifts the baseline.

    Round-4 rejection probe, real logged card Icetill Explorer from
    bug_20261109_212434 ({2}{G}{G}): with 59 Forests and no ramp the old
    formula predicted 3 lands in play on turn 2 and 4 on turn 3 — before
    the four-mana Explorer could even be cast. The baseline must stay at
    the ordinary one drop per turn (2 by turn 2, 3 by turn 3), with the
    Explorer's grant reported as conditional potential naming the cast
    precondition.
    """
    forest = _fixture_card("minsc", "Forest")
    icetill = _fixture_card("minsc", "Icetill Explorer")
    profile = build_profile([dict(forest, count=59), dict(icetill, count=1)])
    in_play = dict(profile.criteria.lands_in_play_by_turn)
    assert in_play[2] == 2
    assert in_play[3] == 3
    assert profile.criteria.extra_land_drop_potential == (
        "Icetill Explorer (MV 4) grants an extra land drop on each of your turns once drawn and cast",
    )
    # The keep guide labels the potential instead of raising the goal.
    lines = profile.keep_criteria_lines()
    assert "puts ~3 lands in play by turn 3" in lines[0]
    assert "potential only" in lines[0]
    assert "Icetill Explorer (MV 4)" in lines[0]


def test_extra_drop_predicate_requires_self_permission_bug_20261109_212434():
    """Only self-addressed, unnegated play permissions count as potential.

    Real card oracle texts (Scryfall, verified 2026-11-12): Azusa, Lost but
    Seeking grants 'You may play two additional lands on each of your
    turns' (persistent self grant); Summer Bloom grants 'You may play up to
    three additional lands this turn' (one-shot self grant); Rites of
    Flourishing says 'Each player may play an additional land on each of
    their turns' — a permission aimed at every player, not this deck's
    player, so it is not counted (conservative: a shared permission is not
    this deck's own grant). A negated sentence ('you can't play an
    additional land') is a restriction, never a grant — no printed card
    pairs those tokens today (full Scryfall /additional land/ sweep, 41
    cards), the guard is defensive for Arena dialects.
    """
    azusa = {
        "name": "Azusa, Lost but Seeking",
        "type_line": "Legendary Creature — Human Monk",
        "mana_cost": "{2}{G}",
        "oracle_text": "You may play two additional lands on each of your turns.",
        "count": 1,
    }
    summer_bloom = {
        "name": "Summer Bloom",
        "type_line": "Sorcery",
        "mana_cost": "{1}{G}{G}",
        "oracle_text": "You may play up to three additional lands this turn.",
        "count": 1,
    }
    rites = {
        "name": "Rites of Flourishing",
        "type_line": "Enchantment",
        "mana_cost": "{2}{G}",
        "oracle_text": (
            "Each player may play an additional land on each of their turns.\n"
            "At the beginning of each player's draw step, that player draws an extra card."
        ),
        "count": 1,
    }
    negated = {
        "name": "Defensive Probe",
        "type_line": "Enchantment",
        "mana_cost": "{1}{G}",
        # Synthetic sentence covering a shape absent from the printed corpus;
        # see docstring. Guards against Arena dialect rewordings.
        "oracle_text": "You can't play an additional land on each of your turns.",
        "count": 1,
    }

    assert deck_profile._extra_land_drop_grant(azusa["oracle_text"]) == (
        "an extra land drop on each of your turns"
    )
    assert deck_profile._extra_land_drop_grant(summer_bloom["oracle_text"]) == (
        "an extra land drop on one turn"
    )
    assert deck_profile._extra_land_drop_grant(rites["oracle_text"]) is None
    assert deck_profile._extra_land_drop_grant(negated["oracle_text"]) is None

    forest = _fixture_card("minsc", "Forest")
    profile = build_profile([dict(forest, count=56), azusa])
    assert profile.criteria.extra_land_drop_potential == (
        "Azusa, Lost but Seeking (MV 3) grants an extra land drop on each of your turns once drawn and cast",
    )
    # Rites of Flourishing contributes no potential and no baseline lift.
    profile = build_profile([dict(forest, count=56), rites])
    assert profile.criteria.extra_land_drop_potential == ()
    assert dict(profile.criteria.lands_in_play_by_turn)[3] == 3


def test_deck_keep_guide_yields_to_cached_playbook_bug_20261109_212434(monkeypatch, tmp_path):
    """When a playbook is cached for this deck, it wins over the profile."""
    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)

    state = {"deck_cards": _expanded_deck_cards("minsc")}
    identity = deck_identity(state)
    cache_dir = tmp_path / "playbooks"
    cache_dir.mkdir()
    monkeypatch.setattr(CoachEngine, "_playbook_cache_path", lambda ident: cache_dir / f"{identity}.json")
    (cache_dir / f"{identity}.json").write_text(json.dumps({"plays": ["hold up interaction"]}))

    # With a cached playbook the profile keep guide is suppressed entirely.
    assert _deck_keep_guide(state) == []

    # Without one, the profile guide appears.
    (cache_dir / f"{identity}.json").unlink()
    assert _deck_keep_guide(state)


def test_saved_playbook_wins_after_cache_file_cleared_bug_20261111_233000(monkeypatch, tmp_path):
    """A valid saved playbook keeps precedence without the disposable cache file.

    Coordinator audit repro (bug_20261111_233000): a real prepare_playbook
    durably saves the playbook; deleting the disposable on-disk cache leaves
    CoachEngine serving the saved playbook from memory, but the keep guide's
    presence check looked only at the cache file and seeded static profile
    guidance over the saved strategy.
    """
    from unittest.mock import Mock

    from tests.deck_fakes import deck_case

    state, catalog, response = deck_case()
    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    mapping.update(catalog)  # the profile must also resolve the fixture deck
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])

    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )

    # Before any playbook exists, the static profile guide is seeded.
    assert _deck_keep_guide(state), "expected a profile-derived keep guide"

    backend = Mock()
    backend.complete.side_effect = ["notes about the deck", json.dumps(response)]
    playbook = coach.prepare_playbook(state, backend=backend)
    assert list(tmp_path.glob("*.json")), "expected the durable save"

    # Delete the disposable on-disk cache; the coach still serves the saved playbook.
    for saved in tmp_path.glob("*.json"):
        saved.unlink()
    assert coach._cached_playbook(playbook.identity, state) is not None

    # Saved-playbook precedence holds: no static profile keep guide.
    assert _cached_playbook_present(state, coach=coach) is True
    assert _deck_keep_guide(state, coach=coach) == []


def test_invalid_saved_playbook_falls_back_to_profile_guide_bug_20261111_233000(monkeypatch, tmp_path):
    """An unreadable saved playbook must not mask the static profile guide.

    The inverse guard for bug_20261111_233000: when the durable save cannot
    be served (corrupt file, schema drift) and the coach holds no valid
    copy, the keep guide falls back to the static profile instead of
    suppressing guidance entirely.
    """
    from unittest.mock import Mock

    from tests.deck_fakes import deck_case

    state, catalog, response = deck_case()
    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    mapping.update(catalog)
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])

    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )
    backend = Mock()
    backend.complete.side_effect = ["notes about the deck", json.dumps(response)]
    playbook = coach.prepare_playbook(state, backend=backend)
    assert list(tmp_path.glob("*.json"))

    # Corrupt the durable save and drop the in-memory copy: the coach can no
    # longer serve the playbook, so the static profile guidance returns.
    (tmp_path / f"{playbook.identity}.json").write_text("{not json")
    fresh = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        fresh, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )
    assert fresh._cached_playbook(playbook.identity, state) is None
    assert _cached_playbook_present(state, coach=fresh) is False
    assert _deck_keep_guide(state, coach=fresh), "static profile guidance should return"


def test_game_plan_seed_prefers_playbook_then_profile_bug_20261109_212434(monkeypatch):
    """Seed order: playbook render, then the cached playbook seed, then the
    freshly computed deck profile (used only when no playbook exists)."""
    manager = GamePlanManager(backend=None)

    mapping = {card["grp_id"]: card for card in LISTS["minsc"]}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    _stub_facts(monkeypatch)

    state = {"deck_cards": _expanded_deck_cards("minsc")}

    # 1. No playbook anywhere: the freshly computed deck profile seeds the plan.
    seed = manager._seed_text(state)
    assert seed is not None and seed.startswith("DECK PROFILE")
    # The seed carries the full list's own numbers (37 lands in 99 cards).
    assert "37" in seed

    # 2. No playbook and no deck list: no seed at all.
    assert manager._seed_text({}) is None

    # 3. A cached playbook seed wins over the computed profile.
    manager.seed("CACHED PLAYBOOK SEED")
    assert manager._seed_text(state) == "CACHED PLAYBOOK SEED"

    # 4. A live playbook source wins over the cached seed.
    class FakePlaybook:
        def render(self, omit_oracle_for=()):
            return "PLAYBOOK RENDER TEXT"

    manager.set_playbook_source(lambda: FakePlaybook())
    assert manager._seed_text(state) == "PLAYBOOK RENDER TEXT"

    # 5. A failing playbook source falls back to the cached seed...
    def boom():
        raise RuntimeError("playbook unavailable")

    manager.set_playbook_source(boom)
    assert manager._seed_text(state) == "CACHED PLAYBOOK SEED"

    # 6. ...and, with no cached seed either, to the computed profile.
    manager._seed = None
    seed = manager._seed_text(state)
    assert seed is not None and seed.startswith("DECK PROFILE")
