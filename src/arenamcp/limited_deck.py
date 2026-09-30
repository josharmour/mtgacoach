"""Counted, validated forty-card builds and spoken cuts for Limited pools."""

import re
from collections import Counter
from itertools import combinations
from typing import Any

from arenamcp.draft_guidance import normalize_card
from arenamcp.limited_rules import rules_profile, synergy_evidence

BASIC_NAMES = {"W": "Plains", "U": "Islands", "B": "Swamps", "R": "Mountains", "G": "Forests"}
COLOR_NAMES = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}

DECK_SYSTEM_PROMPT = """Build exactly 40 cards for this Limited draft from the ACTUAL counted pool.
Use only available copies. Ordinary basic lands can be added freely through basic_lands;
nonbasic lands and every spell must come from the pool. Aim for 23 spells and 17 total
lands, adjusting within 15-19 lands only when the curve warrants it. Prioritize a coherent
two-color plan, early creatures, interaction, supported enablers/payoffs, and a castable
mana base. No unsupported splashes. Read full rules and related_faces, not card names.
Hybrid mana can use either color. Adding loyalty counters is not a loyalty activation.
Honor the previous theme only if this actual pool supports it. Missing ratings are unknown.
Use supported_synergies as grounded links, checking the supplied rules' conditions and
costs. For unmodeled interactions, describe roles rather than inventing a verified combo.
Return JSON only: {"main_deck": [{"grp_id":123,"count":2}],
"basic_lands":{"G":9,"U":8}, "plan":"one short sentence",
"cuts":[{"grp_id":456,"reason":"why this card loses its slot, at most 12 words"}]}.
main_deck includes chosen pool spells AND any drafted nonbasic lands. Supply a concrete
cut reason for every nonbasic pool card excluded, including excess copies. Never assume
the pool is already in the editor: this is a proposed build, not a verified editor view.
"""


def _nonbasics(pool: list[dict]) -> list[dict]:
    return [card for card in pool if "basic land" not in card.get("type_line", "").lower()]


def _castable(card: dict, colors: tuple[str, ...]) -> bool:
    for symbol in re.findall(r"\{([^}]+)\}", card.get("mana_cost", "")):
        choices = set(symbol.split("/"))
        if choices & set("WUBRGC") and not choices & set(colors) and not choices & {"2", "P"}:
            return False
    return True


def validate_deck(payload: dict, pool: list[dict], *, source: str) -> dict[str, Any]:
    cards = _nonbasics(pool)
    available = Counter(card["grp_id"] for card in cards)
    by_id = {card["grp_id"]: card for card in cards}
    kept: Counter = Counter()
    for entry in payload["main_deck"]:
        grp_id, count = entry["grp_id"], entry["count"]
        if type(grp_id) is not int or type(count) is not int or count <= 0:
            raise ValueError("Deck entries need integer IDs and positive counts")
        kept[grp_id] += count
        if kept[grp_id] > available[grp_id]:
            raise ValueError("Deck uses unavailable cards or too many copies")
    basics = payload["basic_lands"]
    if not isinstance(basics, dict) or any(
        color not in BASIC_NAMES or type(count) is not int or count < 0 for color, count in basics.items()
    ):
        raise ValueError("Invalid basic land allocation")
    mana_colors = {color for color, count in basics.items() if count}
    for grp_id in kept:
        card = by_id[grp_id]
        if "land" in card.get("type_line", "").lower():
            mana_colors.update(card.get("produced_mana") or [])
            for clause in re.findall(r"\badd\b[^.\n]*", card.get("oracle_text", "").lower()):
                mana_colors.update(symbol.upper() for symbol in re.findall(r"\{([wubrgc])\}", clause))
                if "any color" in clause:
                    mana_colors.update("WUBRG")
    if any(not _castable(by_id[grp_id], tuple(mana_colors)) for grp_id in kept):
        raise ValueError("Deck includes colors its proposed mana base cannot produce")
    total = sum(kept.values()) + sum(basics.values())
    lands = sum(
        count for grp_id, count in kept.items() if "land" in by_id[grp_id].get("type_line", "").lower()
    )
    lands += sum(basics.values())
    if total != 40 or not 15 <= lands <= 19:
        raise ValueError("Draft deck must total 40 cards with a supported land count")
    reasons = {entry["grp_id"]: entry.get("reason", "") for entry in payload.get("cuts", [])}
    cuts = []
    for grp_id, count in (available - kept).items():
        reason = reasons.get(grp_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Every cut needs an explanation")
        cuts.append(
            {
                "grp_id": grp_id,
                "name": by_id[grp_id]["name"],
                "count": count,
                "reason": " ".join(reason.split()[:16]),
            }
        )
    plan = payload.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        raise ValueError("Deck needs a plan")
    main = [
        {"grp_id": grp_id, "name": by_id[grp_id]["name"], "count": count} for grp_id, count in kept.items()
    ]
    land_parts = [f"{count} {BASIC_NAMES[color]}" for color, count in basics.items() if count]
    land_parts.extend(
        f"{entry['count']} {entry['name']}"
        for entry in main
        if "land" in by_id[entry["grp_id"]].get("type_line", "").lower()
    )
    spoken = "For a 40-card deck from your drafted pool, leave these out of the main deck. "
    if cuts:
        spoken += " ".join(f"Cut {cut['count']} {cut['name']}: {cut['reason']}." for cut in cuts)
    else:
        spoken = "Keep all your drafted spells in this 40-card build."
    spoken += f" Use {total - lands} spells and {lands} lands: {', '.join(land_parts)}. That's 40 cards."
    detailed = spoken + "\nPlan: " + plan.strip()
    detailed += "\nKeep:\n" + "\n".join(f"{entry['count']} {entry['name']}" for entry in main)
    return {
        "main_deck": main,
        "basic_lands": dict(basics),
        "cuts": cuts,
        "total_cards": total,
        "land_count": lands,
        "spell_count": total - lands,
        "plan": plan.strip(),
        "spoken_advice": spoken,
        "detailed_text": detailed,
        "reasoning_source": source,
        "basis": "drafted_pool",
    }


def fallback_deck(pool: list[dict]) -> dict[str, Any]:
    spells = [card for card in _nonbasics(pool) if "land" not in card.get("type_line", "").lower()]

    def score(card):
        normalized = normalize_card(card)
        value = (normalized.gih_wr_pct - 50) if normalized.gih_wr_pct is not None else 0
        value += 4 if rules_profile(card)["unconditional_body"] else 0
        value += 4 if "removal" in normalized.tags else 0
        value += 2 if "card_advantage" in normalized.tags else 0
        value += 2 if normalized.cmc <= 3 else -max(0, normalized.cmc - 4)
        return value

    best = None
    for colors in combinations("WUBRG", 2):
        candidates = [card for card in spells if card.get("type_line") and _castable(card, colors)]
        chosen = []
        while candidates and len(chosen) < 23:
            bodies = sum(rules_profile(card)["unconditional_body"] for card in chosen)
            early = sum(
                rules_profile(card)["unconditional_body"] and normalize_card(card).cmc <= 3 for card in chosen
            )
            expensive = sum(normalize_card(card).cmc >= 5 for card in chosen)

            def fit(card, bodies=bodies, early=early, expensive=expensive, chosen=tuple(chosen)):
                profile = rules_profile(card)
                cost = normalize_card(card).cmc
                value = score(card)
                if profile["unconditional_body"]:
                    value += 5 if bodies < 14 else 0
                    value += 4 if early < 6 and cost <= 3 else 0
                if cost >= 5 and expensive >= 4:
                    value -= 6
                value += min(4, sum(bool(synergy_evidence(card, other)) for other in chosen))
                return value

            picked = max(candidates, key=fit)
            candidates.remove(picked)
            chosen.append(picked)
        support = sum(
            bool(synergy_evidence(first, second))
            for index, first in enumerate(chosen)
            for second in chosen[index + 1 :]
        )
        ranking = (len(chosen), sum(score(card) for card in chosen) + min(12, support))
        if best is None or ranking > best[0]:
            best = (ranking, colors, chosen)
    if best is None or len(best[2]) < 21:
        return {
            "spoken_advice": "I don't yet have enough castable spells to verify a balanced 40-card build. Check that the full draft pool is available.",
            "detailed_text": "No verified 40-card build is available from this pool.",
            "reasoning_source": "heuristic",
            "basis": "drafted_pool",
        }
    _, colors, chosen = best
    kept = Counter(card["grp_id"] for card in chosen)
    weights = Counter({color: 0 for color in colors})
    for card in chosen:
        for symbol in re.findall(r"\{([^}]+)\}", card.get("mana_cost", "")):
            payable = set(symbol.split("/")) & set(colors)
            for color in payable:
                weights[color] += 1 / len(payable)
    land_count = 40 - len(chosen)
    total_weight = sum(weights.values())
    first_count = round(land_count * weights[colors[0]] / total_weight) if total_weight else land_count // 2
    if all(weights[color] for color in colors):
        first_count = min(land_count - 6, max(6, first_count))
    basics = {colors[0]: first_count, colors[1]: land_count - first_count}
    cuts = []
    for card in _nonbasics(pool):
        if not _castable(card, colors):
            reason = "outside the chosen colors without a supported mana base"
        elif "land" in card.get("type_line", "").lower():
            reason = "this starting build uses basic lands for consistent colored mana"
        elif normalize_card(card).cmc >= 5:
            reason = "reduce expensive cards and preserve earlier plays"
        else:
            reason = "keep the stronger on-color creatures and interaction in this starting build"
        cuts.append({"grp_id": card["grp_id"], "reason": reason})
    return validate_deck(
        {
            "main_deck": [{"grp_id": grp_id, "count": count} for grp_id, count in kept.items()],
            "basic_lands": basics,
            "cuts": cuts,
            "plan": f"A curve-based {'/'.join(COLOR_NAMES[color] for color in colors)} starting build; refine synergies from the full rules.",
        },
        pool,
        source="heuristic",
    )


def reconcile_logged_deck(build: dict) -> dict:
    """Describe changes against the last observed deck, never assume unsaved edits."""
    current = build.get("editor_cards")
    if current is None or build.get("total_cards") != 40:
        return build
    basic_types = {"W": "plains", "U": "island", "B": "swamp", "R": "mountain", "G": "forest"}
    desired = Counter({entry["grp_id"]: entry["count"] for entry in build["main_deck"]})
    desired.update({color: count for color, count in build["basic_lands"].items()})
    observed = Counter()
    names = {entry["grp_id"]: entry["name"] for entry in build["main_deck"]}
    names.update(BASIC_NAMES)
    for card in current:
        kind = str(card.get("type_line") or "").lower()
        key = next(
            (color for color, land_type in basic_types.items() if "basic land" in kind and land_type in kind),
            card["grp_id"],
        )
        observed[key] += card["count"]
        if isinstance(key, int):
            names[key] = card.get("name") or f"Card {key}"
    remove, add = observed - desired, desired - observed
    live = build.get("editor_basis") == "live_editor"
    text = f"Your {'current' if live else 'last logged'} deck has {sum(observed.values())} cards. "
    if remove:
        reasons = {entry["grp_id"]: entry["reason"] for entry in build["cuts"]}
        text += (
            "Cut "
            + "; ".join(
                f"{count} {names[key]}" + (f" — {reasons[key]}" if key in reasons else "")
                for key, count in remove.items()
            )
            + ". "
        )
    if add:
        text += "Add " + "; ".join(f"{count} {names[key]}" for key, count in add.items()) + ". "
    if not remove and not add:
        text += "It already matches the recommended 40-card build. "
    else:
        text += "Those changes make exactly 40 cards. "
    if not live:
        text += "Unsaved editor changes may not be logged yet."
    return {
        **build,
        "basis": "live_editor" if live else "logged_deck",
        "remaining_cuts": dict(remove),
        "remaining_additions": dict(add),
        "spoken_advice": text,
        "detailed_text": text + "\n" + build["detailed_text"],
    }
