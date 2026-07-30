"""Content-addressed Model Registry for MTGA Coach models (WP-0.5).

Manages model generation lineage, gate verification reports, and atomic champion
pointer flips.

Layout under ``<registry_dir>``::

    registry.sqlite                     lineage + gate reports (append-only)
    champion_pointer.json               atomically replaced pointer to champion
    models/gen-NNNN-<sha8>/             content-addressed store, one per gen
        metadata.json                   full descriptor (also atomically written)
        adapter/                        optional archived copy of the adapter

Invariants (all enforced here, not merely by convention):

* **Registration is PASS-only.** A generation cannot enter the registry unless
  its gate report carries ``verdict == "PASS"``. There is deliberately no
  bypass/force flag — fail closed (plan constraint 2).
* **History is append-only.** Re-registering an existing ``gen_id`` raises;
  rows are never deleted or overwritten, so a re-run cannot clobber lineage.
* **Registration never promotes.** ``is_champion`` stays 0 until
  :meth:`promote_champion` is called explicitly (plan constraint 5).
* **The champion pointer flip is atomic.** The pointer is written to a temp
  file in the same directory, fsynced, and moved into place with
  ``os.replace``. A crash mid-flip leaves the previous pointer intact.
* **Retention applies to artifacts only.** Only the newest
  :data:`RETENTION_LIMIT` generations (plus the champion) keep their store
  directory on disk; older store directories are pruned and the row is stamped
  with ``pruned_at``. The database rows themselves are never deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("tools.training.registry")

# Number of most-recent generations whose artifacts stay on disk. Older
# generations keep their database row (lineage is append-only) but lose their
# content-store directory.
RETENTION_LIMIT = 10

# Bumped when the metadata.json shape changes.
METADATA_SCHEMA_VERSION = 1

_STORE_DIR_RE = re.compile(r"^gen-(\d{4,})-[0-9a-f]{8}$")

_HASH_CHUNK = 1024 * 1024


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (temp file + os.replace).

    The temp file lives in the destination directory so ``os.replace`` is a
    same-filesystem rename, which POSIX guarantees to be atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise


def _unlink_quiet(path: Path) -> None:
    """Best-effort unlink; never raises."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:  # pragma: no cover - platform dependent
        logger.debug(f"could not remove temp file {path}: {e}")


def hash_directory(path: Path) -> str:
    """Content hash of a directory tree: sha256 over (relpath, bytes) pairs.

    Deterministic across machines: paths are POSIX-normalised and sorted, and
    file bytes are streamed in 1 MiB chunks so large adapters do not need to
    fit in memory. Symlinks are followed as regular files.
    """
    h = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file())
    for f in files:
        rel = f.relative_to(path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        with open(f, "rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


class ModelRegistry:
    """Manages model checkpoints, lineage, and atomic promotion pointers."""

    def __init__(self, registry_dir: Path, retention_limit: int = RETENTION_LIMIT):
        self.registry_dir = Path(registry_dir).resolve()
        self.models_dir = self.registry_dir / "models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.registry_dir / "registry.sqlite"
        self.pointer_path = self.registry_dir / "champion_pointer.json"
        self.retention_limit = max(1, int(retention_limit))
        self._init_db()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                    gen_id TEXT PRIMARY KEY,
                    base_model TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    parent_gen TEXT,
                    git_sha TEXT,
                    adapter_path TEXT NOT NULL,
                    metadata_json TEXT,
                    gate_report_json TEXT,
                    is_champion INTEGER DEFAULT 0
                )
                """
            )
            # Additive migration for pre-WP-0.5 registries: existing rows keep
            # their values and gain NULL content-store columns.
            existing = {r[1] for r in conn.execute("PRAGMA table_info(models)")}
            for col, decl in (
                ("store_path", "TEXT"),
                ("content_sha", "TEXT"),
                ("seq", "INTEGER"),
                ("pruned_at", "REAL"),
            ):
                if col not in existing:
                    conn.execute(f"ALTER TABLE models ADD COLUMN {col} {decl}")
                    logger.info(f"registry migration: added column models.{col}")

    # ------------------------------------------------------------------
    # content addressing
    # ------------------------------------------------------------------

    def _content_sha(self, descriptor: dict[str, Any], adapter_path: Path) -> tuple[str, str]:
        """Return ``(sha256_hex, source)`` for this generation's content.

        When the adapter directory (or file) is reachable, the hash is taken
        over its bytes — that is the real content address. When it is not
        (adapters commonly live on the training host while the registry is
        synced elsewhere), fall back to hashing the canonical descriptor so a
        generation still gets a stable, collision-resistant id. The source is
        recorded in metadata.json so the distinction is never silent.
        """
        try:
            if adapter_path.is_dir():
                return hash_directory(adapter_path), "adapter_dir"
            if adapter_path.is_file():
                h = hashlib.sha256()
                with open(adapter_path, "rb") as fh:
                    while True:
                        chunk = fh.read(_HASH_CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                return h.hexdigest(), "adapter_file"
        except OSError as e:
            logger.warning(f"could not hash adapter at {adapter_path}: {e}; hashing descriptor instead")
        blob = json.dumps(descriptor, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest(), "descriptor"

    def _next_seq(self, conn: sqlite3.Connection) -> int:
        """Next generation sequence number.

        Derived from the max recorded ``seq``, the max ``gen-NNNN`` directory
        already in the store, and the row count — whichever is largest — so a
        registry migrated from the pre-WP-0.5 schema never reuses a number.
        """
        row = conn.execute("SELECT COALESCE(MAX(seq), 0), COUNT(*) FROM models").fetchone()
        max_seq = int(row[0] or 0)
        count = int(row[1] or 0)
        max_dir = 0
        if self.models_dir.is_dir():
            for d in self.models_dir.iterdir():
                m = _STORE_DIR_RE.match(d.name)
                if m:
                    max_dir = max(max_dir, int(m.group(1)))
        return max(max_seq, max_dir, count) + 1

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def register_generation(
        self,
        gen_id: str,
        base_model: str,
        adapter_path: Path,
        parent_gen: str | None = None,
        git_sha: str | None = None,
        metadata: dict[str, Any] | None = None,
        gate_report: dict[str, Any] | None = None,
        copy_artifacts: bool = False,
    ) -> str:
        """Register a gate-PASSED model generation.

        Creates ``models/gen-NNNN-<sha8>/metadata.json`` (content-addressed
        store) and one append-only row in ``registry.sqlite``. The new row is
        never a champion — promotion is a separate, explicit step.

        Raises:
            ValueError: if the gate report is missing/not PASS (fail closed),
                or if ``gen_id`` is already registered (non-clobber policy).
        """
        verdict = (gate_report or {}).get("verdict")
        if verdict != "PASS":
            raise ValueError(
                f"Refusing to register '{gen_id}': gate verdict is {verdict!r}, not 'PASS'. "
                "Registration is PASS-only and has no bypass flag (fail closed)."
            )

        adapter_path = Path(adapter_path)
        created_at = time.time()
        meta = dict(metadata or {})

        descriptor = {
            "gen_id": gen_id,
            "base_model": base_model,
            "adapter_path": str(adapter_path),
            "parent_gen": parent_gen,
            "git_sha": git_sha,
            "metadata": meta,
        }
        content_sha, sha_source = self._content_sha(descriptor, adapter_path)
        sha8 = content_sha[:8]

        meta_json = json.dumps(meta, ensure_ascii=False)
        report_json = json.dumps(gate_report or {}, ensure_ascii=False)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT gen_id FROM models WHERE gen_id = ?", (gen_id,)).fetchone()
            if row:
                raise ValueError(
                    f"Cannot register generation '{gen_id}': already exists in registry. "
                    "Non-clobber policy requires unique generation IDs or additive gate reports."
                )
            seq = self._next_seq(conn)
            store_dir = self.models_dir / f"gen-{seq:04d}-{sha8}"
            store_dir.mkdir(parents=True, exist_ok=True)

            if copy_artifacts and adapter_path.is_dir():
                try:
                    shutil.copytree(adapter_path, store_dir / "adapter", dirs_exist_ok=True)
                except OSError as e:  # pragma: no cover - disk/permission dependent
                    logger.warning(f"could not archive adapter into {store_dir}: {e}")

            _atomic_write_json(
                store_dir / "metadata.json",
                {
                    "schema_version": METADATA_SCHEMA_VERSION,
                    "gen_id": gen_id,
                    "seq": seq,
                    "base_model": base_model,
                    "created_at": created_at,
                    "parent_gen": parent_gen,
                    "git_sha": git_sha,
                    "adapter_path": str(adapter_path),
                    "content_sha": content_sha,
                    "content_sha_source": sha_source,
                    "artifacts_archived": bool(copy_artifacts and (store_dir / "adapter").is_dir()),
                    "metadata": meta,
                    "gate_report": gate_report or {},
                },
            )

            conn.execute(
                """
                INSERT INTO models
                (gen_id, base_model, created_at, parent_gen, git_sha, adapter_path,
                 metadata_json, gate_report_json, is_champion, store_path, content_sha, seq, pruned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)
                """,
                (
                    gen_id,
                    base_model,
                    created_at,
                    parent_gen,
                    git_sha,
                    str(adapter_path),
                    meta_json,
                    report_json,
                    str(store_dir),
                    content_sha,
                    seq,
                ),
            )

        logger.info(f"Registered model generation '{gen_id}' -> {store_dir.name} (sha {sha8}, {sha_source}).")
        self.prune_old_generations()
        return gen_id

    # ------------------------------------------------------------------
    # promotion
    # ------------------------------------------------------------------

    def _exec_tx(self, conn: sqlite3.Connection, stmts: list[tuple[str, tuple]]) -> None:
        """Execute multiple statements in a single SQLite transaction.

        With ``isolation_level=''`` (Python 3.11 default) there is no
        implicit transaction, so we drive ``BEGIN`` / ``COMMIT`` explicitly.
        """
        conn.execute("BEGIN")
        try:
            for sql, params in stmts:
                conn.execute(sql, params)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def promote_champion(self, gen_id: str) -> bool:
        """Atomically set ``gen_id`` as the champion model.

        The database flip commits as a single SQLite transaction first;
        the on-disk pointer is swapped afterwards.  If the pointer swap
        fails the DB *is* already committed — a retry will fix the pointer.
        The old pointer remains valid through the entire operation so a
        crash before the swap leaves it untouched.
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT gen_id FROM models WHERE gen_id = ?", (gen_id,)).fetchone()
            if not row:
                logger.error(f"Cannot promote '{gen_id}': generation not found in registry.")
                return False
            prev = conn.execute("SELECT gen_id FROM models WHERE is_champion = 1").fetchone()
            prev_champion = prev[0] if prev else None

        tmp = self.pointer_path.with_name(self.pointer_path.name + f".tmp.{os.getpid()}")
        try:
            # 1) Flip the database in a single transaction.
            with sqlite3.connect(self.db_path) as conn:
                self._exec_tx(conn, [
                    ("UPDATE models SET is_champion = 0", ()),
                    ("UPDATE models SET is_champion = 1 WHERE gen_id = ?", (gen_id,)),
                ])

            # 2) Stage the pointer and swap it in.
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"champion": gen_id, "updated_at": time.time(), "previous": prev_champion},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.pointer_path)

        except OSError as e:
            logger.error(f"Champion promotion of '{gen_id}' failed (pointer write): {e}")
            # DB is already committed.  The old champion_pointer.json is
            # still intact — recovery is a retry (no data loss).
            return False
        finally:
            _unlink_quiet(tmp)

        logger.info(f"Atomically promoted generation '{gen_id}' to Champion.")
        return True

    def get_champion(self) -> dict[str, Any] | None:
        """Fetch the current champion generation record."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM models WHERE is_champion = 1").fetchone()
            if not row:
                return None
            return self._hydrate(row)

    def read_champion_pointer(self) -> dict[str, Any] | None:
        """Read ``champion_pointer.json``; None when it does not exist yet."""
        try:
            with open(self.pointer_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    # ------------------------------------------------------------------
    # listing / retention
    # ------------------------------------------------------------------

    def list_generations(self) -> list[dict[str, Any]]:
        """List all generations, newest first. Rows are never pruned."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM models ORDER BY created_at DESC, COALESCE(seq, 0) DESC"
            ).fetchall()
            return [self._hydrate(r) for r in rows]

    def get_generation(self, gen_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM models WHERE gen_id = ?", (gen_id,)).fetchone()
            return self._hydrate(row) if row else None

    def prune_old_generations(self) -> list[str]:
        """Delete store directories beyond the retention window.

        Keeps the newest ``retention_limit`` generations plus the champion.
        Database rows — the lineage record — are never deleted; a pruned row
        just gains a ``pruned_at`` timestamp. Returns the pruned gen_ids.
        """
        pruned: list[str] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT gen_id, store_path, is_champion, pruned_at FROM models "
                "ORDER BY created_at DESC, COALESCE(seq, 0) DESC"
            ).fetchall()
            for row in rows[self.retention_limit :]:
                if row["is_champion"]:
                    continue  # the serving champion's artifacts always stay
                store_path = row["store_path"]
                if not store_path:
                    continue
                p = Path(store_path)
                if p.is_dir():
                    try:
                        shutil.rmtree(p)
                    except OSError as e:  # pragma: no cover - permission dependent
                        logger.warning(f"could not prune {p}: {e}")
                        continue
                elif row["pruned_at"]:
                    continue
                conn.execute("UPDATE models SET pruned_at = ? WHERE gen_id = ?", (time.time(), row["gen_id"]))
                pruned.append(row["gen_id"])
        if pruned:
            logger.info(f"Pruned artifacts for {len(pruned)} generation(s) beyond retention: {pruned}")
        return pruned

    # ------------------------------------------------------------------

    @staticmethod
    def _hydrate(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["metadata"] = json.loads(d.get("metadata_json") or "{}")
        d["gate_report"] = json.loads(d.get("gate_report_json") or "{}")
        return d
