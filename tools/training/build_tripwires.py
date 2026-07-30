"""Build and evaluate tripwire puzzle fixtures (WP-1.2 / T5).

Categories (hand-verified game states):
  - obvious_keep         (15 fixtures) — Mono Green Stompy keepable hand
  - obvious_mulligan     (15 fixtures) — Azorius Control 0-land hand
  - lethal_on_board      (10 fixtures) — 2x Grizzly Bears vs empty board at 4HP
  - free_counterspell    (10 fixtures) — Counterspell vs Sheoldred on stack
  - obvious_develop      (2 fixtures)  — Turn 2 Llanowar Elves curve-out
  - obvious_attack       (2 fixtures)  — Turn 5 attack with 2 creatures
  - empty_pass           (1 fixture)   — Opp end-step, nothing on stack, pass
                           ─────────────────
                           55 total fixtures (7 unique scenarios)

Each fixture is derived from a real game state in the repo's evaluation data
(``tools/eval/data/seed_prompts.jsonl``) or from a hand-crafted puzzle that
an MTG player would unambiguously get right.

Usage:
  python tools/training/build_tripwires.py         # generate fixtures
  python tools/training/build_tripwires.py --check  # verify fixture counts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arenamcp.action_planner import ACTION_SCHEMA  # noqa: E402


def generate_tripwire_fixtures() -> list[dict]:
    """Return all tripwire puzzle fixtures.

    Returns 51 fixtures representing 7 unique scenarios. Each category is
    replicated N times so that a model evaluated over the full set gets
    multiple shots at each decision type (statistical reliability).
    """
    fixtures = []

    # 1. Obvious Keeps (15 fixtures)
    for i in range(1, 16):
        user_text = (
            "Match 7 - Game 1. On the play.\n"
            "Hand (7 cards): 3 Forest, Llanowar Elves, Elvish Archdruid, Beast Whisperer, Craterhoof Behemoth.\n"
            "Deck: Mono Green Stompy.\n"
            "Mulligan options: KEEP, MULLIGAN to 6.\n"
            "Legal: [1] KEEP [2] MULLIGAN"
        )
        fixtures.append(
            {
                "id": f"tripwire_keep_{i:03d}",
                "category": "obvious_keep",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "mulligan_keep",
            }
        )

    # 2. Obvious Mulligans (15 fixtures)
    for i in range(1, 16):
        user_text = (
            "Match 7 - Game 1. On the play.\n"
            "Hand (7 cards): 0 Lands, 7 5-drop spells (Sunfall, Teferi, Wandering Emperor, Elspeth, Memory Deluge, Farewell, Archangel).\n"
            "Deck: Azorius Control.\n"
            "Mulligan options: KEEP, MULLIGAN to 6.\n"
            "Legal: [1] KEEP [2] MULLIGAN"
        )
        fixtures.append(
            {
                "id": f"tripwire_mull_{i:03d}",
                "category": "obvious_mulligan",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "mulligan_mull",
            }
        )

    # 3. Lethal on Board (10 fixtures)
    for i in range(1, 11):
        user_text = (
            "Turn 5 - Main Phase 1. Opponent HP: 4. Opponent battlefield: Empty.\n"
            "Your battlefield: 2x 2/2 Grizzly Bears (untapped). No summoning sickness.\n"
            "Legal: [1] Declare Attackers (Attack All) [2] Pass Turn [3] Play Land"
        )
        fixtures.append(
            {
                "id": f"tripwire_lethal_{i:03d}",
                "category": "lethal_on_board",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "declare_attackers",
            }
        )

    # 4. Free Counterspell / Obvious Reaction (10 fixtures)
    for i in range(1, 11):
        user_text = (
            "Turn 4 - Opponent turn. Stack: Opponent casts Sheoldred, the Apocalypse.\n"
            "Your hand: Counterspell (open UU lands available).\n"
            "Legal: [1] Cast Counterspell targeting Sheoldred [2] Pass Priority"
        )
        fixtures.append(
            {
                "id": f"tripwire_counter_{i:03d}",
                "category": "free_counterspell",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "cast_spell",
            }
        )

    # 5. Obvious develop — Turn 2 curve out (2 fixtures)
    # Derived from seed-001-curve in tools/eval/data/seed_prompts.jsonl
    for i in range(1, 3):
        user_text = (
            "Turn 2, your turn, Main Phase 1. Life: you 20, opp 20.\n"
            "Mana: {G:2}. Hand: Llanowar Elves, Forest, Cut Down, Lightning Strike.\n"
            "Battlefield: Forest (untapped), Mountain (untapped). Opponent's battlefield: empty.\n"
            "Legal: [1] Cast Llanowar Elves [2] Play Forest [3] Pass priority"
        )
        fixtures.append(
            {
                "id": f"tripwire_develop_{i:03d}",
                "category": "obvious_develop",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "cast_spell",
            }
        )

    # 6. Obvious attack — Turn 5 declare attackers (2 fixtures)
    # Derived from seed-003-attackers
    for i in range(1, 3):
        user_text = (
            "Turn 5, your turn, Combat: Declare Attackers. Life: you 18, opp 9.\n"
            "Your battlefield: Bristly Bill, Spine Sower (3/3), Llanowar Elves (1/1).\n"
            "Opponent's battlefield: 3x 1/1 Human tokens.\n"
            "Legal: [1] Declare Attackers (Attack All) [2] Pass (no attackers)"
        )
        fixtures.append(
            {
                "id": f"tripwire_attack_{i:03d}",
                "category": "obvious_attack",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "declare_attackers",
            }
        )

    # 7. Empty pass — opponent end step with no stack (1 fixture)
    # Derived from seed-005-end-step
    user_text = (
        "Turn 4, opponent's End Step. Life: you 12, opp 20.\n"
        "Mana: {U:2}. Hand: Make Disappear, Island.\n"
        "Battlefield: 4 Island (all untapped). Opponent just passed without casting anything.\n"
        "Legal: [1] Pass priority"
    )
    fixtures.append(
        {
            "id": "tripwire_pass_001",
            "category": "empty_pass",
            "system": ACTION_SCHEMA,
            "user": user_text,
            "expected_action_type": "pass_priority",
        }
    )

    return fixtures


def run_tripwire_eval(
    fixtures: list[dict],
    model_fn: callable,
    *,
    verbose: bool = True,
) -> dict:
    """Evaluate a model function against all tripwire fixtures.

    ``model_fn`` receives (system_prompt, user_prompt) and must return
    a dict with at least an ``action_type`` key (or a JSON response string).
    Returns a dict with per-category scores and overall pass rate.
    """
    from tools.training.validators import validate_action_schema_json

    results = {}
    for fixture in fixtures:
        fid = fixture["id"]
        cat = fixture["category"]
        expected = fixture["expected_action_type"]

        output = model_fn(fixture["system"], fixture["user"])

        # Parse response — try JSON first
        ok, _ = validate_action_schema_json(output)
        if ok:
            import json as _json

            try:
                data = _json.loads(output)
                action_type = data.get("actions", [{}])[0].get("action_type", None)
                if action_type is None:
                    # Check for pick-based response
                    pick = data.get("actions", [{}])[0].get("pick", None)
                    action_type = str(pick) if pick is not None else None
            except Exception:
                action_type = None
        else:
            # Plain text response
            action_type = output.strip().upper() if output else None

        passed = str(action_type).lower() == expected.lower()
        results[fid] = {
            "category": cat,
            "expected": expected,
            "got": action_type,
            "passed": passed,
        }

    # Category breakdown
    cats = {}
    for fid, r in results.items():
        cat = r["category"]
        if cat not in cats:
            cats[cat] = {"total": 0, "passed": 0}
        cats[cat]["total"] += 1
        if r["passed"]:
            cats[cat]["passed"] += 1

    total = len(results)
    passed_total = sum(1 for r in results.values() if r["passed"])
    summary = {
        "total_fixtures": total,
        "passed": passed_total,
        "failed": total - passed_total,
        "pass_rate": round(passed_total / total * 100, 1) if total else 0.0,
        "categories": cats,
        "results": results,
    }

    if verbose:
        print(f"\n{'='*60}")
        print("TRIPWIRE EVALUATION")
        print(f"{'='*60}")
        print(f"Total: {total} | Passed: {passed_total} | Failed: {total - passed_total}")
        print(f"Overall pass rate: {summary['pass_rate']}%")
        print("\nCategory breakdown:")
        for cat, s in sorted(cats.items()):
            pct = round(s["passed"] / s["total"] * 100, 1) if s["total"] else 0.0
            print(f"  {cat:25s} {s['passed']:3d}/{s['total']:<3d} ({pct:5.1f}%)")
        print(f"{'='*60}")

    return summary


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build tripwire puzzle fixtures")
    parser.add_argument("--check", action="store_true", help="Check fixture count and uniqueness")
    parser.add_argument("--output", type=str, default=None, help="Output path (default: tools/training/data/tripwire_fixtures.jsonl)")
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else REPO / "tools/training/data/tripwire_fixtures.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fixtures = generate_tripwire_fixtures()

    with open(out_path, "w", encoding="utf-8") as f:
        for fix in fixtures:
            f.write(json.dumps(fix, ensure_ascii=False) + "\n")

    print(f"Generated {len(fixtures)} tripwire puzzle fixtures at {out_path}")

    if args.check:
        categories = {}
        seen_users = {}
        for fix in fixtures:
            cat = fix["category"]
            categories[cat] = categories.get(cat, 0) + 1
            # Check uniqueness of user prompt (ignoring id)
            user_key = fix["user"]
            if user_key not in seen_users:
                seen_users[user_key] = {"id": fix["id"], "count": 0}
            seen_users[user_key]["count"] += 1

        print(f"\nCategory breakdown ({len(categories)} categories):")
        for cat, count in sorted(categories.items()):
            print(f"  {cat:30s} {count:3d} fixtures")

        print(f"\nUnique game states: {len(seen_users)}")
        for user_key, info in seen_users.items():
            prefix = user_key[:60].replace("\n", " | ")
            print(f"  [{info['id']}] {prefix}... (x{info['count']})")


if __name__ == "__main__":
    main()
