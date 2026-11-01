"""#308/G04 helper tests: entry -> draw -> opposing ping/amass transition.

The engine and punisher use the real Enduring Innocence / Orcish Bowmasters
text. "Test Recruit" is a fictional simplified creature used only to exercise
the transition; this reconstructs mechanics and is not a historical replay.
"""

import copy
from dataclasses import fields

import pytest
from tests.test_tactical_timing_backlog import card

from arenamcp.entry_draw_transition import (
    Branch,
    EngineAvailability,
    Resolution,
    resolve,
)
from arenamcp.sacrifice_continuation import UnsupportedContinuation

INNOCENCE_TEXT = (
    "Lifelink\n"
    "Whenever one or more other creatures you control with power 2 or less enter, draw a card. "
    "This ability triggers only once each turn.\n"
    "When Enduring Innocence dies, if it was a creature, return it to the battlefield under its owner's "
    "control. It's an enchantment. (It's not a creature.)"
)
BOWMASTERS_TEXT = (
    "Flash\n"
    "When Orcish Bowmasters enters the battlefield, it deals 1 damage to any target.\n"
    "Whenever an opponent draws a card except the first one they draw in each of their draw steps, "
    "Orcish Bowmasters deals 1 damage to any target. Then amass Orcs 1."
)


def innocence(iid: int = 70) -> dict:
    return card(
        iid,
        "Enduring Innocence",
        INNOCENCE_TEXT,
        "Enchantment Creature — Sheep Glimmer",
        "{1}{W}{W}",
        power=2,
        toughness=1,
        card_types=["CardType_Enchantment", "CardType_Creature"],
        subtypes=["Sheep", "Glimmer"],
        keywords=["Lifelink"],
    )


def bowmasters(iid: int = 320) -> dict:
    return card(
        iid,
        "Orcish Bowmasters",
        BOWMASTERS_TEXT,
        "Creature — Orc Archer",
        "{1}{B}",
        seat=2,
        power=1,
        toughness=1,
        colors=["Black"],
        keywords=["Flash"],
    )


def recruit(iid: int = 71, power: int = 2, toughness: int = 2, **kw) -> dict:
    text = kw.pop("text", "")
    return card(
        iid,
        "Test Recruit",
        text,
        "Creature — Human Soldier",
        "{W}",
        power=power,
        toughness=toughness,
        **kw,
    )


def army(iid: int = 500, power: int = 1, toughness: int = 1, counters=None) -> dict:
    return card(
        iid,
        "Orc Army",
        "",
        "Token Creature — Orc Army",
        "",
        seat=2,
        power=power,
        toughness=toughness,
        is_token=True,
        card_types=["CardType_Creature"],
        subtypes=["Orc", "Army"],
        counters={} if counters is None else counters,
    )


def army_factory(iid: int = 900):
    def make() -> dict:
        return card(
            iid,
            "Orc Army",
            "",
            "Token Creature — Orc Army",
            "",
            seat=2,
            power=0,
            toughness=0,
            is_token=True,
            card_types=["CardType_Creature"],
            subtypes=["Orc", "Army"],
        )

    return make


def run(ours, theirs, entering=None, **kw):
    if entering is None:
        entering = [ours[-1]]
    if "engine_use" not in kw:
        engine = next(card for card in ours if card["name"] == "Enduring Innocence")
        kw["engine_use"] = {engine["instance_id"]: EngineAvailability.entered_unused()}
    kw.setdefault("our_life", 20)
    kw.setdefault("opp_life", 20)
    return resolve(entering=entering, ours=ours, theirs=theirs, **kw)


def test_killed_entrant_and_face_branches_preserve_inputs_and_ids():
    engine = innocence()
    entrant = recruit(toughness=1)
    bow = bowmasters()
    ours = [engine, entrant]
    theirs = [bow]
    before_ours = copy.deepcopy(ours)
    before_theirs = copy.deepcopy(theirs)

    result = run(ours, theirs, [entrant], new_army=army_factory())

    assert result.chain is not None
    assert result.chain.draws == 1
    assert result.chain.punisher_id == 320
    assert result.chain.once is True
    # The opponent may also ping the real engine itself; that is a separate branch.
    assert [branch.target for branch in result.branches] == [None, 70, 71]

    face = result.branches[0]
    engine_branch = result.branches[1]
    killed = result.branches[2]
    assert face.draws == killed.draws == 1
    assert face.our_life == 19 and face.opp_life == 20
    assert [body["instance_id"] for body in face.ours] == [70, 71]
    assert face.died == () and face.returned == ()
    assert face.used_after == frozenset({70})
    assert face.army_id == killed.army_id == 900
    assert face.army_counters == killed.army_counters == 1

    new_token = next(body for body in face.theirs if body["instance_id"] == 900)
    assert new_token["power"] == new_token["toughness"] == 1
    assert new_token["counters"] == {"+1/+1": 1}

    assert killed.our_life == 20 and killed.died == (71,) and killed.returned == ()
    assert [body["instance_id"] for body in killed.ours] == [70]
    assert killed.used_after == frozenset({70})

    # The engine-target branch keeps the supported death return, and the fresh
    # returned permanent does not inherit the dead object's consumed use.
    assert engine_branch.died == (70,) and engine_branch.returned == (70,)
    assert engine_branch.used_after == frozenset()

    # No caller-state mutation and no invented drawn-card identity.
    assert ours == before_ours and theirs == before_theirs


def test_existing_single_army_grows_and_no_new_token_is_needed():
    engine = innocence()
    entrant = recruit(toughness=3)
    bow = bowmasters()
    existing = army(power=2, toughness=2, counters={"+1/+1": 1})
    theirs = [bow, existing]

    result = run([engine, entrant], theirs, [entrant], new_army=None)

    assert [branch.target for branch in result.branches] == [None, 70, 71]
    for branch in result.branches:
        grown = next(body for body in branch.theirs if body["instance_id"] == 500)
        assert grown["power"] == grown["toughness"] == 3
        assert grown["counters"] == {"+1/+1": 2}
        assert branch.army_id == 500 and branch.army_counters == 2
        assert all(body["instance_id"] != 900 for body in branch.theirs)
        assert next(body for body in branch.ours if body["instance_id"] == 71)["toughness"] == 3
    assert result.branches[0].died == ()
    assert result.branches[2].died == ()

    # The observed Army input keeps its original counters.
    assert existing["counters"] == {"+1/+1": 1}


def test_existing_non_orc_army_gains_orc_subtype():
    engine = innocence()
    entrant = recruit(toughness=3)
    existing = army(power=2, toughness=2, counters={"+1/+1": 1})
    existing["subtypes"] = ["Army"]
    existing["type_line"] = "Token Creature — Army"

    result = run([engine, entrant], [bowmasters(), existing], [entrant], new_army=None)

    for branch in result.branches:
        grown = next(body for body in branch.theirs if body["instance_id"] == 500)
        assert "Orc" in grown["subtypes"]
        assert "Army" in grown["subtypes"]
        assert grown["type_line"].endswith("Orc Army")
        assert grown["power"] == grown["toughness"] == 3
    assert existing["subtypes"] == ["Army"]
    assert existing["type_line"] == "Token Creature — Army"


def test_name_suffix_is_not_army_when_subtypes_disagree():
    fake = card(
        510,
        "Orc Army",
        "",
        "Creature — Orc",
        "",
        seat=2,
        power=1,
        toughness=1,
        subtypes=["Orc"],
    )
    result = run(
        [innocence(), recruit()],
        [bowmasters(), fake],
        [recruit()],
        new_army=army_factory(),
    )

    assert all(branch.army_id == 900 for branch in result.branches)
    for branch in result.branches:
        untouched = next(body for body in branch.theirs if body["instance_id"] == 510)
        assert untouched["power"] == untouched["toughness"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("keywords", ["lifelink"]), ("granted_abilities", ["Deathtouch"])],
)
def test_granted_source_keywords_abstain(field, value):
    bow = bowmasters()
    bow[field] = value
    with pytest.raises(UnsupportedContinuation):
        run([innocence(), recruit()], [bow], [recruit()], new_army=army_factory())


def test_killing_the_real_engine_applies_supported_death_return():
    engine = innocence()
    entrant = recruit(toughness=2)
    bow = bowmasters()

    result = run([engine, entrant], [bow], [entrant], new_army=army_factory())

    assert [branch.target for branch in result.branches] == [None, 70, 71]
    engine_branch = next(branch for branch in result.branches if branch.target == 70)
    assert engine_branch.died == (70,) and engine_branch.returned == (70,)
    assert engine_branch.used_after == frozenset()
    assert [body["instance_id"] for body in engine_branch.ours] == [71]
    returned = next(card for card in engine_branch.persistent if card["instance_id"] == 70)
    assert returned["type_line"] == "Enchantment"
    assert returned["power"] is None and returned["toughness"] is None
    assert returned["controller_seat_id"] == 1
    assert not any(key in returned for key in ("used", "_used", "used_this_turn"))
    face = next(branch for branch in result.branches if branch.target is None)
    assert face.used_after == frozenset({70})


def test_known_used_engine_yields_no_trigger():
    engine = innocence()
    entrant = recruit()
    result = run(
        [engine, entrant],
        [bowmasters()],
        [entrant],
        engine_use={70: EngineAvailability.entered_used()},
        new_army=army_factory(),
    )
    assert result == Resolution(None, ())


def test_pre_existing_unknown_or_missing_availability_abstains():
    engine = innocence()
    entrant = recruit()
    bow = bowmasters()
    with pytest.raises(UnsupportedContinuation):
        run(
            [engine, entrant],
            [bow],
            [entrant],
            engine_use={70: EngineAvailability.pre_existing_unknown()},
            new_army=army_factory(),
        )
    with pytest.raises(UnsupportedContinuation):
        run([engine, entrant], [bow], [entrant], engine_use={}, new_army=army_factory())


def test_no_punisher_draw_only_branch():
    engine = innocence()
    entrant = recruit()
    result = run([engine, entrant], [], [entrant])

    assert result.chain is not None
    assert result.chain.punisher_id is None
    assert result.chain.draws == 1
    assert result.chain.once is True
    assert len(result.branches) == 1
    branch = result.branches[0]
    assert branch.target is None
    assert branch.our_life == 20 and branch.opp_life == 20
    assert branch.army_id is None and branch.army_counters == 0
    assert branch.used_after == frozenset({70})
    assert [body["instance_id"] for body in branch.ours] == [70, 71]
    assert branch.theirs == ()


def test_only_the_engine_entering_is_not_a_trigger():
    engine = innocence()
    result = run([engine], [bowmasters()], [engine], new_army=army_factory())
    assert result == Resolution(None, ())


def test_power_above_limit_is_not_a_trigger():
    entrant = recruit(power=3, toughness=3)
    result = run([innocence(), entrant], [bowmasters()], [entrant], new_army=army_factory())
    assert result == Resolution(None, ())


@pytest.mark.parametrize("keyword", ["hexproof", "protection from black"])
def test_legally_untargetable_entrant_is_not_a_branch(keyword):
    entrant = recruit(keywords=[keyword])
    result = run([innocence(), entrant], [bowmasters()], [entrant], new_army=army_factory())
    # The engine itself remains a legal target; only the protected entrant is omitted.
    assert [branch.target for branch in result.branches] == [None, 70]


def test_unmodelled_ward_payment_rejects_the_target():
    entrant = recruit(keywords=["ward {2}"])
    with pytest.raises(UnsupportedContinuation):
        run([innocence(), entrant], [bowmasters()], [entrant], new_army=army_factory())


def test_indestructible_target_survives_lethal_ping_with_marked_damage():
    entrant = recruit(toughness=1, keywords=["indestructible"])
    result = run([innocence(), entrant], [bowmasters()], [entrant], new_army=army_factory())

    branch = next(branch for branch in result.branches if branch.target == 71)
    body = next(body for body in branch.ours if body["instance_id"] == 71)
    assert body["_combat_damage"] == 1
    assert branch.died == ()


def test_existing_marked_damage_adds_to_ping_for_death():
    entrant = recruit(toughness=3)
    entrant["_combat_damage"] = 2
    result = run([innocence(), entrant], [bowmasters()], [entrant], new_army=army_factory())

    branch = next(branch for branch in result.branches if branch.target == 71)
    assert branch.died == (71,)
    # The caller's observed body keeps its original marked damage.
    assert entrant["_combat_damage"] == 2


def test_multiple_draw_engines_abstain():
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(70), innocence(72), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
        )


def test_multiple_opposing_armies_abstain():
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters(), army(500), army(501)],
            [entrant],
            new_army=army_factory(),
        )


def test_non_engine_death_return_abstains():
    text = (
        "When Test Recruit dies, if it was a creature, return it to the battlefield under its owner's "
        "control. It's an enchantment."
    )
    entrant = recruit(toughness=1, text=text)
    with pytest.raises(UnsupportedContinuation):
        run([innocence(), entrant], [bowmasters()], [entrant], new_army=army_factory())


def test_fictional_engine_without_supported_return_text_abstains_on_death():
    text = (
        "Whenever one or more other creatures you control with power 2 or less enter, draw a card. "
        "This ability triggers only once each turn."
    )
    engine = card(73, "Test Engine", text, "Creature — Test", "{1}{W}", power=2, toughness=1)
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [engine, entrant],
            [bowmasters()],
            [entrant],
            engine_use={73: EngineAvailability.entered_unused()},
            new_army=army_factory(),
        )


def test_planeswalker_ping_targets_abstain():
    walker = card(80, "Test Walker", "", "Planeswalker — Test", "{2}", loyalty=3)
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            our_walkers=[walker],
        )


def test_draw_replacement_on_the_opposing_board_abstains():
    replacement = card(
        81,
        "Test Restriction",
        "If an opponent would draw a card, instead that player skips that draw.",
        "Enchantment",
        "{1}{B}",
        seat=2,
    )
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            their_permanents=[replacement],
        )


def test_draw_payoff_on_the_local_board_abstains():
    payoff = card(
        82,
        "Test Payoff",
        "Whenever you draw a card, you gain 1 life.",
        "Enchantment",
        "{1}{W}",
    )
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            our_permanents=[payoff],
        )


def test_trigger_doubling_on_the_local_board_abstains():
    doubler = card(
        83,
        "Test Doubler",
        "Each triggered ability triggers an additional time.",
        "Enchantment",
        "{2}{W}",
    )
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            our_permanents=[doubler],
        )


def test_damage_prevention_on_the_opposing_board_abstains():
    shield = card(
        84,
        "Test Shield",
        "Prevent all damage that would be dealt to creatures you control.",
        "Enchantment",
        "{1}{W}",
        seat=2,
    )
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            their_permanents=[shield],
        )


def test_death_payoff_on_the_local_board_abstains_when_a_body_dies():
    mourner = card(
        85,
        "Test Mourner",
        "Whenever a creature dies, you gain 1 life.",
        "Creature — Human",
        "{1}{W}",
        power=1,
        toughness=1,
    )
    entrant = recruit(toughness=1)
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            our_permanents=[mourner],
        )


def test_branch_shape_carries_no_invented_drawn_card_field():
    assert {field.name for field in fields(Branch)} == {
        "target",
        "ours",
        "theirs",
        "persistent",
        "our_life",
        "opp_life",
        "draws",
        "army_id",
        "army_counters",
        "used_after",
        "died",
        "returned",
    }


@pytest.mark.parametrize(
    "text",
    [
        "Each triggered ability triggers an additional time.",
        "Triggered abilities of creatures you control trigger an additional time.",
    ],
)
def test_opposing_trigger_doubler_abstains_for_singular_and_plural_wording(text):
    doubler = card(820, "Test Opposing Doubler", text, "Enchantment", "{2}", seat=2)
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            their_permanents=[doubler],
        )


@pytest.mark.parametrize(
    "text",
    [
        "Whenever a creature enters under your control, you gain 1 life.",
        "Whenever another creature enters the battlefield under your control, you gain 1 life.",
    ],
)
def test_opposing_entry_payoff_abstains_for_generic_entry_wording(text):
    payoff = card(821, "Test Army Entry Payoff", text, "Enchantment", "{2}", seat=2)
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            their_permanents=[payoff],
        )


def test_existing_army_growth_does_not_fire_an_opposing_entry_payoff():
    payoff = card(
        822,
        "Test Army Entry Payoff",
        "Whenever a creature enters under your control, you gain 1 life.",
        "Enchantment",
        "{2}",
        seat=2,
    )
    entrant = recruit()
    result = run(
        [innocence(), entrant],
        [bowmasters(), army(power=2, toughness=2, counters={"+1/+1": 1})],
        [entrant],
        their_permanents=[payoff],
    )
    assert result.branches
    assert {branch.army_counters for branch in result.branches} == {2}


def test_unrelated_opposing_text_does_not_abstain():
    unrelated = card(
        823,
        "Test Unrelated Payoff",
        "Whenever a creature attacks, you gain 1 life.",
        "Enchantment",
        "{2}",
        seat=2,
    )
    entrant = recruit()
    result = run(
        [innocence(), entrant],
        [bowmasters()],
        [entrant],
        new_army=army_factory(),
        their_permanents=[unrelated],
    )
    assert result.branches


def test_any_player_entry_payoff_on_the_local_board_abstains():
    observer = card(
        824,
        "Test Observer",
        "Whenever another creature enters the battlefield, you gain 1 life.",
        "Creature — Human",
        "{1}{W}",
        power=1,
        toughness=1,
    )
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            our_permanents=[observer],
        )


def test_local_controller_scoped_entry_payoff_does_not_fire_on_their_army():
    observer = card(
        825,
        "Test Observer",
        "Whenever a creature enters under your control, you gain 1 life.",
        "Creature — Human",
        "{1}{W}",
        power=1,
        toughness=1,
    )
    entrant = recruit()
    result = run(
        [innocence(), entrant],
        [bowmasters()],
        [entrant],
        new_army=army_factory(),
        our_permanents=[observer],
    )
    assert result.branches


def test_local_opponent_scoped_entry_payoff_fires_on_their_army():
    observer = card(
        826,
        "Test Observer",
        "Whenever a creature enters under an opponent's control, you gain 1 life.",
        "Creature — Human",
        "{1}{W}",
        power=1,
        toughness=1,
    )
    entrant = recruit()
    with pytest.raises(UnsupportedContinuation):
        run(
            [innocence(), entrant],
            [bowmasters()],
            [entrant],
            new_army=army_factory(),
            our_permanents=[observer],
        )


def test_existing_army_growth_does_not_fire_a_local_entry_payoff():
    observer = card(
        827,
        "Test Observer",
        "Whenever another creature enters the battlefield, you gain 1 life.",
        "Creature — Human",
        "{1}{W}",
        power=1,
        toughness=1,
    )
    entrant = recruit()
    result = run(
        [innocence(), entrant],
        [bowmasters(), army(power=2, toughness=2, counters={"+1/+1": 1})],
        [entrant],
        our_permanents=[observer],
    )
    assert result.branches
    assert {branch.army_counters for branch in result.branches} == {2}
