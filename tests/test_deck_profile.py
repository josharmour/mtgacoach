"""G1 regression tests: instant deck profile from card facts.

Every fixture below is a verbatim slice of a real logged case (the case id is
in the test name): bug_reports/bug_20261109_212434.json (Minsc & Beluna RG
legendary-matters Brawl deck), bug_reports/bug_20261109_231712.json (mono-red
99 with 91 Mountains), and match_bundles/
00651f77-a4b5-41b7-bfe6-089c5cbcf090.json.gz (60-card mono-red aggro).
Card names appear only as fixture data — the code under test keys on generic
fields (type lines, mana costs, oracle text, effect tags).
"""

from __future__ import annotations

import types

from arenamcp import deck_profile
from arenamcp.card_facts.client import hash_for_card
from arenamcp.deck_profile import (
    Engine,
    build_profile,
    profile_for_state,
    profile_seed_for_state,
)
from arenamcp.game_plan import GamePlanManager
from arenamcp.land_sequence import _variants, _wanted_kinds
from arenamcp.mulligan_policy import _deck_keep_guide

# --- bug_20261109_212434 (Minsc & Beluna) -----------------------------------

MINSC_SLICE = [
    {
        "grp_id": 75553,
        "count": 6,
        "name": "Forest",
        "mana_cost": "",
        "type_line": "Basic Land — Forest",
        "oracle_text": "({T}: Add {G}.)",
    },
    {
        "grp_id": 75555,
        "count": 3,
        "name": "Mountain",
        "mana_cost": "",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "({T}: Add {R}.)",
    },
    {
        "grp_id": 83949,
        "count": 1,
        "name": "Copperline Gorge",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "This land enters tapped unless you control two or fewer other lands.\n"
        "{T}: Add {R} or {G}.",
    },
    {
        "grp_id": 66491,
        "count": 1,
        "name": "Rootbound Crag",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "This land enters tapped unless you control a Mountain or a Forest.\n"
        "{T}: Add {R} or {G}.",
    },
    {
        "grp_id": 97998,
        "count": 1,
        "name": "Multiversal Passage",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "As this land enters, choose a basic land type. Then you may pay 2 life. "
        "If you don't, it enters tapped.\nThis land is the chosen type.",
    },
    {
        "grp_id": 19831,
        "count": 1,
        "name": "Chrome Mox",
        "mana_cost": "{0}",
        "type_line": "Artifact",
        "oracle_text": "Imprint — When this artifact enters, you may exile a nonartifact, nonland card "
        "from your hand.\n{T}: Add one mana of any of the exiled card's colors.",
    },
    {
        "grp_id": 70262,
        "count": 1,
        "name": "Bonecrusher Giant // Stomp",
        "mana_cost": "{2}{R}",
        "type_line": "Creature — Giant",
        "oracle_text": "Whenever this creature becomes the target of a spell, this creature deals "
        "2 damage to that spell's controller.",
    },
    {
        "grp_id": 91167,
        "count": 1,
        "name": "Fury",
        "mana_cost": "{3}{R}{R}",
        "type_line": "Creature — Elemental Incarnation",
        "oracle_text": "Double strike\nWhen this creature enters, it deals 4 damage divided as you choose "
        "among any number of target creatures and/or planeswalkers.\n"
        "Evoke—Exile a red card from your hand.",
    },
    {
        "grp_id": 70318,
        "count": 1,
        "name": "Questing Beast",
        "mana_cost": "{2}{G}{G}",
        "type_line": "Legendary Creature — Beast",
        "oracle_text": "Vigilance, deathtouch, haste\nQuesting Beast can't be blocked by creatures with "
        "power 2 or less.\nCombat damage that would be dealt by creatures you control can't "
        "be prevented.\nWhenever Questing Beast deals combat damage to an opponent, it deals "
        "that much damage to target planeswalker that player controls.",
    },
    {
        "grp_id": 100171,
        "count": 1,
        "name": "Unknown(100171)",
        "mana_cost": "",
        "type_line": "",
        "oracle_text": "",
    },
]


def test_deck_profile_minsc_bug_20261109_212434():
    profile = build_profile([dict(card) for card in MINSC_SLICE])
    assert profile.card_count == 17
    assert profile.land_count == 12
    # Copies, not unique cards: six Forests + three Mountains all count.
    assert ("land", 12) in profile.roles
    assert ("threat", 3) in profile.roles
    assert ("ramp", 1) in profile.roles  # Chrome Mox accelerates mana ({T}: Add ...)
    assert profile.unknown_cards == 1
    # Red is first needed on turn 3 (Bonecrusher Giant {2}{R}), green on turn
    # 4 (Questing Beast {2}{G}{G}); Fury {3}{R}{R} adds more red later.
    assert profile.criteria.colour_priority == ("R", "G")
    # Real mana values: Chrome Mox {0} and the unresolved Unknown(100171) sit
    # at MV0; Bonecrusher Giant {2}{R}=MV3, Questing Beast {2}{G}{G}=MV4,
    # Fury {3}{R}{R}=MV5.
    assert dict(profile.curve)[0] == 2
    assert dict(profile.curve)[3] == 1
    assert dict(profile.curve)[4] == 1
    assert dict(profile.curve)[5] == 1
    # Sources: the twelve lands plus Chrome Mox ({T}: Add ...); colours R and G.
    assert profile.sources.total == 13
    assert profile.sources.colours == frozenset({"R", "G"})
    # Drawbacks counted from the real oracle text: Copperline Gorge,
    # Rootbound Crag and Multiversal Passage can enter tapped; only the
    # Passage asks for life.
    assert profile.sources.enters_tapped == 3
    assert profile.sources.restricted == 1
    assert profile.seed_text.startswith("DECK PROFILE")


def test_multiversal_passage_choice_land_bug_20261109_212434():
    """The choose-a-basic-land-type branch: drawbacks counted; variants ordered."""
    passage = next(card for card in MINSC_SLICE if card["name"] == "Multiversal Passage")
    profile = build_profile([dict(passage)])
    assert profile.land_count == 1
    assert profile.sources.enters_tapped == 1
    assert profile.sources.restricted == 1

    # land_sequence honours the requested basic-type order for choice lands…
    ordered = list(
        _variants(dict(passage), [], life=20, kinds=("Forest", "Mountain", "Plains", "Island", "Swamp"))
    )
    kinds_seen = [kind for _, _, _, kind in ordered]
    assert kinds_seen[0] == "Forest"
    assert set(kinds_seen) == {"Forest", "Mountain", "Plains", "Island", "Swamp"}
    # …and ignores it for lands without the choice ability.
    crag = next(card for card in MINSC_SLICE if card["name"] == "Rootbound Crag")
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


# --- match_bundles/00651f77 (60-card mono-red aggro) ------------------------

SIXTY_SLICE = [
    {
        "grp_id": 65385,
        "count": 20,
        "name": "Mountain",
        "mana_cost": "",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "({T}: Add {R}.)",
    },
    {
        "grp_id": 90492,
        "count": 4,
        "name": "Slickshot Show-Off",
        "mana_cost": "{1}{R}",
        "type_line": "Creature — Bird Wizard",
        "oracle_text": "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 "
        "until end of turn.\nPlot {1}{R} (You may pay {1}{R} and exile this card from your "
        "hand. Cast it as a sorcery on a later turn without paying its mana cost. "
        "Plot only as a sorcery.)",
    },
    {
        "grp_id": 91668,
        "count": 4,
        "name": "Emberheart Challenger",
        "mana_cost": "{1}{R}",
        "type_line": "Creature — Mouse Warrior",
        "oracle_text": "Haste\nProwess (Whenever you cast a noncreature spell, this creature gets +1/+1 "
        "until end of turn.)\nValiant — Whenever this creature becomes the target of a spell "
        "or ability you control for the first time each turn, exile the top card of your "
        "library. Until end of turn, you may play that card.",
    },
    {
        "grp_id": 91676,
        "count": 4,
        "name": "Hired Claw",
        "mana_cost": "{R}",
        "type_line": "Creature — Lizard Mercenary",
        "oracle_text": "Whenever you attack with one or more Lizards, this creature deals 1 damage to "
        "target opponent.\n{1}{R}: Put a +1/+1 counter on this creature. Activate only if "
        "an opponent lost life this turn and only once each turn.",
    },
    {
        "grp_id": 95628,
        "count": 2,
        "name": "Fleeting Effigy",
        "mana_cost": "{R}",
        "type_line": "Creature — Elemental",
        "oracle_text": "Haste\nAt the beginning of your end step, return this creature to its owner's "
        "hand. (Return it only if it's on the battlefield.)\n{2}{R}: This creature gets "
        "+2/+0 until end of turn.",
    },
    {
        "grp_id": 94052,
        "count": 2,
        "name": "Giant Cindermaw",
        "mana_cost": "{2}{R}",
        "type_line": "Creature — Dinosaur Beast",
        "oracle_text": "Trample (This creature can deal excess combat damage to the player or planeswalker "
        "it's attacking.)\nPlayers can't gain life.",
    },
]


def test_copy_counts_not_unique_sixty_match_bundle_00651f77():
    """Counts are copies: 20 Mountains + 16 creature copies profile as such."""
    profile = build_profile([dict(card) for card in SIXTY_SLICE])
    assert profile.card_count == 36
    assert profile.land_count == 20
    assert ("land", 20) in profile.roles
    assert ("threat", 16) in profile.roles
    # Six MV1 copies (Hired Claw x4, Fleeting Effigy x2), eight MV2 copies
    # (Slickshot x4, Emberheart x4), two MV3 Cindermaws.
    assert dict(profile.curve)[1] == 6
    assert dict(profile.curve)[2] == 8
    assert dict(profile.curve)[3] == 2
    assert profile.criteria.colour_priority == ("R",)
    # Enough one-drops that the early-play ceiling is MV <= 1.
    assert profile.criteria.early_play_ceiling == 1
    # Projected lands on board by turn 3 from a 20/36 land ratio.
    assert dict(profile.criteria.lands_by_turn)[3] == 6


# --- bug_20261109_231712 (mono-red 99, 91 Mountains) ------------------------

MONO99_CARDS = [
    {
        "grp_id": 91109,
        "count": 1,
        "name": "Meteoric Mace",
        "mana_cost": "{4}{R}{R}",
        "type_line": "Artifact — Equipment",
        "oracle_text": "Equipped creature gets +4/+0 and has trample.\nEquip {4}\nCascade (When you cast "
        "this spell, exile cards from the top of your library until you exile a nonland card "
        "that costs less. You may cast it without paying its mana cost. Put the exiled cards "
        "on the bottom in a random order.)",
    },
    {
        "grp_id": 65385,
        "count": 91,
        "name": "Mountain",
        "mana_cost": "",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "({T}: Add {R}.)",
    },
    {
        "grp_id": 87325,
        "count": 1,
        "name": "Trumpeting Carnosaur",
        "mana_cost": "{4}{R}{R}",
        "type_line": "Creature — Dinosaur",
        "oracle_text": "Trample\nWhen this creature enters, discover 5.\n{2}{R}, Discard this card: It "
        "deals 3 damage to target creature or planeswalker.",
    },
    {
        "grp_id": 87301,
        "count": 1,
        "name": "Geological Appraiser",
        "mana_cost": "{2}{R}{R}",
        "type_line": "Creature — Human Artificer",
        "oracle_text": "When this creature enters, if you cast it, discover 3. (Exile cards from the top "
        "of your library until you exile a nonland card with mana value 3 or less. Cast it "
        "without paying its mana cost or put it into your hand. Put the rest on the bottom "
        "in a random order.)",
    },
    {
        "grp_id": 87300,
        "count": 1,
        "name": "Etali's Favor",
        "mana_cost": "{2}{R}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature you control\nWhen this Aura enters, discover 3. (Exile cards from "
        "the top of your library until you exile a nonland card with mana value 3 or less. "
        "Cast it without paying its mana cost or put it into your hand. Put the rest on the "
        "bottom in a random order.)\nEnchanted creature gets +1/+1 and has trample.",
    },
    {
        "grp_id": 87443,
        "count": 1,
        "name": "Hidden Volcano",
        "mana_cost": "",
        "type_line": "Land — Cave",
        "oracle_text": "This land enters tapped.\n{T}: Add {R}.\n{4}{R}, {T}, Sacrifice this land: "
        "Discover 4. Activate only as a sorcery. (Exile cards from the top of your library "
        "until you exile a nonland card with mana value 4 or less. Cast it without paying "
        "its mana cost or put it into your hand. Put the rest on the bottom in a random "
        "order.)",
    },
    {
        "grp_id": 87291,
        "count": 1,
        "name": "Daring Discovery",
        "mana_cost": "{4}{R}",
        "type_line": "Sorcery",
        "oracle_text": "Up to three target creatures can't block this turn.\nDiscover 4. (Exile cards from "
        "the top of your library until you exile a nonland card with mana value 4 or less. "
        "Cast it without paying its mana cost or put it into your hand. Put the rest on the "
        "bottom in a random order.)",
    },
    {
        "grp_id": 71491,
        "count": 1,
        "name": "Throes of Chaos",
        "mana_cost": "{3}{R}",
        "type_line": "Sorcery",
        "oracle_text": "Cascade (When you cast this spell, exile cards from the top of your library until "
        "you exile a nonland card that costs less. You may cast it without paying its mana "
        "cost. Put the exiled cards on the bottom in a random order.)\nRetrace (You may cast "
        "this card from your graveyard by discarding a land card in addition to paying its "
        "other costs.)",
    },
    {
        "grp_id": 98652,
        "count": 1,
        "name": "Volcanic Torrent",
        "mana_cost": "{4}{R}",
        "type_line": "Sorcery",
        "oracle_text": "Cascade (When you cast this spell, exile cards from the top of your library until "
        "you exile a nonland card that costs less. You may cast it without paying its mana "
        "cost. Put the exiled cards on the bottom in a random order.)\nVolcanic Torrent deals "
        "X damage to each creature and planeswalker your opponents control, where X is the "
        "number of spells you've cast this turn.",
    },
]


def test_mono99_ninety_one_mountains_bug_20261109_231712():
    """The logged degenerate deck profiles honestly: 92 of 99 cards are lands."""
    profile = build_profile([dict(card) for card in MONO99_CARDS])
    assert profile.card_count == 99
    assert profile.land_count == 92
    assert profile.sources.total == 92  # all lands; Hidden Volcano is one of them
    assert profile.sources.colours == frozenset({"R"})
    assert profile.sources.enters_tapped == 1  # Hidden Volcano
    assert profile.criteria.colour_priority == ("R",)
    # Real mana values: Etali's Favor {2}{R}=MV3, Geological Appraiser
    # {2}{R}{R}=MV4, Throes of Chaos {3}{R}=MV4, Daring Discovery
    # {4}{R}=MV5, Volcanic Torrent {4}{R}=MV5, Meteoric Mace and
    # Trumpeting Carnosaur {4}{R}{R}=MV6.
    assert dict(profile.curve)[3] == 1  # Etali's Favor
    assert dict(profile.curve)[4] == 2  # Geological Appraiser / Throes of Chaos
    assert dict(profile.curve)[5] == 2  # Daring Discovery / Volcanic Torrent
    assert dict(profile.curve)[6] == 2  # Meteoric Mace / Trumpeting Carnosaur
    # Seven spell copies: ceiling lands on turn 4.
    assert profile.criteria.early_play_ceiling == 4


# --- card-facts overrides ----------------------------------------------------


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
    profile = build_profile([dict(a), dict(b)], facts={hash_for_card(a): record})
    assert profile.engines == (Engine(source=a["name"], target=b["name"], kind="ramps into"),)
    # Engine endpoints become key cards ahead of ordinary threats.
    assert a["name"] in profile.key_cards and b["name"] in profile.key_cards


# --- state plumbing ----------------------------------------------------------


def _fake_resolve(mapping):
    def resolve(grp_id):
        card = dict(mapping.get(int(grp_id), {}))
        card.setdefault("name", f"Unknown({grp_id})")
        return card

    return resolve


def test_profile_for_state_resolves_deck_cards_bug_20261109_212434(monkeypatch):
    """profile_for_state walks state['deck_cards'], resolves ids, counts copies."""
    mapping = {card["grp_id"]: card for card in MINSC_SLICE}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))

    def fake_facts(cards):
        return {}

    import arenamcp.card_facts.client as facts_client

    monkeypatch.setattr(facts_client, "facts_for", fake_facts)

    deck_cards = []
    for card in MINSC_SLICE:
        deck_cards.extend([card["grp_id"]] * card["count"])
    profile = profile_for_state({"deck_cards": deck_cards})
    assert profile is not None
    assert profile.card_count == 17
    assert profile.land_count == 12
    assert profile.seed_text.startswith("DECK PROFILE")

    # Nothing to profile: empty or unparseable deck lists yield None / "".
    assert profile_for_state({}) is None
    assert profile_for_state({"deck_cards": ["not-an-int"]}) is None
    assert profile_seed_for_state({}) == ""


def test_deck_keep_guide_quoted_in_mulligan_bug_20261109_212434(monkeypatch):
    """The mulligan prompt carries the profile's keep criteria when available."""
    real = deck_profile.profile_for_state

    def seeded(state):
        if state.get("deck_cards"):
            return build_profile([dict(card) for card in MINSC_SLICE])
        return real(state)

    monkeypatch.setattr(deck_profile, "profile_for_state", seeded)

    guide = _deck_keep_guide({"deck_cards": [75553] * 6})
    assert guide and guide[0].startswith("DECK PROFILE KEEP GUIDE")
    # 12/17 lands ≈ 71% → ~7 lands projected by turn 3.
    assert "~7 lands" in guide[0]
    # Cheapest three spell copies reach up to MV3 (Chrome Mox and the
    # unresolved Unknown at MV0, Bonecrusher Giant at MV3): the guide
    # quotes MV<=3.
    assert "MV<=3" in guide[0]
    assert "colour priority R, G" in guide[0]

    # No deck list → no guide line, quietly.
    assert _deck_keep_guide({}) == []


def test_game_plan_seed_prefers_playbook_then_profile(monkeypatch):
    """Seed order: playbook render, then the cached playbook seed, then the
    freshly computed deck profile (used only when no playbook exists)."""
    manager = GamePlanManager(backend=None)

    mapping = {card["grp_id"]: card for card in MINSC_SLICE}
    monkeypatch.setattr(deck_profile, "_resolve", _fake_resolve(mapping))
    import arenamcp.card_facts.client as facts_client

    monkeypatch.setattr(facts_client, "facts_for", lambda cards: {})

    deck_cards = []
    for card in MINSC_SLICE:
        deck_cards.extend([card["grp_id"]] * card["count"])
    state = {"deck_cards": deck_cards}

    # 1. No playbook anywhere: the freshly computed deck profile seeds the plan.
    seed = manager._seed_text(state)
    assert seed is not None and seed.startswith("DECK PROFILE")

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

    # 5. A failing playbook source falls back to the cached seed…
    def boom():
        raise RuntimeError("playbook unavailable")

    manager.set_playbook_source(boom)
    assert manager._seed_text(state) == "CACHED PLAYBOOK SEED"

    # 6. …and, with no cached seed either, to the computed profile.
    manager._seed = None
    seed = manager._seed_text(state)
    assert seed is not None and seed.startswith("DECK PROFILE")
