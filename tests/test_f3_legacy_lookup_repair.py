import dataclasses

import pytest

from arenamcp import line_search_moves as lsm
from arenamcp.card_facts.client import hash_for_card
from test_line_search import _CHROME_MOX_CARD, _a1_board, _a1_variants


@pytest.mark.parametrize("text", ["<i>Bad</nobr>", "<bad>Text</bad>", "{oJ}", "{oT", "{oG//oR}"])
def test_legacy_lookup_still_refuses_malformed_rendering(text):
    assert hash_for_card({**_CHROME_MOX_CARD, "oracle_text": text}) is None


def test_no_record_imprint_transition_never_grants_free_mana(monkeypatch):
    monkeypatch.setattr(lsm, "facts_for", lambda cards: {})
    apply = lsm.Moves._apply_casts
    observed = []

    def guarded(self, mid, *args, **kwargs):
        apply(self, mid, *args, **kwargs)
        for var, _ in mid.casts:
            if self.spells[var.spell].name == "Chrome Mox":
                observed.append(True)
                assert not any(rock.name == "Chrome Mox" for rock in mid.rocks)
                assert not var.imprint_rock
                # The legacy generic flag cannot receive the mana-rock valuation
                # bonus either. Compare identical variants with only that flag off.
                assert self.item_value(var, None, mid.theirs) == self.item_value(
                    dataclasses.replace(var, rock=False), None, mid.theirs
                )

    monkeypatch.setattr(lsm.Moves, "_apply_casts", guarded)
    variants = _a1_variants(_a1_board(_CHROME_MOX_CARD))
    assert variants["Chrome Mox"] == ((), None)
    assert not observed


@pytest.mark.parametrize(
    "replace_from,replace_to",
    [
        ("card's", "card’s"),
        ("card's colors", "card's colours"),
        ("mana of", "mana  of"),
    ],
)
def test_unbound_imprint_wording_cannot_be_modelled(replace_from, replace_to, monkeypatch):
    monkeypatch.setattr(lsm, "facts_for", lambda cards: {})
    raw = {
        **_CHROME_MOX_CARD,
        "oracle_text": _CHROME_MOX_CARD["oracle_text"].replace(replace_from, replace_to),
    }
    assert lsm.unmodelled_effect(raw)
    assert _a1_variants(_a1_board(raw))["Chrome Mox"] == ((), None)


@pytest.mark.parametrize(
    "text",
    [
        "{T}: Add {C}.",
        "{T}: Add one mana of any color.",
        "{oT}: Add one mana of any color.\n{o2}, {oT}, Discard a card: Draw a card.",
    ],
)
def test_genuine_unconditional_rock_still_admitted(text):
    raw = {"name": "Ordinary rock", "type_line": "Artifact", "mana_cost": "{2}", "oracle_text": text}
    assert lsm._plain_rock_supported(raw)
    variants, plain = _a1_variants(_a1_board(raw))["Ordinary rock"]
    assert plain is not None and plain.rock and any(v.rock for v in variants)


@pytest.mark.parametrize(
    "text",
    [
        "{T}: Add {G} only if you control a Forest.",
        "If you control a Forest:\n{T}: Add {G}.",
        "{T}: Add one mana of the exiled card’s colours.",
        "{T}, Sacrifice a creature: Add {G}.",
    ],
)
def test_conditional_or_paid_mana_not_generic_rock(text):
    raw = {"name": "Conditional rock", "type_line": "Artifact", "mana_cost": "{2}", "oracle_text": text}
    assert not lsm._plain_rock_supported(raw)
    variants, plain = _a1_variants(_a1_board(raw))["Conditional rock"]
    assert all(not v.rock for v in variants)
    assert plain is None or not plain.rock


@pytest.mark.parametrize(
    "rule",
    [
        "Activated abilities of artifacts can't be activated.",
        "Activated abilities of artifacts can’t be activated.",
        "Activated abilities of this artifact cost {1} more.",
        "If you would add mana, add {C} instead.",
    ],
)
def test_static_mana_restriction_not_hidden_by_other_activation(rule):
    raw = {
        "name": "Restricted rock",
        "type_line": "Artifact",
        "mana_cost": "{2}",
        "oracle_text": "{T}: Add {G}.\n{2}, {T}, Discard a card: Draw a card.\n" + rule,
    }
    assert not lsm._plain_rock_supported(raw)


@pytest.mark.parametrize(
    "text,expected",
    [("{T}: Add {R}.", {"R"}), ("{oT}: Add {oC}.", {"C"}), ("{T}: Add one mana of any color.", set("WUBRG"))],
)
def test_actual_plain_rock_transition_exact_color(text, expected, monkeypatch):
    raw = {"name": "Exact rock", "type_line": "Artifact", "mana_cost": "{0}", "oracle_text": text}
    apply = lsm.Moves._apply_casts
    seen = []

    def inspect(self, mid, *args, **kwargs):
        apply(self, mid, *args, **kwargs)
        for rock in mid.rocks:
            if rock.name == "Exact rock":
                seen.append(True)
                assert rock.produces == frozenset(expected)

    monkeypatch.setattr(lsm.Moves, "_apply_casts", inspect)
    _a1_variants(_a1_board(raw))
    assert seen


@pytest.mark.parametrize(
    "suffix",
    [
        "This artifact enters tapped.",
        "This artifact has no abilities.",
        "Pay {1} to activate this ability.",
        "This artifact enters with a stun counter.",
        "Unrecognized restriction.",
    ],
)
def test_unknown_extra_clause_never_admitted(suffix):
    raw = {
        "name": "Restricted rock",
        "type_line": "Artifact",
        "mana_cost": "{2}",
        "oracle_text": "{T}: Add {R}.\n" + suffix,
    }
    assert lsm._plain_rock_output(raw) == frozenset()
    assert lsm.unmodelled_effect(raw) == "its unsupported mana ability"
    assert _a1_variants(_a1_board(raw))["Restricted rock"] == ((), None)


@pytest.mark.parametrize("seat", [1, 2])
def test_known_global_artifact_suppression_abstains_public_search(seat):
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search import search_lines

    rock = {"name": "Rock", "type_line": "Artifact", "mana_cost": "{0}", "oracle_text": "{T}: Add {G}."}
    source = _a1_board(rock)
    source["battlefield"].append(
        {
            "instance_id": 9999,
            "name": "Global suppression",
            "type_line": "Enchantment",
            "controller_seat_id": seat,
            "owner_seat_id": seat,
            "oracle_text": "Activated abilities of artifacts can’t be activated.",
        }
    )
    result = search_lines(build_board_model(source), survival=True, lethal_now=False, hard_ms=1e9)
    assert result.lines == [] and not result.best.complete
    assert result.best.unsupported == ("artifact activation suppression is not projected",)


def test_conditional_or_different_suppression_is_not_claimed_exact():
    from types import SimpleNamespace

    model = SimpleNamespace(
        spells=[],
        battlefield=[
            {
                "type_line": "Artifact",
                "oracle_text": "Activated abilities of artifacts can't be activated unless you pay {1}.",
            }
        ],
    )
    assert not lsm._artifact_activation_suppression_present(model)
