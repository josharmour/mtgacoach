"""Conservative rendering of printed facts in decision prompts.

Supports verified keyword-only creature bodies (via :func:`render`) as well
as compact single-line ``CardFacts`` summaries for non-option deck cards via
:func:`compact_facts_summary`. Options, linked faces, and unverified cards
retain caller Oracle text verbatim.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from arenamcp.card_facts.hashing import normalise, rules_hash
from arenamcp.card_facts.schema import CardFacts
from arenamcp.card_facts.source_identity import (
    executable_source_hash,
    legacy_lookup_hash,
    single_face_source,
)

_KEYWORDS = frozenset(
    {
        "flying",
        "first strike",
        "double strike",
        "haste",
        "vigilance",
        "trample",
        "reach",
        "deathtouch",
        "lifelink",
        "menace",
        "defender",
    }
)
_TAG_RE = re.compile(r"</?(?:i|nobr|indent(?:=4%)?)>")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_REMINDER_RE = re.compile(r"\s*\([^()]*\)")


def _strip_reminder(text: str) -> str:
    cleaned = _REMINDER_RE.sub("", text or "")
    return " ".join(cleaned.replace(" .", ".").replace(" ,", ",").split())


def _normalize_native_token(inner: str) -> str:
    if not inner.startswith("o"):
        return "{" + inner + "}"
    if "/" in inner:
        return "{" + "/".join(part[1:] if part.startswith("o") else part for part in inner.split("/")) + "}"
    parts = re.findall(r"o([0-9]+|[WUBRGCSXYZTQEP]+)", inner)
    if "".join(f"o{p}" for p in parts) == inner and len(parts) > 1:
        return "".join("{" + p + "}" for p in parts)
    return "{" + inner[1:] + "}"


_LOYALTY_BRACKET_RE = re.compile(r"(?m)^\[([+−-]?(?:\d+|X))\]:")


def _normalize_native_text(text: str) -> str:
    no_tags = _TAG_RE.sub("", text)
    no_loyalty = _LOYALTY_BRACKET_RE.sub(r"\1:", no_tags)
    return _BRACE_RE.sub(lambda match: _normalize_native_token(match.group(1)), no_loyalty)


def normalized_scryfall_card(card: dict[str, Any]) -> dict[str, str] | None:
    """Canonical-symbol normalization of a verified single-face native card dict."""
    if not isinstance(card, dict) or not single_face_source(card) or legacy_lookup_hash(card) is None:
        return None
    normalized = dict(card)
    changed = False
    for key in ("name", "mana_cost", "type_line", "oracle_text"):
        raw_val = card.get(key)
        if not isinstance(raw_val, str):
            return None
        conv = _normalize_native_text(raw_val)
        if conv != raw_val:
            normalized[key] = conv
            changed = True
    if not changed or executable_source_hash(normalized) is None:
        return None
    return normalized


def candidate_fact_hashes(card: dict[str, Any]) -> tuple[str, ...]:
    """Return raw legacy hash and (when valid/distinct) Scryfall-normalized hash."""
    from arenamcp.card_facts.client import hash_for_card

    hashes: list[str] = []
    raw_h = hash_for_card(card)
    if raw_h:
        hashes.append(raw_h)
    norm = normalized_scryfall_card(card)
    if norm is not None:
        norm_h = executable_source_hash(norm)
        if norm_h and norm_h not in hashes:
            hashes.append(norm_h)
    return tuple(hashes)


def source_text(card):
    """Exact single-face source template accepted by the keyword renderer."""
    return (
        f"Name: {card['name']}\nMana cost: {card['mana_cost']}\n"
        f"Type: {card['type_line']}\nPower/toughness: {card['power']}/{card['toughness']}\n"
        f"Rules: {card['oracle_text']}"
    )


def _unknown(value):
    if getattr(value, "_unknown_fields", None):
        return True
    if dataclasses.is_dataclass(value):
        return any(_unknown(getattr(value, field.name)) for field in dataclasses.fields(value))
    if isinstance(value, (tuple, list)):
        return any(_unknown(item) for item in value)
    return False


def render(card, record, full_text, *, enabled=False, role="unknown"):
    """Return exact full_text unless a narrow complete source contract passes.

    Only role='stable_deck' is eligible. Callers must not substitute a compact
    descriptor for dynamic battlefield state or option text. The exact template
    match prevents discarding arbitrary caller annotations or other faces.
    """
    if enabled is not True or role != "stable_deck":
        return full_text
    if type(card) is not dict or type(record) is not CardFacts or type(full_text) is not str:
        return full_text
    try:
        if full_text != source_text(card):
            return full_text
        if not single_face_source(card) or card.get("loyalty") is not None:
            return full_text
        if (
            type(record.schema_version) is not int
            or record.schema_version != 1
            or record.confidence != "high"
            or _unknown(record)
        ):
            return full_text
        if (
            record.effects
            or record.mana
            or record.threat
            or record.relationships
            or record.limited
            or record.notes
        ):
            return full_text
        if (record.name, record.mana_cost, record.type_line) != (
            card["name"],
            card["mana_cost"],
            card["type_line"],
        ):
            return full_text
        if record.rules_hash != rules_hash(
            record.name, record.mana_cost, record.type_line, card["oracle_text"]
        ):
            return full_text
        if "Creature" not in record.type_line or "//" in record.name or record.body.loyalty is not None:
            return full_text
        if not all(re.fullmatch(r"\d+", str(card[key])) for key in ("power", "toughness")):
            return full_text
        if (str(record.body.power), str(record.body.toughness)) != (
            str(card["power"]),
            str(card["toughness"]),
        ):
            return full_text
        keywords = tuple(normalise(word) for word in record.body.keywords)
        if any(word not in _KEYWORDS for word in keywords) or len(set(keywords)) != len(keywords):
            return full_text
        if normalise(card["oracle_text"]) != ", ".join(keywords):
            return full_text
        if len(record.modes) != 1:
            return full_text
        mode = record.modes[0]
        cost = mode.cost
        if (
            mode.name != "cast"
            or mode.stays_on_battlefield is not True
            or mode.cost_text != record.mana_cost
            or cost.mana != record.mana_cost
            or not re.fullmatch(r"(?:\{[0-9WUBRGC]+\})+", record.mana_cost)
            or cost.tap
            or cost.sacrifice is not None
            or cost.life
            or cost.discard
            or cost.exile_from_graveyard
        ):
            return full_text
        compact = (
            f"{record.name} — {record.mana_cost}; {record.type_line}; "
            f"{record.body.power}/{record.body.toughness}"
            + (f"; {', '.join(record.body.keywords)}" if keywords else "")
        )
        return compact if len(compact) < len(full_text) else full_text
    except (AttributeError, KeyError, TypeError, ValueError):
        return full_text


def _format_effect(entry: Any, *, is_permanent: bool = True) -> str:
    effect = entry.effect
    tag = getattr(effect, "effect_type", "")
    if tag == "Damage":
        body = f"{effect.amount} dmg -> {effect.target}"
    elif tag == "Draw":
        body = f"Draw {effect.count}"
    elif tag == "GainLife":
        body = f"+{effect.amount} life"
    elif tag == "Destroy":
        body = f"Destroy {effect.target}"
    elif tag == "Exile":
        body = f"Exile {effect.target}"
    elif tag == "Counter":
        body = f"Counter {effect.target}"
    elif tag == "Pump":
        body = f"{effect.target} {effect.power:+d}/{effect.toughness:+d}"
    elif tag == "AddMana":
        amt = effect.amount
        body = f"+{amt}x {effect.mana}" if amt > 1 else f"+{effect.mana}"
    elif tag == "PutCounters":
        body = f"+{effect.count} {effect.counter_type}"
    elif tag == "Bounce":
        body = f"Bounce {effect.target}"
    elif tag == "ReturnFromGraveyard":
        body = f"Return {effect.target_filter} (GY)"
    elif tag == "CreateToken":
        spec = effect.spec
        dims = (
            f"{spec.power}/{spec.toughness} " if spec.power is not None and spec.toughness is not None else ""
        )
        mods = [
            word
            for flag, word in (
                (getattr(spec, "not_legendary", False), "nonlegendary"),
                (getattr(spec, "tapped", False), "tapped"),
                (getattr(spec, "attacking", False), "attacking"),
            )
            if flag
        ]
        mod_prefix = f"{' '.join(mods)} " if mods else ""
        if getattr(spec, "copy_of_self", False):
            desc = f"{dims}copy of self".strip()
        else:
            name = (spec.name or "").strip()
            spec_types = [t for t in (getattr(spec, "types", ()) or ()) if t]
            has_non_creature_type = any(t.casefold() != "creature" for t in spec_types)
            extra_types = [
                t
                for t in spec_types
                if not (t.casefold() == "creature" and dims and not has_non_creature_type)
                and t.casefold() not in name.casefold()
            ]
            subs = [
                s for s in (getattr(spec, "subtypes", ()) or ()) if s and s.casefold() not in name.casefold()
            ]
            type_bits = " ".join([*extra_types, *([name] if name else []), *subs]).strip()
            desc = f"{dims}{type_bits}".strip() or "token"
        kw = f" ({', '.join(spec.keywords)})" if spec.keywords else ""
        per_opp = " per opponent" if getattr(spec, "per_opponent", False) else ""
        suffix = "" if desc.casefold().endswith("token") else " token"
        body = f"{spec.count}x {mod_prefix}{desc}{kw}{suffix}{per_opp}"
    else:
        body = _strip_reminder(str(getattr(effect, "text", "") or ""))
    if not body:
        return ""
    trig = str(entry.trigger or "").strip()
    skip_cast = (not is_permanent) and trig.casefold() == "cast"
    prefix = f"[{trig}] " if trig and trig.casefold() != "static" and not skip_cast else ""
    conds = [c.strip() for c in (entry.conditions or ()) if str(c).strip()]
    cond = f"if {', '.join(conds)}: " if conds else ""
    return f"{prefix}{cond}{body}"


def _format_mode(mode: Any, printed_mana: str, stays_default: bool) -> str:
    name = str(mode.name or "").strip()
    cost = mode.cost
    cost_parts = []
    if mode.cost_text and mode.cost_text.strip() != printed_mana:
        cost_parts.append(_strip_reminder(mode.cost_text))
    else:
        if cost.mana and cost.mana.strip() != printed_mana:
            cost_parts.append(cost.mana.strip())
        if cost.tap:
            cost_parts.append("{T}")
        if cost.life:
            cost_parts.append(f"Pay {cost.life} life")
        if cost.discard:
            cost_parts.append(f"Discard {cost.discard}")
        if cost.exile_from_graveyard:
            cost_parts.append(f"Exile {cost.exile_from_graveyard} from GY")
        if cost.sacrifice is not None:
            sac_target = getattr(cost.sacrifice, "target", "") or "permanent"
            sac_count = getattr(cost.sacrifice, "count", 1)
            sac_cond = str(getattr(cost.sacrifice, "condition", "") or "").strip()
            sac_phrase = f"Sac {sac_count} {sac_target}" if sac_count != 1 else f"Sac {sac_target}"
            if sac_cond:
                sac_phrase += f" ({sac_cond})"
            cost_parts.append(sac_phrase)
    cost_str = ", ".join(cost_parts)
    low_name = name.casefold()
    if low_name == "cast" and mode.stays_on_battlefield == stays_default:
        return f"Additional cost: {cost_str}" if cost_str else ""
    if low_name in ("alt", "alternate_cast", "cast_without_paying_mana_cost"):
        return f"Alt: {cost_str}" if cost_str else "Alt"
    return f"{name}: {cost_str}" if cost_str else name


def compact_facts_summary(card: dict[str, Any], record: CardFacts) -> str | None:
    """Return a concise single-line CardFacts summary when strictly shorter and valid."""
    if type(card) is not dict or type(record) is not CardFacts:
        return None
    if not single_face_source(card) or not card.get("oracle_text"):
        return None
    if (
        type(record.schema_version) is not int
        or record.schema_version != 1
        or record.confidence != "high"
        or _unknown(record)
    ):
        return None
    if record.relationships or record.limited or record.notes:
        return None
    norm = normalized_scryfall_card(card)
    mana_candidates = {card.get("mana_cost")}
    if norm is not None:
        mana_candidates.add(norm["mana_cost"])
    if (
        record.name != card.get("name")
        or record.type_line != card.get("type_line")
        or record.mana_cost not in mana_candidates
    ):
        return None
    if record.rules_hash not in candidate_fact_hashes(card):
        return None
    if card.get("loyalty") is not None and record.body.loyalty is None:
        return None

    try:
        source = source_text(card)
        if render(card, record, source, enabled=True, role="stable_deck") != source:
            return " ".join(str(card["oracle_text"]).split())
    except KeyError:
        pass

    from arenamcp.match_context import oracle_lines

    non_cast_modes = [m for m in record.modes if str(m.name or "").strip().casefold() != "cast"]
    if len(non_cast_modes) > 1 or (non_cast_modes and len(record.effects) > 1):
        return None
    if any(
        getattr(entry.effect, "effect_type", "")
        in ("PutCounters", "Draw", "GainLife", "Pump", "ReturnFromGraveyard")
        for entry in record.effects
    ):
        return None

    raw_line = " ".join(oracle_lines(card["oracle_text"]))
    parts: list[str] = []
    if record.body.keywords:
        parts.append(", ".join(record.body.keywords))
    stays_default = not any(w in record.type_line.split() for w in ("Instant", "Sorcery"))
    for mode in record.modes:
        formatted_mode = _format_mode(mode, record.mana_cost, stays_default)
        if formatted_mode:
            parts.append(formatted_mode)
    if record.mana:
        mana_strs = [f"{m.mana} ({m.condition})" if m.condition else m.mana for m in record.mana if m.mana]
        if mana_strs:
            parts.append("Mana: " + "/".join(mana_strs))
    for entry in record.effects:
        text = _format_effect(entry, is_permanent=stays_default)
        if text:
            parts.append(text)

    summary = " ".join("; ".join(parts).split())
    if not summary or len(summary) >= len(raw_line):
        return None
    return summary


def compact_history_rules(catalog, *, warm: bool = False):
    """Quote verified CardFacts summaries by print ID for non-option deck cards."""
    from arenamcp.card_facts.client import facts_for, warm_memory_from_disk
    from arenamcp.card_facts.schema import card_facts_from_dict

    query_cards: list[dict[str, Any]] = []
    alt_hashes: list[str] = []
    for card in catalog.values():
        if not isinstance(card, dict) or not single_face_source(card) or not card.get("oracle_text"):
            continue
        query_cards.append(card)
        norm = normalized_scryfall_card(card)
        if norm is not None:
            query_cards.append(norm)
        if warm:
            alt_hashes.extend(candidate_fact_hashes(card))
    if not query_cards:
        return {}
    try:
        if warm:
            warm_memory_from_disk(query_cards, alt_hashes=alt_hashes)
        records = facts_for(query_cards, memory_only=True)
    except (AttributeError, OSError, TypeError, ValueError):
        return {}
    if not records:
        return {}
    compact = {}
    for gid, card in catalog.items():
        if not isinstance(card, dict) or not single_face_source(card) or not card.get("oracle_text"):
            continue
        raw = None
        for digest in candidate_fact_hashes(card):
            raw = records.get(digest)
            if raw:
                break
        if not raw:
            continue
        try:
            record = card_facts_from_dict(raw)
            summary = compact_facts_summary(card, record)
            if summary:
                compact[gid] = summary
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return compact
