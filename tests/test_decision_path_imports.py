"""The decision-path modules never import an LLM backend or a network client.

The board model, the line search and its guard, and the opponent trick
estimate run inside every decision; they must stay pure board math.
``arenamcp/__init__`` imports the coach (and so ``arenamcp.backends``), so the
check loads the package without it, in a fresh interpreter, and also runs a
real trick estimate (its service may only read the cache, never fetch).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.test_opponent_tricks import COLOR_RATINGS, LOOKUP, RATINGS, make_primer

from arenamcp import opponent_tricks as ot

REPO = Path(__file__).resolve().parents[1]

_CHECK = """
import sys, types
pkg = types.ModuleType("arenamcp"); pkg.__path__ = [{src!r}]
sys.modules["arenamcp"] = pkg
sys.path.insert(0, {repo!r})

def bad():
    return sorted(
        m for m in sys.modules
        if m.startswith(("arenamcp.backends", "arenamcp.coach")) or m in ("requests", "httpx", "openai")
    )

for module in ("board_model", "line_search", "line_guard", "opponent_tricks"):
    __import__("arenamcp." + module)
    if bad():
        sys.exit(f"importing arenamcp.{{module}} loaded {{bad()}}")

import arenamcp.opponent_tricks as ot
from tests import strategic_states as S

table = ot.TrickTable.from_json(open({table!r}, encoding="utf-8").read())
assert ot.trick_risk(S.G3_T10_BLOCKS, table).known
assert ot.TrickTableService(cache_dir=None).get("FRA") is None
if bad():
    sys.exit(f"estimating a trick risk loaded {{bad()}}")
"""


def test_decision_path_imports_no_llm_backend_or_network_client(tmp_path):
    table = ot.build_trick_table("FRA", make_primer(), RATINGS, LOOKUP.get, color_ratings=COLOR_RATINGS)
    path = tmp_path / "table.json"
    path.write_text(table.to_json(), encoding="utf-8")
    code = _CHECK.format(src=str(REPO / "src" / "arenamcp"), repo=str(REPO), table=str(path))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO, env=env, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
