#!/usr/bin/env python3
"""Offline ranking-component replay of saved audit rows; no model or live actions.

This reports agreement with saved dynamic scores, not optimal picks or win rate.
Reserved replay checks may already be exposed; this tool never calls them blind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.audit_untapped_drafts import dynamic_comparison, summarize

from arenamcp.draft_autopick import is_ordinary_basic, rank_pack
from arenamcp.draft_event import DraftEventDriver
from arenamcp.mtgadb import MTGADatabase
from arenamcp.set_primer import SetPrimer


def replay_draft(draft: dict, database, primer: SetPrimer | None) -> dict:
    """Inputs to ranking exclude all recorded choices, rationale and scores."""
    cube = "cube" in str(draft.get("event") or "").lower()
    rows = []
    for row in draft["picks"]:
        pack = [int(c["id"]) for c in row["offered"]]
        pool = [int(c) for c in row["pool_before_ids"]]
        metadata = {g: database.get_card(g) for g in set(pack + pool)}
        missing = [g for g, card in metadata.items() if card is None or not card.type_line]
        if missing or not pack or (not cube and primer is None):
            rows.append(
                dict(
                    pack=row["pack"],
                    pick=row["pick"],
                    skipped="missing metadata or event primer",
                    missing_ids=missing,
                )
            )
            continue
        actual_ids = {g: g for g in pack}
        if cube:
            ranking = DraftEventDriver._unrated_ranking(
                SimpleNamespace(_db=database), pack, pool, row["pack"], row["pick"]
            )
        else:
            # A primer snapshot and actual pool reproduce this component,
            # not the historical model, retained plan, table signals or wheel.
            by_name = primer.by_name()
            canonical = {
                g: g
                if primer.card(g) is not None or c.name.lower() not in by_name
                else by_name[c.name.lower()].grp_id
                for g, c in metadata.items()
            }
            if any(
                primer.card(canonical[g]) is None and not is_ordinary_basic(metadata[g].name)
                for g in set(pack + pool)
            ):
                rows.append(
                    dict(pack=row["pack"], pick=row["pick"], skipped="primer lacks an offered or owned card")
                )
                continue
            ranking = rank_pack(
                [canonical[g] for g in pack],
                [canonical[g] for g in pool],
                primer,
                pack_number=row["pack"],
                pick_number=row["pick"],
                mana_costs={canonical[g]: c.mana_cost for g, c in metadata.items()},
                names={canonical[g]: c.name for g, c in metadata.items()},
            )
            actual_ids = {}
            for g in pack:
                actual_ids.setdefault(canonical[g], g)
        chosen = str(actual_ids[ranking[0].grp_id])
        # Freely available basics are excluded symmetrically, never assigned an
        # imputed score. All missing NONBASIC scores still exclude the row.
        compared = [c for c in row["offered"] if not is_ordinary_basic(c["name"])]
        ids = [c["id"] for c in compared]
        scores = {c["id"]: {"dynamicScore": c.get("dynamic_score")} for c in compared}
        current = dynamic_comparison(ids, scores, chosen)
        historical = dynamic_comparison(ids, scores, row.get("coach_pick_id"))
        rows.append(
            dict(
                pack=row["pack"],
                pick=row["pick"],
                pool_before_ids=row["pool_before_ids"],
                ranking=[dict(id=str(actual_ids[c.grp_id]), name=c.name, score=c.score) for c in ranking],
                replay_pick_id=chosen,
                recorded_coach_pick_id=row.get("coach_pick_id"),
                replay_comparison=current,
                historical_comparison=historical,
            )
        )
    paired = [
        r
        for r in rows
        if r.get("replay_comparison", {}).get("complete_coverage")
        and r.get("historical_comparison", {}).get("complete_coverage")
        and r["replay_comparison"]["gap"] is not None
        and r["historical_comparison"]["gap"] is not None
    ]
    return dict(
        draft_id=draft["draft_id"],
        event=draft.get("event"),
        recorded_partition=draft.get("partition"),
        evaluated_rows=sum("ranking" in r for r in rows),
        skipped_rows=sum("skipped" in r for r in rows),
        replay_summary=summarize(paired, "replay_comparison"),
        historical_summary=summarize(paired, "historical_comparison"),
        picks=rows,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--card-db", type=Path, required=True)
    parser.add_argument("--draft-id", action="append", required=True)
    parser.add_argument("--primer", action="append", default=[], help="SET=/path/to/frozen-primer.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    wanted = set(args.draft_id)
    drafts = [d for d in audit["drafts"] if d["draft_id"] in wanted]
    if {d["draft_id"] for d in drafts} != wanted:
        parser.error("requested draft ID missing from audit")
    primers, hashes = {}, {}
    for item in args.primer:
        code, path = item.split("=", 1)
        data = Path(path).read_bytes()
        primers[code] = SetPrimer.from_json(data.decode())
        hashes[path] = hashlib.sha256(data).hexdigest()
    database = MTGADatabase(args.card_db)
    results = []
    for draft in drafts:
        event = str(draft.get("event") or "")
        primer = next((p for code, p in primers.items() if f"_{code}_" in event), None)
        results.append(replay_draft(draft, database, primer))
    code_root = Path(__file__).resolve().parents[1]
    report = dict(
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        model_calls=0,
        live_actions=0,
        limitations=[
            "Previously reserved drafts may have informed earlier work; no unbiased holdout is claimed.",
            "Scores measure Untapped agreement, not pick correctness, strength or win rate.",
            "Each row uses its actual historical pool; alternative earlier choices are not propagated.",
            "Replay is the deterministic ranking component; no model, prior accepted plan, table signals or wheel reconstruction.",
            "Current local metadata and explicit cached primer snapshots are used; no network refresh or historical-snapshot equivalence assumed.",
            "Paired summaries require both choices scored, complete nonbasic coverage, and more than one nonbasic choice.",
            "Narration changes do not establish ranking improvement; no per-card tuning follows this evaluation.",
        ],
        inputs=dict(
            audit=str(args.audit),
            audit_sha256=hashlib.sha256(args.audit.read_bytes()).hexdigest(),
            card_db=str(args.card_db),
            card_db_sha256=hashlib.sha256(args.card_db.read_bytes()).hexdigest(),
            primers=hashes,
        ),
        source_sha256={
            name: hashlib.sha256((code_root / "src/arenamcp" / name).read_bytes()).hexdigest()
            for name in ["draft_autopick.py", "draft_guidance.py", "draft_event.py", "draft_advisor.py"]
        },
        drafts=results,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            [
                dict(
                    draft_id=d["draft_id"],
                    evaluated_rows=d["evaluated_rows"],
                    skipped_rows=d["skipped_rows"],
                    replay=d["replay_summary"],
                    historical=d["historical_summary"],
                )
                for d in results
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
