"""Read-only coverage audit: python -m arenamcp.card_audit."""

import json
import sqlite3

from arenamcp.card_db import MTGADatabaseAdapter, is_bogus_type_line, is_unknown_card_name
from arenamcp.mtgadb import MTGADatabase, find_mtga_database


def audit_records(records: list[dict], database) -> dict:
    result = {
        "total_records": len(records),
        "card_records": 0,
        "catalog_placeholders": [],
        "effect_markers": [],
        "unresolved": [],
    }
    for offset in range(0, len(records), 500):
        batch = records[offset : offset + 500]
        cards = database.prewarm_cards([row["GrpId"] for row in batch])
        for row in batch:
            identity = row["GrpId"]
            if 0 <= identity <= 10:
                result["catalog_placeholders"].append(identity)
                continue
            card = cards.get(identity)
            if not row.get("Types") and row.get("IsToken") and not row.get("IsPrimaryCard"):
                result["effect_markers"].append({"grp_id": identity, "name": card.name if card else ""})
                continue
            result["card_records"] += 1
            missing = []
            if card is None or is_unknown_card_name(card.name):
                missing.append("name")
            if card is None or not card.type_line or is_bogus_type_line(card.type_line):
                missing.append("type_line")
            if row.get("OldSchoolManaText") and (card is None or not card.mana_cost):
                missing.append("mana_cost")
            if row.get("AbilityIds") and (card is None or not card.oracle_text):
                missing.append("rules_text")
            if missing:
                result["unresolved"].append({"grp_id": identity, "missing": missing})
    result["resolved_card_records"] = result["card_records"] - len(result["unresolved"])
    return result


def main() -> int:
    path = find_mtga_database()
    if path is None:
        print(json.dumps({"error": "No installed Arena card database found"}))
        return 1
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT GrpId, Types, IsToken, IsPrimaryCard, OldSchoolManaText, AbilityIds FROM Cards"
            )
        ]
    result = audit_records(rows, MTGADatabaseAdapter(MTGADatabase(path)))
    print(json.dumps(result, indent=2))
    return int(bool(result["unresolved"]))


if __name__ == "__main__":
    raise SystemExit(main())
