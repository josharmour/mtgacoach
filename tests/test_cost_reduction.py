"""A spell's own cost reduction (pro YFRA game, 2026-10-18, match 1 turn 15).

The pro cast Seed Suture on the 4/4 Beast (5/5), then Ghalta the Unstoppable
("This spell costs {X} less to cast, where X is the greatest power among
creatures you control") for {3}{G}. The line search read Ghalta at nine mana
until it parsed the spell's own reduction (``cost_reduction.parse_cost_reduction``).
"""

from __future__ import annotations

from copy import deepcopy

from tests.boards_pro_yfra_2026_10_18 import CARDS, M1_T15

from arenamcp.board_model import build_board_model
from arenamcp.cost_reduction import parse_cost_reduction, reduced_mana_value, reduction_amount
from arenamcp.line_search import _Search, search_lines

GHALTA = CARDS["Ghalta the Unstoppable"]["oracle_text"]


def test_parse_greatest_power_and_for_each():
    ghalta = parse_cost_reduction(GHALTA)
    assert (ghalta.amount, ghalta.per) == (1, "greatest_power")
    assert reduction_amount(ghalta, [4, 1]) == 4
    each = parse_cost_reduction("This spell costs {1} less to cast for each artifact you control.")
    assert (each.amount, each.per) == (1, "artifact")
    perms = [{"type_line": "Artifact — Equipment"}, {"type_line": "Basic Land — Forest"},
             {"type_line": "Artifact Creature — Golem"}]  # fmt: skip
    assert reduction_amount(each, [], perms) == 2
    number = parse_cost_reduction("This spell costs {X} less to cast, where X is the number of creatures you control.")
    assert reduction_amount(number, [2, 3, 1]) == 3
    # Another permanent's reduction for other spells is not this spell's own.
    assert parse_cost_reduction("Noncreature spells you cast cost {1} less to cast.") is None


def test_reduction_pays_generic_mana_only():
    assert reduced_mana_value(9, 1, 5) == 4
    assert reduced_mana_value(9, 1, 20) == 1


def _three_casts(source):
    """The pro's T15 set (Phytomedic + Seed Suture + Ghalta) among the root's cast sets."""
    model = build_board_model(deepcopy(source))
    assert model is not None
    search = _Search(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None,
                     groups=8, width=8, k_next=4)  # fmt: skip
    node = next(iter(dict(search.roots()).values()))
    _base, sources = search.sources(node, None)
    sets = search.cast_sets(node, sources, instant_only=False)
    names = {"Emergency Phytomedic", "Seed Suture", "Ghalta the Unstoppable"}
    found = [c for c in sets if {search.spells[v.spell].name for v, _ in c} == names]
    return search, node, sources, found


def test_pro_m1_t15_pump_makes_ghalta_affordable_and_is_cast_first():
    search, node, sources, found = _three_casts(M1_T15)
    assert len(sources) == 6 and found, "Phytomedic + Seed Suture + Ghalta must fit six mana"
    casts = found[0]
    assert search.set_cost(node.ours, casts) == 6  # 1 + 1 + (9 - 5)
    mid = search.begin(node, None, casts)
    order = [search.spells[v.spell].name for v, _ in mid.casts]
    assert order.index("Seed Suture") < order.index("Ghalta the Unstoppable"), order
    finished = search.finish(mid, ("none", ()))
    assert any(b["name"] == "Beast" and b["power"] == 5 for b in finished.ours), finished.ours


def test_without_the_pump_ghalta_and_phytomedic_still_cost_six():
    model = build_board_model(deepcopy(M1_T15))
    result = search_lines(model, survival=False, lethal_now=False, hard_ms=1e9)
    # Ghalta at 9 - 4 = 5 plus Phytomedic's 1: the printed nine is gone.
    assert "Ghalta the Unstoppable" in result.best.steps[0].casts, result.best.steps[0]

