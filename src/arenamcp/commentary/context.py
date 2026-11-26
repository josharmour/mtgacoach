"""Select useful whole facts within the existing commentary request budget."""

import json
import re


def generation_format(reply=False):
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
                        "maxItems": 1 if reply else 2,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["role", "text", "refs"],
                            "properties": {
                                "role": {
                                    "type": "string",
                                    "enum": ["color_analyst"] if reply else ["play_by_play", "color_analyst"],
                                },
                                "text": {
                                    "description": "One complete broadcast sentence. Lead with the takeaway and stop. Aim for 60-95 characters.",
                                    "type": "string",
                                    "maxLength": 220,
                                },
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


def live_speech_format(roles):
    """The live author's entire deliverable is who speaks and what they say."""
    shape = generation_format()
    turns = shape["json_schema"]["schema"]["properties"]["turns"]
    turns["maxItems"] = len(roles)
    line = turns["items"]
    del line["properties"]["refs"]
    line["required"] = ["role", "text"]
    line["properties"]["role"]["enum"] = list(roles)
    line["properties"]["text"] = {"type": "string", "minLength": 1, "maxLength": 600}
    return shape


def request_size(system, context):
    return (
        len(system.encode())
        + len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode())
        + 256
    )


def compact_context(system, context, *, reply_system=None, pair_for=None):
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
    # Reserve conversational memory BEFORE filling the request with card rules.
    # A separate analyst ledger survives many intervening play-by-play calls.
    result["recent_analysis"] = []
    for line in reversed(context.get("recent_analysis", ())[-8:]):
        if not isinstance(line, str):
            continue
        result["recent_analysis"].insert(0, line)
        if len(json.dumps(result["recent_analysis"]).encode()) > 900:
            result["recent_analysis"].pop(0)
            break
    result["already_called"] = []
    for text in reversed(context.get("already_called", ())[-2:]):
        if (
            isinstance(text, str)
            and len(text.encode()) <= 240
            and request_size(system, result) + len(text.encode()) + 8 <= limit
        ):
            result["already_called"].insert(0, text)
    facts = context["facts"]
    new = list(context.get("new_events", ()))
    # Preserve the meaningful play in a burst even when a routine land came last.
    weights = {
        "game_end": 20,
        "outcome": 18,
        "attack_declared": 12,
        "life_change": 8,
        "cast": 10,
        "resolved": 9,
        "permanent_entered": 9,
        "counter": 15,
        "spell_cast": 10,
        "spell_resolved": 7,
        "hand_changed": 6,
    }
    ranked = sorted(new, key=lambda k: weights.get(facts.get(k, {}).get("kind"), 0), reverse=True)
    causal_kinds = {"cast", "permanent_entered", "resolved", "spell_cast", "spell_resolved"}
    needs_causal = any(
        facts.get(k, {}).get("kind") == "outcome"
        and not (facts.get(k, {}).get("data") or {}).get("source_grp_id")
        and not (facts.get(k, {}).get("data") or {}).get("source_id")
        for k in ranked[:2]
    )
    causal_events: list[str] = []
    if needs_causal:
        causal_key = next(
            (k for k in ranked[2:] if facts.get(k, {}).get("kind") in causal_kinds),
            None,
        )
        if causal_key is None:
            ranked_top = set(ranked[:2])
            causal_key = next(
                (
                    ref
                    for ref in reversed(context.get("play_sequence", ()))
                    if ref not in ranked_top and ref in facts and facts[ref].get("kind") in causal_kinds
                ),
                None,
            )
        if causal_key is not None:
            causal_events = [causal_key]
    order = ranked[:2] + causal_events
    order += ["state:turn", "state:local_seat"] + [k for k in facts if k.startswith("state:player:")]
    related = []
    for key in ranked[:2] + causal_events:
        data = facts.get(key, {}).get("data", {})
        gid = data.get("grp_id") or data.get("source_grp_id")
        if gid:
            related.append(f"card:{gid}")
    # Pair individual public permanents with their rules. Dense boards often
    # cannot fit as a whole. Prefer the just-played card and give both sides a
    # chance to supply the interaction before filling the budget with aggregates.
    permanents = [k for k in facts if k.startswith("state:permanent:")]
    focal = [k for k in permanents if f"card:{facts[k].get('grp_id')}" in related]
    actor = facts.get(ranked[0], {}).get("seat") if ranked else None
    # Highlight related public evidence without prescribing the analyst's answer.
    # The same open brief applies to casts, combat, trades and other new plays.
    if ranked:
        result["editorial_question"] = {
            "question": "What changed for either player, and which cards or choices make that matter?",
            "check_these_refs": [],
        }
    # Connect a new payoff to a public enabler, or an answer to the opposing
    # engine it might interrupt. A matching card is not an observed target.
    entry_suppression = any(
        "don't cause abilities"
        in (facts.get(f"card:{facts[k].get('grp_id')}", {}).get("rules") or "").lower().replace("’", "'")
        for k in permanents
    )
    for focal_key in focal[:1]:
        focal_card = facts[focal_key]
        rule = facts.get(f"card:{focal_card.get('grp_id')}", {}).get("rules") or ""
        for key in permanents:
            if key == focal_key:
                continue
            partner = facts[key]
            partner_rule = facts.get(f"card:{partner.get('grp_id')}", {}).get("rules") or ""
            related_pair = (
                not entry_suppression
                and actor in (1, 2)
                and partner.get("controller_seat_id") == actor
                and "if you gained life this turn" in rule.lower()
                and re.search(
                    r"whenever (?:another|a) creature you control enters, you gain 1 life\.",
                    partner_rule,
                    re.I,
                )
            ) or (
                partner.get("controller_seat_id") != actor
                and "exile target nonland permanent" in rule.lower()
                and "don't cause abilities to trigger" in partner_rule.lower()
            )
            if related_pair:
                refs = [focal_key, f"card:{focal_card['grp_id']}", key, f"card:{partner['grp_id']}"]
                order += refs
                result["editorial_question"]["check_these_refs"] = refs
                event = facts[ranked[0]]
                name = (event.get("data") or {}).get("name")
                who = event.get("player")
                if (
                    event.get("kind") == "cast"
                    and name
                    and who
                    and actor in (1, 2)
                    and set(result.get("allowed_roles", ())) == {"play_by_play", "color_analyst"}
                    and event["data"].get("grp_id") == focal_card.get("grp_id")
                ):
                    result["partner_call"] = {
                        "role": "play_by_play",
                        "text": f"{who} {'cast' if who == 'You' else 'casts'} {name}.",
                        "refs": [ranked[0]],
                    }
                break
    if ranked and pair_for is not None and not result["editorial_question"]["check_these_refs"]:
        # Give the model the event, current permanent and both complete printed rules.
        # Their compatibility focuses attention without asserting the ability triggered.
        pair = pair_for(ranked[0])
        if pair and all(key in facts for key in pair):
            refs = [ranked[0], *pair]
            result["editorial_question"]["check_these_refs"] = refs
            order += refs
    if ranked and set(result.get("allowed_roles", ())) == {"play_by_play", "color_analyst"}:
        ref = ranked[0]
        event = facts[ref]
        heard = context.get("heard_cast_calls", {}).get(ref)
        who = event.get("player")
        data = event.get("data") or {}
        name = data.get("name")
        live_call = f"{who} {'cast' if who == 'You' else 'casts'} {name}"
        if (
            heard
            and event.get("kind") == "cast"
            and event.get("source") == "observed_GRE"
            and event.get("seat") in (1, 2)
            and who
            and name
            and type(data.get("grp_id")) is int
            and data["grp_id"] > 0
            and heard in {live_call + ".", live_call + "!", f"{who} cast {name}."}
        ):
            # A completed, event-bound public call can hand off even while a
            # spell is still on the stack. No board interaction is presumed.
            result["partner_call"] = {"role": "play_by_play", "text": heard, "refs": [ref]}
            result["partner_call_heard"] = True
        elif event.get("kind") in {"attack_declared", "life_change", "outcome"} and event.get("source") in {
            "observed_GRE",
            "public_GRE_annotation",
        }:
            heard = context.get("heard_public_calls", {}).get(ref)
            if isinstance(heard, str) and heard and len(heard.encode()) <= 240:
                result["partner_call"] = {"role": "play_by_play", "text": heard, "refs": [ref]}
                result["partner_call_heard"] = True
    # Carry reviewed relationship possibilities and threat descriptions while
    # the originating play is present in the new event batch.
    event_kinds = {
        "cast": "spell_cast",
        "resolved": "permanent_entered",
        "permanent_entered": "permanent_entered",
        "land_drop": "permanent_entered",
    }
    event_prints = {
        (
            event_kinds.get(facts[k].get("kind")),
            facts[k].get("seat"),
            (facts[k].get("data") or {}).get("instance_id"),
        )
        for k in ranked[:2]
    }
    annotations = [
        k
        for k, f in facts.items()
        if k.startswith("state:annotation:")
        and (f.get("event_kind"), f.get("controller"), f.get("instance_id")) in event_prints
    ]
    annotations.sort(key=lambda k: facts[k].get("kind") != "card_relationship")
    # Reviewed links are the strongest evidence for an entry. Reserve room
    # for one while allowing the focal rules and established engines to fit.
    # Bring one public deck hypothesis to the analyst when this play involves
    # its evidence. An unrelated action does not reopen the same talking point.
    reads = {}
    for k, fact in facts.items():
        if not k.startswith("state:opponent_read:"):
            continue
        seen_ids = {sighting["grp_id"] for sighting in fact["evidence"]}
        references = [
            event_key
            for event_key in ranked[:2]
            if facts[event_key].get("seat") == fact["opponent_seat"]
            and facts[event_key].get("kind") in event_kinds
            and (facts[event_key].get("data") or {}).get("grp_id") in seen_ids
        ]
        if references:
            reads[k] = references
            break
    preamble = system
    if reply_system and result.get("partner_call"):
        preamble = reply_system
    handoff = (
        result.get("editorial_question", {}).get("check_these_refs", ()) if result.get("partner_call") else ()
    )
    order = ranked[:2] + causal_events + annotations + list(reads) + list(handoff) + order[len(ranked[:2]) :]
    sequence = [ref for ref in context.get("play_sequence", ()) if ref in facts][-4:]
    if sequence:
        result["play_sequence"] = list(sequence)
        order += sequence
    order += [key for key in facts if key.startswith("private:hand:")]
    order += ["state:revealed_opposing_hand", "private:library"]
    remaining = [key for key in facts if key.startswith("private:remaining:")]
    order += remaining[:2]
    order += related
    order += focal
    others = [k for k in permanents if k not in focal]
    sides = [
        [k for k in others if (facts[k].get("controller_seat_id") == actor) == own] for own in (False, True)
    ]
    for i in range(max(map(len, sides), default=0)):
        for side in sides:
            if i < len(side):
                key = side[i]
                order += [key, f"card:{facts[key].get('grp_id')}"]
    for key, fact in facts.items():
        if key.startswith("private:hand:"):
            related.extend(f"card:{c['grp_id']}" for c in fact.get("cards", ()) if c.get("grp_id"))
    order += ["state:game_read", "state:battlefield", "private:deck_plan", "private:synergies"] + related
    order += [
        f"card:{c['grp_id']}" for c in facts.get("state:board", {}).get("objects", []) if c.get("grp_id")
    ]
    order += ["state:board"]
    order += [k for k in facts if k.startswith("private:hand:")]
    order += ranked[2:] + remaining[2:]
    order += [k for k in facts if k.startswith("card:")]
    for key in dict.fromkeys(order):
        if key not in facts:
            continue
        result["facts"][key] = facts[key]
        if key in new:
            result["new_events"].append(key)
        if request_size(preamble, result) > limit:
            result["facts"].pop(key)
            if key in result["new_events"]:
                result["new_events"].remove(key)
    if sequence:
        result["play_sequence"] = [ref for ref in sequence if ref in result["facts"]]
    for key, references in reads.items():
        if not any(ref in result["new_events"] for ref in references):
            result["facts"].pop(key, None)
    angle = result.get("editorial_question")
    if angle and (
        not set(angle["check_these_refs"]).issubset(result["facts"])
        or not ranked
        or ranked[0] not in result["new_events"]
    ):
        result["editorial_question"]["check_these_refs"] = []
        result.pop("partner_call_heard", None)
        if result.pop("partner_call", None) and preamble != system:
            # Without the handoff, generation uses the longer two-speaker prompt.
            return compact_context(system, context)
    for key in ("heard", "earlier_points", "in_flight"):
        result.setdefault(key, [])
        for item in reversed(context.get(key, ())[-2:]):
            result[key].insert(0, item)
            if request_size(preamble, result) > limit:
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
    from arenamcp.deck_strategy import clean_deck_prose

    summary = clean_deck_prose(book.data.get("spoken_summary"))
    from .overview_quality import speakable_overview

    if not speakable_overview(summary):
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


def isolate_overlength_lines(output, word_count):
    """Drop only overlong lines, never shorten claims or bypass the native audit.

    A valid partner can still be checked in full by the existing structure,
    reference, factual, freshness and semantic validators. Malformed structures
    remain untouched so native rejection is unchanged. No repair/model call.
    The passed counter is the native validator's exact spoken-word counter.
    """
    if (
        type(output) is not dict
        or set(output) != {"turns"}
        or type(output["turns"]) is not list
        or len(output["turns"]) > 2
        or any(
            type(turn) is not dict
            or set(turn) != {"role", "text", "refs"}
            or type(turn["role"]) is not str
            or turn["role"] not in {"play_by_play", "color_analyst"}
            or type(turn["text"]) is not str
            or type(turn["refs"]) is not list
            for turn in output["turns"]
        )
    ):
        return output, []
    kept, dropped = [], []
    for turn in output["turns"]:
        maximum = 28 if turn["role"] == "color_analyst" else 18
        if len(turn["text"]) > 320 or word_count(turn["text"]) > maximum:
            dropped.append(turn["role"])
        else:
            kept.append(turn)
    return {"turns": kept}, dropped


def audit_format(*, single_reply=False):
    fields = {
        "index": {"type": "integer"},
        "supported": {"type": "boolean"},
        "unsupported_claim": {"type": "string"},
        "adds_insight": {"type": "boolean"},
        "scope": {"type": "string", "enum": ["historical", "position", "snapshot"]},
    }
    item = {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "commentary_audit",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["checks"],
                "properties": {
                    "checks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1 if single_reply else 2,
                        "items": item,
                    }
                },
            },
        },
    }


def valid_verdict(result, count):
    if type(result) is not dict or set(result) != {"checks"}:
        return False
    checks = result["checks"]
    return (
        type(checks) is list
        and len(checks) == count
        and 1 <= count <= 2
        and all(
            type(c) is dict
            and set(c) == {"index", "supported", "unsupported_claim", "adds_insight", "scope"}
            and type(c["index"]) is int
            and c["index"] == i
            and type(c["supported"]) is bool
            and type(c["adds_insight"]) is bool
            and type(c["unsupported_claim"]) is str
            and isinstance(c["scope"], str)
            and c["scope"] in {"historical", "position", "snapshot"}
            for i, c in enumerate(checks)
        )
    )


def parse_completed_json(text):
    """Duplicate verdict fields are ambiguous even when the last value parses."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                from .budget import BudgetExceeded

                raise BudgetExceeded("invalid_json")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique)


def cast_entry_conflict(text, evidence):
    """A cast-only call cannot describe an entry. State/rules are not an entry event."""
    kinds = {v.get("kind") for k, v in evidence.items() if k.startswith("event:")}
    if kinds != {"cast"}:
        return False
    return bool(
        re.search(
            r"\b(?:drops?\b.{0,70}\binto play|(?:lands|hits|enters) (?:on )?the battlefield|slides?\b.{0,70}\bonto the table)\b",
            text,
            re.I,
        )
    )


def omits_on_hit_counter_cost(text, evidence):
    """Withhold damage-to-player removal shorthand for a counter-paid reflexive trigger.

    This veto covers the observed shorthand. The semantic audit still checks
    controller, timing, payment and target restrictions for the whole exchange.
    """
    # An analyst may contrast two cited attackers. Keep a claim explicitly
    # about a different card's printed, no-payment trigger in the semantic audit.
    for key, item in evidence.items():
        name = item.get("name")
        if not key.startswith("card:") or not isinstance(name, str) or not name:
            continue
        if (
            f"Whenever {name} deals combat damage to a player, you may destroy target artifact that player controls."
            in (item.get("rules") or "").splitlines()
        ):
            text = re.sub(
                "^" + re.escape(name) + r" can destroy an artifact on hit(?=[,.!]|$)", "", text, flags=re.I
            )
    if not re.search(r"\b(?:on (?:a )?hit|when (?:it|she|he) (?:hits|connects)|on contact)\b", text, re.I):
        return False
    if not re.search(
        r"\b(?:strip(?:s|ped)?|destroy(?:s|ed)?|remov(?:es?|ed)|kill(?:s|ed)?|blows? up)"
        r"\s+(?:an? |the |their |that |opposing )*(?:artifacts?|enchantments?)\b",
        text,
        re.I,
    ):
        return False
    # Never infer payment from words elsewhere in the sentence. A longer
    # explicitly conditional explanation can omit this ambiguous shorthand.
    return any(
        re.search(
            r"deals combat damage to a player, you may remove a counter from [^.]+\. "
            r"(?:When|If) you do, destroy target artifact or enchantment that player controls",
            item.get("rules") or "",
            re.I,
        )
        for key, item in evidence.items()
        if key.startswith("card:")
    )
