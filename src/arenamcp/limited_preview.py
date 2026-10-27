"""Small, read-only desktop projection of the actual limited build candidates."""

from collections import Counter

from arenamcp.limited_deck import normalize_card

BASICS = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}


def deck_preview(plan: dict, event: str) -> dict:
    pool = {c["grp_id"]: c for c in plan["cards"]}
    pool_counts = Counter(c["grp_id"] for c in plan["cards"])
    options = []
    seen = set()
    for build in [plan["build"], *plan.get("options", [])]:
        kept = Counter()
        for entry in build.get("main_deck", []):
            kept[entry["grp_id"]] += entry["count"]
        basics = {k: v for k, v in build.get("basic_lands", {}).items() if v}
        identity = (tuple(sorted(kept.items())), tuple(sorted(basics.items())))
        if identity in seen:
            continue
        seen.add(identity)
        rows, cuts, curve = [], [], Counter()
        lands = sum(basics.values())
        for gid, count in kept.items():
            card = pool.get(gid, {})
            is_land = "land" in card.get("type_line", "").lower()
            mv = normalize_card(card).cmc if card else None
            rows.append(
                {
                    "name": card.get("name", f"Card {gid}"),
                    "count": count,
                    "cost": card.get("mana_cost", ""),
                    "mv": mv,
                    "land": is_land,
                }
            )
            if is_land:
                lands += count
            elif mv is not None:
                curve[str(min(7, int(mv)))] += count
        rows.sort(key=lambda r: (r["land"], r["mv"] or 0, r["name"]))
        rows.extend(
            {"name": BASICS.get(c, c), "count": n, "cost": "", "land": True} for c, n in basics.items()
        )
        reasons = {c.get("grp_id"): c.get("reason", "") for c in build.get("cuts", [])}
        for gid, card in pool.items():
            count = pool_counts[gid] - kept[gid]
            if count > 0:
                cuts.append(
                    {"name": card.get("name", f"Card {gid}"), "count": count, "reason": reasons.get(gid, "")}
                )
        options.append(
            {
                "recommended": not options,
                "cards": rows,
                "cuts": cuts,
                "total": sum(kept.values()) + sum(basics.values()),
                "lands": lands,
                "curve": dict(curve),
                "plan": build.get("plan", ""),
            }
        )
        if len(options) == 3:
            break
    return {"event": event, "source": plan.get("source", ""), "options": options}
