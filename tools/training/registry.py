"""Content-addressed Model Registry for MTGA Coach models.

Manages model generation lineage, gate verification reports, and atomic champion pointer flips
in registry.sqlite. Deletion of past generations is prohibited; last 10 generations retained.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tools.training.registry")


class ModelRegistry:
    """Manages model checkpoints, lineage, and atomic promotion pointers."""

    def __init__(self, registry_dir: Path):
        self.registry_dir = registry_dir.resolve()
        self.models_dir = self.registry_dir / "models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.registry_dir / "registry.sqlite"
        self._init_db()

    def _init_db(self):
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

    def register_generation(
        self,
        gen_id: str,
        base_model: str,
        adapter_path: Path,
        parent_gen: str | None = None,
        git_sha: str | None = None,
        metadata: dict[str, Any] | None = None,
        gate_report: dict[str, Any] | None = None,
    ) -> str:
        """Register a new candidate model generation in the database."""
        created_at = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        report_json = json.dumps(gate_report or {}, ensure_ascii=False)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO models 
                (gen_id, base_model, created_at, parent_gen, git_sha, adapter_path, metadata_json, gate_report_json, is_champion)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
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
                ),
            )
        logger.info(f"Registered model generation '{gen_id}' in registry.")
        return gen_id

    def promote_champion(self, gen_id: str) -> bool:
        """Atomically set gen_id as the champion model."""
        with sqlite3.connect(self.db_path) as conn:
            # Check existence
            row = conn.execute("SELECT gen_id FROM models WHERE gen_id = ?", (gen_id,)).fetchone()
            if not row:
                logger.error(f"Cannot promote '{gen_id}': generation not found in registry.")
                return False

            # Reset previous champion and set new champion
            conn.execute("UPDATE models SET is_champion = 0")
            conn.execute("UPDATE models SET is_champion = 1 WHERE gen_id = ?", (gen_id,))

        pointer_file = self.registry_dir / "champion_pointer.json"
        with open(pointer_file, "w", encoding="utf-8") as f:
            json.dump({"champion": gen_id, "updated_at": time.time()}, f, indent=2)

        logger.info(f"✓ Atomically promoted generation '{gen_id}' to Champion!")
        return True

    def get_champion(self) -> dict[str, Any] | None:
        """Fetch the current champion generation record."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM models WHERE is_champion = 1").fetchone()
            if not row:
                return None
            data = dict(row)
            data["metadata"] = json.loads(data.get("metadata_json") or "{}")
            data["gate_report"] = json.loads(data.get("gate_report_json") or "{}")
            return data

    def list_generations(self) -> list[dict[str, Any]]:
        """List all generations ordered by creation time."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM models ORDER BY created_at DESC").fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d.get("metadata_json") or "{}")
                d["gate_report"] = json.loads(d.get("gate_report_json") or "{}")
                results.append(d)
            return results
