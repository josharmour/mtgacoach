"""A separate self-sacrifice draw option does not change a tap-for-one source."""

import pytest
from tests.strategic_states import card

from arenamcp import line_search_moves as lsm
from test_line_search import _a1_board, _a1_variants


def rock(text, name="Independent rock"):
    return dict(name=name, type_line="Artifact", mana_cost="{2}", oracle_text=text)


@pytest.mark.parametrize("tap,one,mana", [("{T}", "{1}", "{C}"), ("{oT}", "{o1}", "{oC}")])
@pytest.mark.parametrize("reference", ["this artifact", "Independent rock"])
def test_independent_draw_keeps_exact_single_color_source(tap, one, mana, reference, monkeypatch):
    raw = rock(f"{tap}: Add {mana}.\n{one}, {tap}, Sacrifice {reference}: Draw a card.")
    assert lsm._plain_rock_output(raw) == frozenset({"C"})
    assert lsm.unmodelled_effect(raw) == ""
    monkeypatch.setattr(lsm, "facts_for", lambda cards: {})
    original = lsm.Moves._apply_casts
    seen = []

    def inspect(self, mid, *args, **kwargs):
        original(self, mid, *args, **kwargs)
        matches = [r for r in mid.rocks if r.name == raw["name"]]
        if matches:
            assert len(matches) == 1
            assert matches[0].produces == frozenset({"C"})
            seen.append(True)

    monkeypatch.setattr(lsm.Moves, "_apply_casts", inspect)
    source = _a1_board(raw)
    source["battlefield"].extend(card(i, "Mountain", 1, is_tapped=False) for i in (910, 911))
    _a1_variants(source)
    assert seen


@pytest.mark.parametrize(
    "text",
    [
        "{T}, Sacrifice this artifact: Add {C}.",
        "{T}: Add {C}{C}.\n{1}, {T}, Sacrifice this artifact: Draw a card.",
        "{T}: Add {C}.\n{1}, {T}, Sacrifice another artifact: Draw a card.",
        "{T}: Add {C}.\n{1}, {T}, Sacrifice an artifact: Draw a card.",
        "{T}: Add {C}.\n{1}, {T}, Sacrifice Other rock: Draw a card.",
        "{T}: Add {C}.\n{1}, {T}, Sacrifice this artifact: Draw a card and add {R}.",
        "{T}: Add {C}.\n{1}, {T}, Sacrifice this artifact: Draw a card. Activate only if you control a creature.",
        "{T}: Add {C}.\nThis artifact enters tapped.\n{1}, {T}, Sacrifice this artifact: Draw a card.",
        "{T}: Add {C}. Spend this mana only to cast artifacts.\n{1}, {T}, Sacrifice this artifact: Draw a card.",
        "{T}: Add {C}.\nWhenever you draw a card, add {R}.\n{1}, {T}, Sacrifice this artifact: Draw a card.",
        "{1}, {T}, Sacrifice this artifact: Draw a card.",
    ],
)
def test_unrelated_costs_restrictions_and_extra_mana_stay_unmodelled(text, monkeypatch):
    monkeypatch.setattr(lsm, "facts_for", lambda cards: {})
    raw = rock(text)
    assert not lsm._plain_rock_output(raw)
    variants, _ = _a1_variants(_a1_board(raw))["Independent rock"]
    assert not any(v.rock for v in variants)


def test_named_self_reference_is_literal_not_regex():
    raw = rock("{T}: Add {C}.\n{1}, {T}, Sacrifice Different: Draw a card.", name=".*")
    assert not lsm._plain_rock_output(raw)
