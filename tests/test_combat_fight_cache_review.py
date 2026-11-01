"""Independent behavioral probes for the CI performance candidate; synthetic data only."""

from copy import deepcopy

from arenamcp import combat_protection as cp
from arenamcp import combat_solver as cs


def body(iid, power=3, toughness=3, **kw):
    return dict(
        instance_id=iid,
        name=f"Body {iid}",
        power=power,
        toughness=toughness,
        oracle_text="",
        keywords=[],
        **kw,
    )


def test_empty_raw_protection_removal():
    a = body(801, 4, 4, colors=["R"])
    b = body(802, 2, 2, _card={})
    b["oracle_text"] = "Protection from red"
    cs._RESOLVE_CACHE.clear()
    assert not cs._resolve_attacker_damage(a, [b]).blockers_died
    b["removed_abilities"] = ["protection"]
    actual = cs._resolve_attacker_damage(a, [b])
    expected = cs._resolve_attacker_damage_uncached(a, [b])
    assert actual == expected and actual.blockers_died == [b]


def test_counter_type_changes_conditional_keyword():
    a = body(811, counters={"test": True})
    a["oracle_text"] = "This creature has double strike as long as it's modified."
    cs._RESOLVE_CACHE.clear()
    b = body(812, 1, 4)
    assert not cs._resolve_attacker_damage(a, [b]).blockers_died
    a["counters"]["test"] = 1
    actual = cs._resolve_attacker_damage(a, [b])
    expected = cs._resolve_attacker_damage_uncached(a, [b])
    assert actual == expected and actual.blockers_died == [b]


def test_markup_normalization_is_preserved():
    for field in ("oracle_text", "keywords", "granted_abilities"):
        card = {
            field: "Pro<b>tection</b> from red" if field == "oracle_text" else ["Pro<b>tection</b> from red"]
        }
        assert cp.protection_qualities(card) == frozenset({"R"})
    assert cp.protection_qualities({"oracle_text": "This creature has no protective ability."}) == frozenset()


def test_hit_results_use_current_objects_and_mutable_results_are_fresh():
    a = body(821, 5, 5)
    b = body(822, 2, 2)
    cs._RESOLVE_CACHE.clear()
    first = cs._resolve_attacker_damage(a, [b])
    assert first.blockers_died == [b]
    first.blockers_died.clear()
    first.damage_deltas[999] = 99
    first.counter_deltas[999] = 99
    fresh_a, fresh_b = deepcopy(a), deepcopy(b)
    again = cs._resolve_attacker_damage(fresh_a, [fresh_b])
    assert again.blockers_died[0] is fresh_b
    assert again == cs._resolve_attacker_damage_uncached(fresh_a, [fresh_b])
    assert 999 not in again.damage_deltas and 999 not in again.counter_deltas


def test_dynamic_source_colors_and_future_grants():
    a = body(831, 4, 4, colors=["R"])
    b = body(832, 2, 2)
    b["granted_abilities"] = ["Protection from red"]
    cs._RESOLVE_CACHE.clear()
    assert not cs._resolve_attacker_damage(a, [b]).blockers_died
    a["modified_colors"] = ["U"]
    assert cs._resolve_attacker_damage(a, [b]) == cs._resolve_attacker_damage_uncached(a, [b])
    assert cs._resolve_attacker_damage(a, [b]).blockers_died == [b]
    a["modified_colors"] = ["R"]
    b["_protection_future"] = True
    assert cs._resolve_attacker_damage(a, [b]) == cs._resolve_attacker_damage_uncached(a, [b])
    assert cs._resolve_attacker_damage(a, [b]).blockers_died == [b]


def test_ignored_unknown_counter_values_do_not_crash():
    for value in ([], {}, ["unknown"], {"unknown": 1}):
        a = body(851, counters={"test": value})
        b = body(852, 1, 4)
        a["oracle_text"] = "This creature has double strike as long as it's modified."
        cs._RESOLVE_CACHE.clear()
        assert cs._resolve_attacker_damage(a, [b]) == cs._resolve_attacker_damage_uncached(a, [b])
        assert not cs._resolve_attacker_damage(a, [b]).blockers_died
