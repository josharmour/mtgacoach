"""gain_drain fast path: no engine list when lifelink gained nothing (2026-11-01).

``persistent_returns.gain_drain`` returns 0 immediately for ``gained <= 0``; ``Moves._policy``
therefore skips building its battlefield/theirs/persistent engine list on that proven zero path.
The positive-lifelink control pins the consulted amounts and the resulting life totals; the
return-loop boundary itself is covered by ``test_session_exact_closure.py``.
"""

from copy import deepcopy

from tests import strategic_states as ss
from tests.race_boards_20261019 import BURN_HAND

from arenamcp import combat_solver as cs
from arenamcp import line_search as ls
from arenamcp import persistent_returns as pr
from arenamcp.board_model import build_board_model


def _search(source):
    cs._RESOLVE_CACHE.clear()
    return ls.search_lines(build_board_model(deepcopy(source)), survival=True, lethal_now=False, hard_ms=1e9)


def _record(monkeypatch):
    calls = []
    original = pr.gain_drain

    def record(cards, gained, life):
        calls.append((gained, life))
        return original(cards, gained, life)

    monkeypatch.setattr(pr, "gain_drain", record)
    return calls


def test_zero_gain_path_skips_engine_list(monkeypatch):
    calls = _record(monkeypatch)
    result = _search(ss.SLOW_202111)
    assert calls == []  # no lifelink gain on this board: gain_drain is never consulted
    assert (result.nodes, result.combats) == (185, 310)


def test_positive_lifelink_still_consulted(monkeypatch):
    calls = _record(monkeypatch)
    result = _search(BURN_HAND)
    # Dawnwing Marshal's lifelink deals 5: every consultation carries that gain.
    assert calls and all(gained == 5 for gained, _life in calls)
    # The lifelink line still resolves to the expected life totals and opponent loss.
    assert result.best.summary().endswith("survives (life 9, 9, 9; opponent at 7)")
