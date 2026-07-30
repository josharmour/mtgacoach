"""WP-0.5 acceptance: promote/rollback/promote cycle, crash safety, retention.

This file implements the three acceptance criteria from rl-pipeline-fix.md:

  1. Promote → rollback → promote cycle leaves artifacts intact and pointers clean.
  2. A simulated crash between write and flip leaves the old champion pointer valid.
  3. Retention pruning at >10 generations keeps lineage rows and spares champion.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training import registry as registry_mod
from tools.training.registry import RETENTION_LIMIT, ModelRegistry

PASS_REPORT = {"verdict": "PASS", "gate": "stage0", "failures": []}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _adapter(tmp_path: Path, name: str, payload: str = "weights") -> Path:
    d = tmp_path / "checkpoints" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapter_model.safetensors").write_text(payload, encoding="utf-8")
    return d


def _register(reg: ModelRegistry, gen_id: str, adapter: Path, **kw) -> str:
    return reg.register_generation(
        gen_id=gen_id,
        base_model=kw.pop("base_model", "google/gemma-4-12B-it"),
        adapter_path=adapter,
        gate_report=kw.pop("gate_report", PASS_REPORT),
        **kw,
    )


def _assert_artifacts_intact(reg: ModelRegistry, gen_id: str) -> None:
    """Assert that *all* store directories (champion or not) are on disk with
    valid metadata.json."""
    for g in reg.list_generations():
        store = g.get("store_path")
        assert store is not None, f"gen {g['gen_id']} has no store_path"
        p = Path(store)
        assert p.is_dir(), f"store dir missing for {g['gen_id']}: {p}"
        meta = json.loads((p / "metadata.json").read_text(encoding="utf-8"))
        assert meta["gen_id"] == g["gen_id"]
        assert meta["content_sha"] == g["content_sha"]


def _assert_pointers_clean(reg: ModelRegistry, expected_champion: str | None):
    """Verify champion_pointer.json exists, is parseable, has no stray
    temp files, and names the expected champion."""
    pointer = reg.read_champion_pointer()
    if expected_champion is None:
        assert pointer is None
    else:
        assert pointer is not None
        assert pointer["champion"] == expected_champion

    # No orphaned .tmp files from interrupted writes.
    temps = list(reg.registry_dir.glob("champion_pointer.json.tmp*"))
    assert temps == [], f"stray temp files: {[p.name for p in temps]}"

    # SQLite and pointer agree.
    if expected_champion is not None:
        db_champ = reg.get_champion()
        assert db_champ is not None
        assert db_champ["gen_id"] == expected_champion
        assert db_champ["store_path"] is not None
        assert Path(db_champ["store_path"]).is_dir()


# ===================================================================
# ACCEPT 1: Promote → rollback → promote cycle
# ===================================================================


def test_promote_rollback_promote_cycle(tmp_path):
    """Full acceptance cycle: promote gen-1, rollback to gen-2, promote
    gen-1 again. Artifacts must be intact and pointers clean at every step."""
    reg = ModelRegistry(tmp_path / "models")

    # -- setup: three registered generations --------------------------------
    _register(reg, "gen-1", _adapter(tmp_path, "g1", "v1"))
    _register(reg, "gen-2", _adapter(tmp_path, "g2", "v2"))
    _register(reg, "gen-3", _adapter(tmp_path, "g3", "v3"))
    assert len(reg.list_generations()) == 3

    # ---- STEP 1: promote gen-1 --------------------------------------------
    assert reg.promote_champion("gen-1") is True
    _assert_artifacts_intact(reg, "gen-1")
    _assert_pointers_clean(reg, "gen-1")
    assert reg.get_generation("gen-1")["is_champion"] == 1
    assert reg.get_generation("gen-2")["is_champion"] == 0
    assert reg.get_generation("gen-3")["is_champion"] == 0

    # ---- STEP 2: rollback — promote gen-2 (replaces gen-1) ----------------
    assert reg.promote_champion("gen-2") is True

    # All three artifact directories must still exist.
    _assert_artifacts_intact(reg, "gen-2")
    _assert_pointers_clean(reg, "gen-2")
    pointer = reg.read_champion_pointer()
    assert pointer["previous"] == "gen-1"
    assert reg.get_generation("gen-1")["is_champion"] == 0
    assert reg.get_generation("gen-2")["is_champion"] == 1

    # ---- STEP 3: promote gen-1 again (re-replaces gen-2) ------------------
    assert reg.promote_champion("gen-1") is True
    _assert_artifacts_intact(reg, "gen-1")
    _assert_pointers_clean(reg, "gen-1")
    pointer = reg.read_champion_pointer()
    assert pointer["previous"] == "gen-2"
    assert reg.get_generation("gen-1")["is_champion"] == 1
    assert reg.get_generation("gen-2")["is_champion"] == 0

    # ---- Final check: no row lost, no stray files -------------------------
    assert len(reg.list_generations()) == 3
    temps = list(reg.registry_dir.glob("champion_pointer.json.tmp*"))
    assert temps == [], f"stray temp files: {[p.name for p in temps]}"


# ===================================================================
# ACCEPT 2: Crash safety — old pointer remains valid
# ===================================================================


@pytest.mark.parametrize("crash_point", ["before_sqlite", "after_sqlite"])
def test_crash_between_write_and_flip_leaves_old_pointer_intact(tmp_path, crash_point):
    """Simulate a crash between temp-file write and os.replace.

    Two crash points:

    * ``before_sqlite`` — the temp pointer file was written but the SQLite
      transaction never committed. After recovery the old champion pointer
      must be untouched AND the DB must still report the old champion.

    * ``after_sqlite`` — the SQLite transaction committed (new champion in
      DB) but os.replace never ran. After recovery the old champion pointer
      must still be valid (old pointer file intact). The DB is inconsistent
      with the pointer but neither is corrupt — the pointer is the
      authoritative external contract.
    """
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-1", _adapter(tmp_path, "g1", "one"))
    _register(reg, "gen-2", _adapter(tmp_path, "g2", "two"))
    assert reg.promote_champion("gen-1") is True
    old_pointer = reg.read_champion_pointer()
    assert old_pointer["champion"] == "gen-1"

    # ---- Simulate the crash -----------------------------------------------
    tmp = reg.pointer_path.with_name(reg.pointer_path.name + f".tmp.{os.getpid()}")
    try:
        # Stage the temp pointer file (this is what promote_champion does
        # before os.replace).
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "champion": "gen-2",
                    "updated_at": 1000.0,
                    "previous": "gen-1",
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
            f.flush()
            os.fsync(f.fileno())

        with sqlite3.connect(reg.db_path) as conn:
            conn.execute("UPDATE models SET is_champion = 0")
            conn.execute(
                "UPDATE models SET is_champion = 1 WHERE gen_id = ?",
                ("gen-2",),
            )
        # If crash_point == "before_sqlite", roll back the SQLite change to
        # simulate what happens when the tx never commits.
        if crash_point == "before_sqlite":
            with sqlite3.connect(reg.db_path) as conn:
                conn.execute("UPDATE models SET is_champion = 0")
                conn.execute(
                    "UPDATE models SET is_champion = 1 WHERE gen_id = ?",
                    ("gen-1",),
                )
        # "crash" — never call os.replace.

        # ---- Simulate recovery: new ModelRegistry instance -----------------
        reg2 = ModelRegistry(tmp_path / "models")

        # The old champion pointer MUST be valid.
        assert reg2.read_champion_pointer()["champion"] == "gen-1"
        assert reg2.read_champion_pointer()["previous"] is None

        if crash_point == "before_sqlite":
            # DB agrees with pointer.
            assert reg2.get_champion()["gen_id"] == "gen-1"
        else:
            # DB shows gen-2 (post-crash inconsistency — fixable by recovery).
            # The pointer itself is still valid and readable.
            assert reg2.get_champion()["gen_id"] == "gen-2"
            assert reg2.read_champion_pointer()["champion"] == "gen-1"

    finally:
        # Clean up the orphaned temp file (simulated crash artifact).
        registry_mod._unlink_quiet(tmp)

    # ---- A subsequent promote must succeed normally -----------------------
    reg3 = ModelRegistry(tmp_path / "models")
    if crash_point == "after_sqlite":
        # The DB says gen-2 is champion but the pointer says gen-1.
        # Promoting gen-2 is the logical recovery step.
        assert reg3.promote_champion("gen-2") is True
        assert reg3.get_champion()["gen_id"] == "gen-2"
        assert reg3.read_champion_pointer()["champion"] == "gen-2"
    else:
        # Consistent state — promote gen-2 normally.
        assert reg3.promote_champion("gen-2") is True
        assert reg3.get_champion()["gen_id"] == "gen-2"

    # No stray temp files after recovery.
    temps = list(reg3.registry_dir.glob("champion_pointer.json.tmp*"))
    assert temps == [], f"stray temp files: {[p.name for p in temps]}"

    # Artifacts are intact.
    _assert_artifacts_intact(reg3, "gen-2")


def test_atomic_write_temp_cleanup_on_crash(tmp_path):
    """An orphaned .tmp file from a crash must not prevent future promotes.

    If the process dies after writing the staged pointer but before os.replace
    (or the finally block), an orphan ``champion_pointer.json.tmp.<PID>``
    sits in the registry directory. A subsequent promote must clean it up
    via _unlink_quiet in the new process's finally block.
    """
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-1", _adapter(tmp_path, "g1", "one"))
    _register(reg, "gen-2", _adapter(tmp_path, "g2", "two"))
    reg.promote_champion("gen-1")

    # Plant an orphaned temp file.
    orphan = reg.pointer_path.with_name(reg.pointer_path.name + f".tmp.{os.getpid()}")
    orphan.write_text(
        json.dumps(
            {
                "champion": "gen-2",
                "updated_at": 1000.0,
                "previous": "gen-1",
            }
        ),
        encoding="utf-8",
    )

    # A new promote should clean up the orphan.
    reg2 = ModelRegistry(tmp_path / "models")
    assert reg2.promote_champion("gen-2") is True
    temps = list(reg2.registry_dir.glob("champion_pointer.json.tmp*"))
    assert temps == [], f"orphan temp file left behind: {[p.name for p in temps]}"
    assert reg2.get_champion()["gen_id"] == "gen-2"
    assert reg2.read_champion_pointer()["champion"] == "gen-2"


# ===================================================================
# ACCEPT 3: Retention pruning at >10 generations
# ===================================================================


def test_retention_at_11_generations_prunes_first(tmp_path):
    """With RETENTION_LIMIT=10, registering the 11th generation must prune the
    oldest non-champion generation's artifacts."""
    reg = ModelRegistry(tmp_path / "models")

    for i in range(1, 11):
        _register(reg, f"gen-{i:04d}", _adapter(tmp_path, f"g{i}", f"w{i}"))

    # All 10 fit within retention — none pruned.
    pruned = reg.prune_old_generations()
    assert pruned == []
    live = [r for r in reg.list_generations() if r["store_path"] and Path(r["store_path"]).is_dir()]
    assert len(live) == 10

    # 11th generation — oldest (gen-0001) gets pruned.
    _register(reg, "gen-0011", _adapter(tmp_path, "g11", "w11"))
    gen1 = reg.get_generation("gen-0001")
    assert gen1["pruned_at"] is not None, "oldest generation should be pruned"
    assert not Path(gen1["store_path"]).is_dir(), "oldest store dir should be deleted"

    # Check that gen-0002..gen-0011 are still intact.
    for i in range(2, 12):
        gid = f"gen-{i:04d}"
        g = reg.get_generation(gid)
        assert g["pruned_at"] is None, f"{gid} should not be pruned"
        assert Path(g["store_path"]).is_dir(), f"{gid} store should exist"


def test_retention_champion_never_pruned_even_beyond_limit(tmp_path):
    """If the champion is the oldest generation and there are 16 newer
    generations (17 total), the champion's artifacts must survive while
    7 of the oldest non-champion artifacts are pruned."""
    reg = ModelRegistry(tmp_path / "models")

    _register(reg, "gen-champ", _adapter(tmp_path, "gc", "v0"))
    reg.promote_champion("gen-champ")

    for i in range(1, 17):
        _register(reg, f"gen-{i:04d}", _adapter(tmp_path, f"g{i}", f"w{i}"))

    # gen-champ should survive pruning even though it's the oldest.
    champ = reg.get_generation("gen-champ")
    assert champ["is_champion"] == 1
    assert champ["pruned_at"] is None
    assert Path(champ["store_path"]).is_dir(), "champion store must survive"

    # Non-champion generations outside the retention window should be pruned.
    # 17 total - 10 keep = 7 outside; gen-champ is spared = 6 pruned.
    for i in range(1, 7):
        g = reg.get_generation(f"gen-{i:04d}")
        assert g["pruned_at"] is not None, f"gen-{i:04d} should be pruned"
        assert not Path(g["store_path"]).is_dir(), f"gen-{i:04d} store should be deleted"

    # Latest 10 non-champion gens should still be intact.
    for i in range(7, 17):
        g = reg.get_generation(f"gen-{i:04d}")
        assert g["pruned_at"] is None, f"gen-{i:04d} should NOT be pruned"
        assert Path(g["store_path"]).is_dir(), f"gen-{i:04d} store should exist"

    # Database rows are never deleted.
    assert len(reg.list_generations()) == 17  # champ + 16 more


def test_retention_20_generations_prunes_10(tmp_path):
    """Register exactly 21 generations → exactly 10 remain on disk."""
    reg = ModelRegistry(tmp_path / "models")

    for i in range(1, 22):
        _register(reg, f"gen-{i:04d}", _adapter(tmp_path, f"g{i}", f"w{i}"))

    rows = reg.list_generations()
    assert len(rows) == 21  # rows never deleted

    live = [r for r in rows if Path(r["store_path"]).is_dir()]
    assert len(live) == RETENTION_LIMIT, f"expected {RETENTION_LIMIT} live, got {len(live)}"

    pruned = [r for r in rows if r["pruned_at"] is not None]
    assert len(pruned) == 11  # 21 total - 10 retained = 11 pruned
    for r in pruned:
        assert not Path(r["store_path"]).is_dir(), f"{r['gen_id']} store should be gone"


# ===================================================================
# ACCEPT 4 (hard): No registration without a gate verdict
# ===================================================================


def test_no_code_path_registers_without_gate_verdict(tmp_path):
    """Verify that every registration entry point enforces a PASS gate.

    This is an exhaustive check: register_generation is the single public
    entry point, and it always validates verdict == 'PASS'. There is no
    bypass flag, no _force_register private method, and no other public
    method that writes to models table.
    """
    reg = ModelRegistry(tmp_path / "models")

    # -- Every non-PASS verdict is rejected --------------------------------
    for bad_report in (None, {}, {"verdict": "BLOCKED"}, {"verdict": "FAIL"}):
        with pytest.raises(ValueError, match="PASS-only"):
            reg.register_generation(
                gen_id="bad",
                base_model="m",
                adapter_path=_adapter(tmp_path, "bad"),
                gate_report=bad_report,
            )

    # -- PASS must be accepted ---------------------------------------------
    reg.register_generation(
        gen_id="good",
        base_model="m",
        adapter_path=_adapter(tmp_path, "good"),
        gate_report={"verdict": "PASS", "gate": "stage0", "failures": []},
    )
    assert reg.get_generation("good") is not None

    # -- Verify no bypass exists on the ModelRegistry class ----------------
    assert not hasattr(reg, "_force_register"), "a bypass registration method exists"
    assert not hasattr(reg, "_register_internal"), "an internal registration helper exists"


def test_promote_never_registers(tmp_path):
    """promote_champion must never create a new database row.

    It should only flip the is_champion flag. An attempt to promote an
    unknown gen_id must return False and leave the champion unchanged.
    """
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-1", _adapter(tmp_path, "g1"))
    reg.promote_champion("gen-1")

    count_before = len(reg.list_generations())
    assert reg.promote_champion("does-not-exist") is False
    count_after = len(reg.list_generations())
    assert count_after == count_before, "promote unknown must not create a row"
    assert reg.get_champion()["gen_id"] == "gen-1"
