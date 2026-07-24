"""17lands Public Match Replay Ingestion Tool.

Parses 17lands public match replay files (mulligans, game replay CSV/JSON)
and converts them into Stage 0 SFT training prompts serialized to ACTION_SCHEMA JSON format.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

from arenamcp.action_planner.coach_prompts import DEFAULT_SYSTEM_PROMPT
from arenamcp.action_planner import game_action_to_schema_json

logger = logging.getLogger("tools.training.ingest_17lands")


def parse_17lands_mulligans(csv_or_json_path: Path) -> list[dict[str, str]]:
    """Parse 17lands mulligan records into SFT prompts."""
    records = []
    logger.info(f"Ingesting 17lands mulligans from {csv_or_json_path}")
    
    with open(csv_or_json_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if content.startswith("[") or content.startswith("{"):
            data = json.loads(content)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                sys_p = item.get("system") or DEFAULT_SYSTEM_PROMPT
                usr_p = item.get("user") or item.get("prompt") or ""
                resp = item.get("response") or item.get("decision") or "KEEP"
                resp_json = game_action_to_schema_json(resp)
                if usr_p:
                    records.append({
                        "system": sys_p,
                        "user": usr_p,
                        "response": resp_json,
                    })
        else:
            reader = csv.DictReader(content.splitlines())
            for row in reader:
                hand = row.get("hand") or row.get("opening_hand") or ""
                keep = row.get("keep") or row.get("action") or "1"
                decision = "KEEP" if str(keep).strip() in ("1", "true", "keep", "KEEP") else "MULLIGAN"
                usr_p = f"Opening Hand: {hand}\nDecision: Keep or Mulligan?"
                records.append({
                    "system": DEFAULT_SYSTEM_PROMPT,
                    "user": usr_p,
                    "response": game_action_to_schema_json(decision),
                })

    logger.info(f"Parsed {len(records)} 17lands SFT prompt examples.")
    return records


def main():
    p = argparse.ArgumentParser(description="Ingest 17lands public match dataset into SFT dataset.")
    p.add_argument("--input", required=True, type=Path, help="Input 17lands CSV or JSON file")
    p.add_argument("--output", required=True, type=Path, help="Output SFT dataset JSON file")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    records = parse_17lands_mulligans(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    logger.info(f"✓ Saved {len(records)} Stage 0 SFT prompts to {args.output}")


if __name__ == "__main__":
    main()
