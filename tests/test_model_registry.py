"""WP-0.5 regression tests: content-addressed registry, retention, atomic pointer.

Covers the invariants the RL pipeline depends on:
  * registration is PASS-only and never promotes,
  * lineage is append-only (a re-run cannot clobber history),
  * every generation gets a ``models/gen-NNNN-<sha8>/metadata.json`` store,
  * only the last 10 generations keep artifacts, rows are never deleted,
  * the champion pointer flip is atomic (crash leaves the old pointer intact).
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


# ----------------------------------------------------------------- store


def test_register_creates_content_addressed_store(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    adapter = _adapter(tmp_path, "gen1")
    _register(reg, "gen-0001-v0", adapter, git_sha="deadbeef", metadata={"stage": "stage0-sft"})

    row = reg.get_generation("gen-0001-v0")
    store = Path(row["store_path"])
    assert store.parent == reg.models_dir
    assert store.name == f"gen-0001-{row['content_sha'][:8]}", store.name

    meta = json.loads((store / "metadata.json").read_text(encoding="utf-8"))
    assert meta["gen_id"] == "gen-0001-v0"
    assert meta["seq"] == 1
    assert meta["git_sha"] == "deadbeef"
    assert meta["content_sha"] == row["content_sha"]
    assert meta["content_sha_source"] == "adapter_dir"
    assert meta["gate_report"]["verdict"] == "PASS"
    assert meta["metadata"]["stage"] == "stage0-sft"


def test_content_sha_tracks_adapter_bytes(tmp_path):
    """Same adapter bytes -> same sha; different bytes -> different sha."""
    reg = ModelRegistry(tmp_path / "models")
    a = _adapter(tmp_path, "a", "identical")
    b = _adapter(tmp_path, "b", "identical")
    c = _adapter(tmp_path, "c", "different")
    _register(reg, "gen-a", a)
    _register(reg, "gen-b", b)
    _register(reg, "gen-c", c)

    sha = {g["gen_id"]: g["content_sha"] for g in reg.list_generations()}
    assert sha["gen-a"] == sha["gen-b"]
    assert sha["gen-a"] != sha["gen-c"]


def test_missing_adapter_falls_back_to_descriptor_hash(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-remote", tmp_path / "not" / "on" / "this" / "host")
    meta = json.loads((Path(reg.get_generation("gen-remote")["store_path"]) / "metadata.json").read_text())
    assert meta["content_sha_source"] == "descriptor"
    assert len(meta["content_sha"]) == 64


def test_copy_artifacts_archives_adapter(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    adapter = _adapter(tmp_path, "gen1", "abc")
    _register(reg, "gen-copy", adapter, copy_artifacts=True)
    store = Path(reg.get_generation("gen-copy")["store_path"])
    assert (store / "adapter" / "adapter_model.safetensors").read_text() == "abc"


# --------------------------------------------------------- fail-closed


@pytest.mark.parametrize("report", [None, {}, {"verdict": "BLOCKED"}, {"verdict": "FAIL"}])
def test_registration_is_pass_only(tmp_path, report):
    reg = ModelRegistry(tmp_path / "models")
    adapter = _adapter(tmp_path, "gen1")
    with pytest.raises(ValueError, match="PASS-only"):
        reg.register_generation(
            gen_id="gen-blocked",
            base_model="m",
            adapter_path=adapter,
            gate_report=report,
        )
    assert reg.get_generation("gen-blocked") is None
    assert list(reg.models_dir.iterdir()) == []


def test_registration_does_not_promote(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-1", _adapter(tmp_path, "g1"))
    assert reg.get_generation("gen-1")["is_champion"] == 0
    assert reg.get_champion() is None
    assert reg.read_champion_pointer() is None


def test_reregistration_does_not_clobber_history(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    a = _adapter(tmp_path, "g1", "first")
    _register(reg, "gen-1", a, git_sha="sha-one", metadata={"note": "original"})
    (a / "adapter_model.safetensors").write_text("second", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        _register(reg, "gen-1", a, git_sha="sha-two", metadata={"note": "rerun"})

    row = reg.get_generation("gen-1")
    assert row["git_sha"] == "sha-one"
    assert row["metadata"]["note"] == "original"
    assert len(reg.list_generations()) == 1


# ------------------------------------------------------------ promotion


def test_promote_champion_flips_exclusively_and_writes_pointer(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-1", _adapter(tmp_path, "g1", "one"))
    _register(reg, "gen-2", _adapter(tmp_path, "g2", "two"))

    assert reg.promote_champion("gen-1") is True
    assert reg.get_champion()["gen_id"] == "gen-1"
    assert reg.read_champion_pointer()["champion"] == "gen-1"

    assert reg.promote_champion("gen-2") is True
    assert reg.get_champion()["gen_id"] == "gen-2"
    assert [g["gen_id"] for g in reg.list_generations() if g["is_champion"]] == ["gen-2"]
    pointer = reg.read_champion_pointer()
    assert pointer["champion"] == "gen-2"
    assert pointer["previous"] == "gen-1"
    # No stray temp files left behind by the atomic write.
    assert [p.name for p in reg.registry_dir.glob("champion_pointer.json.tmp*")] == []


def test_promote_unknown_generation_is_refused(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-1", _adapter(tmp_path, "g1"))
    reg.promote_champion("gen-1")
    assert reg.promote_champion("gen-does-not-exist") is False
    assert reg.get_champion()["gen_id"] == "gen-1"
    assert reg.read_champion_pointer()["champion"] == "gen-1"


def test_pointer_flip_is_atomic_on_failure(tmp_path, monkeypatch):
    """A failed swap must leave BOTH the pointer file and the DB on the old champion.

    Regression for the plain ``open(pointer, "w")`` write, which truncated the
    live pointer before the new champion was durable.
    """
    reg = ModelRegistry(tmp_path / "models")
    _register(reg, "gen-1", _adapter(tmp_path, "g1", "one"))
    _register(reg, "gen-2", _adapter(tmp_path, "g2", "two"))
    reg.promote_champion("gen-1")

    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith("champion_pointer.json"):
            raise OSError("simulated crash mid-flip")
        return real_replace(src, dst)

    monkeypatch.setattr(registry_mod.os, "replace", boom)
    assert reg.promote_champion("gen-2") is False
    monkeypatch.undo()

    assert reg.read_champion_pointer()["champion"] == "gen-1"
    assert reg.get_champion()["gen_id"] == "gen-1"
    assert [p.name for p in reg.registry_dir.glob("champion_pointer.json.tmp*")] == []


# ------------------------------------------------------------ retention


def test_retention_keeps_last_10_and_never_deletes_rows(tmp_path):
    reg = ModelRegistry(tmp_path / "models")
    for i in range(1, 14):
        _register(reg, f"gen-{i:04d}", _adapter(tmp_path, f"g{i}", f"weights-{i}"))

    rows = reg.list_generations()
    assert len(rows) == 13, "lineage rows must never be deleted"

    live = [r for r in rows if Path(r["store_path"]).is_dir()]
    assert len(live) == RETENTION_LIMIT
    assert {r["gen_id"] for r in live} == {f"gen-{i:04d}" for i in range(4, 14)}

    for r in rows:
        if r["gen_id"] in {"gen-0001", "gen-0002", "gen-0003"}:
            assert r["pruned_at"] is not None
            assert not Path(r["store_path"]).is_dir()
        else:
            assert r["pruned_at"] is None


def test_retention_never_prunes_the_champion(tmp_path):
    reg = ModelRegistry(tmp_path / "models", retention_limit=2)
    _register(reg, "gen-1", _adapter(tmp_path, "g1", "one"))
    reg.promote_champion("gen-1")
    for i in range(2, 6):
        _register(reg, f"gen-{i}", _adapter(tmp_path, f"g{i}", f"w{i}"))

    champ = reg.get_generation("gen-1")
    assert champ["is_champion"] == 1
    assert champ["pruned_at"] is None
    assert Path(champ["store_path"]).is_dir()
    assert (Path(champ["store_path"]) / "metadata.json").is_file()


def test_sequence_numbers_are_monotonic(tmp_path):
    reg = ModelRegistry(tmp_path / "models", retention_limit=2)
    for i in range(1, 6):
        _register(reg, f"gen-{i}", _adapter(tmp_path, f"g{i}", f"w{i}"))
    seqs = [r["seq"] for r in sorted(reg.list_generations(), key=lambda r: r["created_at"])]
    assert seqs == [1, 2, 3, 4, 5]


# ------------------------------------------------------------ migration


def test_migrates_legacy_registry_without_losing_rows(tmp_path):
    """A pre-WP-0.5 registry (no store columns) must open and keep its rows."""
    reg_dir = tmp_path / "models"
    reg_dir.mkdir(parents=True)
    db = reg_dir / "registry.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE models (
                gen_id TEXT PRIMARY KEY, base_model TEXT NOT NULL, created_at REAL NOT NULL,
                parent_gen TEXT, git_sha TEXT, adapter_path TEXT NOT NULL, metadata_json TEXT,
                gate_report_json TEXT, is_champion INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO models VALUES (?,?,?,?,?,?,?,?,0)",
            (
                "gen-0001-v0",
                "google/gemma-4-12B-it",
                1784889107.0,
                None,
                "49c1c4a",
                "tools/training/checkpoints/stage0_gemma12b_lora",
                '{"stage": "stage0-sft"}',
                '{"verdict": "PASS"}',
            ),
        )

    reg = ModelRegistry(reg_dir)
    legacy = reg.get_generation("gen-0001-v0")
    assert legacy["git_sha"] == "49c1c4a"
    assert legacy["store_path"] is None
    assert legacy["is_champion"] == 0

    _register(reg, "gen-0002-v0", _adapter(tmp_path, "g2"))
    assert reg.get_generation("gen-0002-v0")["seq"] == 2
    assert len(reg.list_generations()) == 2
