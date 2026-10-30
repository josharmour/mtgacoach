"""Select useful whole facts within the existing commentary request budget."""

import json
import re


def generation_format():
    """Constrain transport shape without weakening factual validation."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "commentary",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["turns"],
                "properties": {
                    "turns": {
                        "type": "array",
                        "maxItems": 2,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["role", "text", "refs"],
                            "properties": {
                                "role": {"type": "string", "enum": ["play_by_play", "color_analyst"]},
                                "text": {"type": "string", "maxLength": 220},
                                "refs": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 10,
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    }
                },
            },
        },
    }


def request_size(system, context):
    return (
        len(system.encode())
        + len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode())
        + 256
    )


def compact_context(system, context):
    limit = 5900 - len(json.dumps(generation_format(), separators=(",", ":")).encode())
    result = {
        key: context[key] for key in ("booth", "allowed_roles", "style", "focus", "airtime") if key in context
    }
    result.update(
        facts={},
        new_events=[],
        heard=[],
        earlier_points=[],
        in_flight=[],
        omitted_context="Omitted facts are unknown, not absent. Speculation must be labeled and grounded in included evidence.",
    )
    facts = context["facts"]
    new = list(context.get("new_events", ()))
    # Preserve the meaningful play in a burst even when a routine land came last.
    weights = {
        "game_end": 20,
        "attack_declared": 12,
        "life_change": 8,
        "spell_cast": 10,
        "spell_resolved": 7,
        "hand_changed": 6,
    }
    ranked = sorted(new, key=lambda k: weights.get(facts.get(k, {}).get("kind"), 0), reverse=True)
    order = ranked[:2] + [k for k in facts if k.startswith("private:hand:")]
    order += ["state:turn", "state:local_seat"] + [k for k in facts if k.startswith("state:player:")]
    related = []
    for key in ranked[:2]:
        data = facts.get(key, {}).get("data", {})
        gid = data.get("grp_id") or data.get("source_grp_id")
        if gid:
            related.append(f"card:{gid}")
    for key, fact in facts.items():
        if key.startswith("private:hand:"):
            related.extend(f"card:{c['grp_id']}" for c in fact.get("cards", ()) if c.get("grp_id"))
    order += related + ["state:game_read"] + ranked[2:] + ["state:board"]
    order += [k for k in facts if k.startswith("card:")]
    for key in dict.fromkeys(order):
        if key not in facts:
            continue
        result["facts"][key] = facts[key]
        if key in new:
            result["new_events"].append(key)
        if request_size(system, result) > limit:
            result["facts"].pop(key)
            if key in result["new_events"]:
                result["new_events"].remove(key)
    for key in ("heard", "earlier_points", "in_flight", "already_called"):
        result.setdefault(key, [])
        for item in reversed(context.get(key, ())[-2:]):
            result[key].insert(0, item)
            if request_size(system, result) > limit:
                result[key].pop(0)
    return result


def deck_overview(coach, snapshot):
    """Read only the already-computed playbook for this exact match/deck."""
    engine = getattr(coach, "_coach", None)
    key = getattr(coach, "_deck_analysis_key", None)
    book = getattr(engine, "_deck_playbook", None)
    if not (
        isinstance(key, tuple)
        and len(key) == 2
        and key[0] == snapshot.get("match_id")
        and book is not None
        and book.identity == key[1]
    ):
        return None
    summary = book.data.get("spoken_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
        return None
    return {"identity": book.identity, "summary": summary.strip()}


def compact_audit(context):
    """Share cited facts between lines; never discard audit evidence."""
    facts, lines = {}, []
    for line in context["lines"]:
        facts.update(line["evidence"])
        lines.append({"index": line["index"], "text": line["text"], "refs": list(line["evidence"])})
    return {"facts": facts, "lines": lines, "coaching_allowed": context.get("coaching_allowed", False)}


def add_damage_arithmetic(facts):
    """Supply checked hypothetical arithmetic, never an observed outcome.

    Only printed fixed damage to a player/any target qualifies. Variable damage,
    creature-only damage, and inferred abilities cannot create these facts.
    """
    players = [f for k, f in facts.items() if k.startswith("state:player:") and type(f.get("life")) is int]
    for key, fact in list(facts.items()):
        if not key.startswith("card:") or not isinstance(fact.get("rules"), str):
            continue
        match = re.search(
            r"\bdeals (\d+) damage to (?:any target|target (?:player|opponent))\b",
            fact.get("rules", ""),
            re.I,
        )
        if not match:
            continue
        damage = int(match[1])
        facts[key] = {
            **fact,
            "hypothetical_arithmetic": {
                "condition": "Arithmetic only: if that player actually takes the printed damage. No target, resolution, prevention or outcome is established.",
                "players": [
                    {
                        "seat": p["seat"],
                        "life_before": p["life"],
                        "damage": damage,
                        "life_after": p["life"] - damage,
                    }
                    for p in players
                ],
            },
        }
