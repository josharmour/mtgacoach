"""Deck-specific strategic knowledge derived from supplied card rules.

The playbook is model analysis, not a rules engine. Validate its card references
and rule references, retain its conditions, and let live legality/state override it.
There are deliberately no card-name-specific strategic policies here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass

PLAYBOOK_VERSION = 1

DECK_ANALYSIS_PROMPT = """Analyze every card in the supplied Magic deck using its supplied Oracle text, costs, types and faces. Build a reusable playbook for both coaching and autoplay, not a generic archetype summary. Explicitly analyze each designated commander; a legendary card is not necessarily the commander. If there is no commander, analyze the deck's engines normally.

Identify how resources become a win: setup, repeatable engines, payoffs, protection, interaction and recovery. Consider every supplied card, including lands. Record concise roles for the 5-10 cards that materially determine the plan; do not spend the output budget restating every basic land or filler card. Explain the actual mechanisms, not just pairs of card names. Reserve the first mechanism(s) for the designated commander(s), including the commander card ID in cards; then cover the other key engines. For each mechanism list EVERY involved card by its supplied card ID. The application attaches those cards' original rules directly; never retype or invent quotations. Only reference supplied card IDs. Missing rules are unknown; do not invent abilities.

Study trigger event, controller/owner, destination, token/nontoken restrictions, optionality, timing, once-per-turn limits, activation and additional costs, mana spending restrictions, summoning sickness and commander tax. Casting is different from entering; tutoring/returning to hand is different from putting onto the battlefield. Calculate resource changes after costs, sacrifices and losses. Do not assume a combo wins without sufficient mana, bodies, damage and timing.

COMMANDER ACCESS RULE: A designated commander that dies can be moved from the graveyard to the command zone at the next state-based-action check, then cast again for its printed cost plus {2} per previous command-zone cast (subject to legal timing and other costs/restrictions). Choosing that recovery does NOT permanently lose access to the original or its future entry triggers. Tokens that leave the battlefield cease to exist and cannot be recast. Recovery costs mana/tempo and loses counters and attachments, so it is not automatically profitable.

RESOURCE CONSERVATION EXAMPLE (apply this bookkeeping, not a fixed strategy): Starting creatures [commander A, body B, body C]. If B alone dies, survivors are [A,C]: 2 bodies. If A alone dies, survivors are [B,C]: 2 bodies. Recovering/casting A then gives [new A,B,C]: 3 bodies BEFORE any new cast/entry effects. Add or subtract ONLY what the supplied Oracle effects actually do. Neither B nor C disappeared in the A-dies branch. Still price the mana, lost counters/ongoing effects, timing, and alternative plays before preferring either line.

For each commander compare equal immediate outcomes from ONE shared starting resource state (for example, either of two bodies can be spent to prevent the same damage; BOTH branches must pay that one-body cost). Do not compare losing the commander against losing nothing. Build both branches explicitly in resource_comparison: keeping it while spending a substitute, versus spending it and recovering/recasting it. Account for what survives, what a replay adds, what is permanently lost, colored mana, increasing commander tax and tempo. Explicitly count the resulting bodies and usable resource production AFTER each line, including new triggers and which survivors can pay the recovery cost. Do not describe a recoverable death as forfeiting future regeneration. Use variables/formulas for unknown future resource counts. An increasing cost does not by itself make a replay bad: express actual affordability and payoff, never invent cutoffs such as "only at low tax" or "only below N previous casts". A static ability is not a per-turn growth trigger; without another event its output stays constant. A necessary-but-missing recovery resource invalidates reuse, whereas a resource supplied by the surviving board supports it. Then explain when to deploy, preserve, trade, sacrifice, replay or recur it, IF its rules and this deck support those lines. Compare unique ongoing value with repeatable cast/enter/death value. Commander and token copies may share combat stats but have different recovery value. Resources created by a new trigger are ADDED to unaffected surviving resources; never silently erase surviving tokens or conflate old copies with new copies. Compute mana output as number of eligible untapped producers times their actual per-producer yield, not all creatures of a counted type unless they actually share that mana ability. An ETB trigger is spent once it resolves: merely keeping its source does not repeat that trigger. Distinguish the original's CURRENT ongoing abilities from the future value of replaying it, and compare the abilities of copies independently. Neither 'always preserve the commander' nor 'always sacrifice the commander' is a policy. Explain conditions and opportunity costs. Other decks may need to preserve an engine or protect counters/equipment instead. Derive all such priorities from this deck's rules.

Return ONLY JSON with this shape. Use 3-6 meaningful mechanisms (fewer for a simple deck) and 3-6 decision rules. Keep the entire output under 2500 tokens, prioritizing game-changing interactions and their conditions over card summaries. Internal analysis is not the spoken summary.
{
  "archetype": "specific deck identity",
  "primary_plan": "how this deck actually wins, with prerequisites",
  "backup_plan": "alternative when its main engine is disrupted",
  "card_roles": {"123": "role and important limitation for card 123"},
  "commanders": [{"card": 123, "deployment": "when and why", "ongoing_value": "what the original still provides after its entry triggers have resolved, compared with copies", "replay_value": "what a new cast/entry can add, and what death/zone changes lose", "resource_comparison": {"starting_resources": "one shared starting state, using variables where necessary", "preserve_original": "spend a substitute to achieve the same immediate effect: subtract it and count remaining resources", "spend_and_recover": "spend original for that same effect, then pay recovery costs and resolve actual triggers: count resulting resources", "decision_test": "compare both results; require surviving resources to pay recovery, and price timing/opportunity cost"}, "preserve_or_reuse": "conditional preservation/recovery policy", "constraints": "costs, timing and exceptions"}],
  "mechanisms": [{"id": "engine_1", "cards": [123, 456], "effect": "the interaction and resource payoff", "requires": ["prerequisites, costs and timing"], "avoid": ["tempting misuse or condition that makes it bad"]}],
  "decision_rules": [{"id": "rule_1", "decisions": ["combat"], "mechanisms": ["engine_1"], "baseline": "the usual heuristic", "when": "deck-specific conditions that overturn it", "prefer": "the better line and payoff", "unless": "exceptions, cost or risk that restores the baseline"}],
  "phases": {"early": "setup priorities", "mid": "development and interaction", "late": "conversion to a win", "recovery": "rebuild after disruption"},
  "spoken_summary": "2-3 natural sentences summarizing this same plan"
}
Identify the deck's game-changing exceptions to normal heuristics in decision_rules. Link each exception to supported mechanism IDs; do not invent an exception for every card. Allowed decisions: combat, sacrifice, discard, development, mana, targeting, tutor, commander_zone, mulligan, all. Include resource/recovery tradeoffs where this deck supports them. The rule must state WHEN it is better and WHEN NOT; 'tokens are disposable' and 'protect the commander' are only baselines, never universal conclusions.

Before returning, check that every designated commander has a policy AND a supported mechanism, every game-changing card has been considered, every cited rule exists, and no line attributes a cast trigger to a non-cast event. Do not assume named cards work as remembered when the provided rules say otherwise."""


DECK_DISCOVERY_PROMPT = DECK_ANALYSIS_PROMPT.split("Return ONLY JSON", 1)[0] + (
    "Write concise analytical notes, not JSON. Identify source card IDs, 3-6 mechanisms, "
    "key game-changing cards and conditional exceptions. Explicitly compare BOTH alternatives "
    "for each commander; list the remaining original and old copies separately from new bodies. "
    "State when to preserve and when to spend/recover the resource. Keep under 1800 tokens."
)


def commander_ids(state: dict) -> list[int]:
    """Resolve designated card IDs even after the commander changes zones."""
    explicit = state.get("commander_grp_ids") or []
    if explicit:
        return sorted({gid for gid in explicit if isinstance(gid, int) and gid > 4})
    player = next((p for p in state.get("players", []) if p.get("is_local")), {})
    seat = state.get("local_seat_id") or player.get("seat_id")
    instances = set(player.get("commander_ids") or [])
    return sorted(
        {
            card.get("base_grp_id") or card["grp_id"]
            for zone in ("command", "battlefield", "hand", "graveyard", "exile", "stack")
            for card in state.get(zone) or []
            if card.get("owner_seat_id") == seat
            and (zone == "command" or card.get("instance_id") in instances)
            and card.get("grp_id", 0) > 4
            and not card.get("is_token")
            and "token" not in str(card.get("object_kind", "")).lower()
        }
    )


def deck_identity(state: dict) -> str:
    """Stable across draws/zone changes, different for deck or commander swaps."""
    deck = Counter(gid for gid in state.get("deck_cards") or [] if isinstance(gid, int) and gid > 4)
    if not deck:
        return ""
    data = [PLAYBOOK_VERSION, sorted(deck.items()), commander_ids(state)]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def oracle_rules(card: dict) -> list[str]:
    rules = [line.strip() for line in (card.get("oracle_text") or "").splitlines() if line.strip()]
    for face in card.get("related_faces") or []:
        for line in (face.get("oracle_text") or "").splitlines():
            if line.strip():
                rules.append(
                    f"Other face {face.get('name', '?')} ({face.get('mana_cost', '')}): {line.strip()}"
                )
    if card.get("type_line"):
        stats = (
            f" {card['power']}/{card['toughness']}"
            if card.get("power") not in (None, "") and card.get("toughness") not in (None, "")
            else ""
        )
        rules.append(f"Printed characteristics: {card.get('mana_cost', '')} {card['type_line']}{stats}.")
    return rules


def analysis_reference(state: dict, catalog: dict[int, dict]) -> str:
    """Full card facts with stable rule handles; the model need not copy text."""
    counts = Counter(state.get("deck_cards") or [])
    commanders = commander_ids(state)
    lines = ["COMPLETE DECK ORACLE REFERENCE (printed facts, not a current board):"]
    for gid, card in sorted(catalog.items(), key=lambda item: (item[0] not in commanders, item[0])):
        designation = " DESIGNATED COMMANDER" if gid in commanders else ""
        lines.append(
            f"{counts.get(gid, 1)}x {card.get('name', 'Unknown')} [grp:{gid}]{designation} | "
            f"{card.get('mana_cost', '')} | {card.get('type_line', '')} | "
            f"printed {card.get('power', '?')}/{card.get('toughness', '?')}"
        )
        rules = oracle_rules(card)
        lines.extend(f"  RULE {gid}:{number}: {text}" for number, text in enumerate(rules, 1))
        if not rules:
            lines.append("  Rules unavailable; do not invent abilities.")
    return "\n".join(lines)


def playbook_response_format(catalog: dict[int, dict], commanders: list[int]) -> dict:
    """Constrain structure at generation; code still verifies references/coverage."""

    def text(limit=600):
        return {"type": "string", "minLength": 1, "maxLength": limit}

    def obj(properties, required=None):
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False,
        }

    def array(item, minimum=1, maximum=6):
        return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}

    card = {"type": "integer", "enum": sorted(catalog)}
    mechanism_id = {"type": "string", "enum": [f"engine_{i}" for i in range(1, 7)]}
    roles = obj({str(gid): text(240) for gid in catalog}, [str(gid) for gid in commanders])
    roles["minProperties"] = 1
    roles["maxProperties"] = 10
    schema = obj(
        {
            "archetype": text(160),
            "primary_plan": text(),
            "backup_plan": text(),
            "card_roles": roles,
            "commanders": array(
                obj(
                    {
                        "card": {"type": "integer", "enum": commanders or [0]},
                        "deployment": text(),
                        "ongoing_value": text(),
                        "replay_value": text(),
                        "resource_comparison": obj(
                            {
                                field: text(600)
                                for field in (
                                    "starting_resources",
                                    "preserve_original",
                                    "spend_and_recover",
                                    "decision_test",
                                )
                            }
                        ),
                        "preserve_or_reuse": text(1000),
                        "constraints": text(),
                    }
                ),
                len(commanders),
                len(commanders),
            ),
            "mechanisms": array(
                obj(
                    {
                        "id": mechanism_id,
                        "cards": array(card),
                        "effect": text(),
                        "requires": array(text(240), maximum=4),
                        "avoid": array(text(240), maximum=4),
                    }
                )
            ),
            "decision_rules": array(
                obj(
                    {
                        "id": text(80),
                        "decisions": array({"type": "string", "enum": sorted(DECISION_KINDS)}),
                        "mechanisms": array(mechanism_id),
                        "baseline": text(200),
                        "when": text(400),
                        "prefer": text(600),
                        "unless": text(400),
                    }
                ),
                minimum=0,
            ),
            "phases": obj({phase: text(320) for phase in ("early", "mid", "late", "recovery")}),
            "spoken_summary": text(500),
        }
    )
    return {"type": "json_schema", "json_schema": {"name": "deck_playbook", "strict": True, "schema": schema}}


@dataclass
class DeckPlaybook:
    data: dict
    catalog: dict[int, dict]
    commanders: list[int]
    identity: str = ""

    @classmethod
    def parse(cls, response: str, catalog: dict[int, dict], commanders: list[int], identity=""):
        """Reject unsupported/missing references instead of blessing free prose.

        Resolving rule handles establishes provenance, not proof that the model's
        strategic inference is correct. Keep the cited rules in the rendering.
        """
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Deck analysis must be an object")

        def require_text(value):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Deck analysis contains an empty/non-text field")

        for key in ("archetype", "primary_plan", "backup_plan", "spoken_summary"):
            require_text(data.get(key))
        roles = data.get("card_roles")
        if not isinstance(roles, dict):
            raise ValueError("Missing card_roles object")
        allowed = {str(gid) for gid in catalog}
        required = {str(gid) for gid in commanders}
        if not roles or not set(roles) <= allowed or not required <= set(roles):
            raise ValueError(
                f"card_roles must name supplied cards and include each commander; "
                f"missing commanders={sorted(required - set(roles))}, unknown={sorted(set(roles) - allowed)}"
            )
        for value in roles.values():
            require_text(value)
        phases = data.get("phases")
        if not isinstance(phases, dict):
            raise ValueError("Missing phase/recovery plan")
        for key in ("early", "mid", "late", "recovery"):
            require_text(phases.get(key))
        policies = data.get("commanders")
        if not isinstance(policies, list) or len(policies) != len(commanders):
            raise ValueError("Missing designated commander policy")
        if {p.get("card") for p in policies if isinstance(p, dict)} != set(commanders):
            raise ValueError("Commander policy refers to the wrong card")
        for policy in policies:
            for key in (
                "deployment",
                "ongoing_value",
                "replay_value",
                "preserve_or_reuse",
                "constraints",
            ):
                require_text(policy.get(key))
            comparison = policy.get("resource_comparison")
            if not isinstance(comparison, dict):
                raise ValueError("Missing commander resource comparison")
            for key in ("starting_resources", "preserve_original", "spend_and_recover", "decision_test"):
                require_text(comparison.get(key))
        mechanisms = data.get("mechanisms")
        if not isinstance(mechanisms, list) or not mechanisms:
            raise ValueError("Missing deck mechanisms")
        covered = set()
        mechanism_ids = set()
        for mechanism in mechanisms:
            if not isinstance(mechanism, dict):
                raise ValueError("Invalid mechanism")
            require_text(mechanism.get("id"))
            if mechanism["id"] in mechanism_ids:
                raise ValueError("Duplicate mechanism ID")
            mechanism_ids.add(mechanism["id"])
            cards = mechanism.get("cards")
            if not isinstance(cards, list) or not cards or any(gid not in catalog for gid in cards):
                raise ValueError("Mechanism references a card outside this deck")
            require_text(mechanism.get("effect"))
            for key in ("requires", "avoid"):
                if not isinstance(mechanism.get(key), list) or not mechanism[key]:
                    raise ValueError("Mechanism lacks conditions or limitations")
                for value in mechanism[key]:
                    require_text(value)
            evidence = mechanism.get("evidence")
            if evidence is None:
                # Copy trusted source facts ourselves. Asking the model to
                # reproduce the same card list as citations added failures,
                # not evidence of understanding. The audit checks inference.
                evidence = [
                    {"card": gid, "rule": number}
                    for gid in dict.fromkeys(cards)
                    for number in range(1, len(oracle_rules(catalog[gid])) + 1)
                ]
                mechanism["evidence"] = evidence
            if not isinstance(evidence, list) or not evidence:
                raise ValueError("Mechanism lacks Oracle evidence")
            cited = set()
            for entry in evidence:
                if not isinstance(entry, dict) or entry.get("card") not in cards:
                    raise ValueError("Evidence refers to a different card")
                gid = entry["card"]
                rule = entry.get("rule")
                if set(entry) != {"card", "rule"} or not isinstance(rule, int) or isinstance(rule, bool):
                    raise ValueError(f"Evidence for card {gid} needs a numeric rule reference")
                if not 1 <= rule <= len(oracle_rules(catalog[gid])):
                    raise ValueError(f"Oracle rule {gid}:{rule} was not supplied")
                cited.add(gid)
            if cited != set(cards):
                raise ValueError(
                    f"Mechanism {mechanism['id']} lacks Oracle evidence for card IDs {sorted(set(cards) - cited)}"
                )
            covered.update(cards)
        if not set(commanders) <= covered:
            raise ValueError(
                f"Commander card IDs {sorted(set(commanders) - covered)} must appear in a "
                "mechanism cards list; revise the commander engine before unrelated mechanisms"
            )
        rules = data.get("decision_rules")
        if not isinstance(rules, list):
            raise ValueError("Missing conditional decision rules")
        rule_ids = set()
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("Invalid decision rule")
            for key in ("id", "baseline", "when", "prefer", "unless"):
                require_text(rule.get(key))
            if rule["id"] in rule_ids:
                raise ValueError("Duplicate decision rule ID")
            rule_ids.add(rule["id"])
            links = rule.get("mechanisms")
            if not isinstance(links, list) or not links or any(link not in mechanism_ids for link in links):
                raise ValueError(
                    f"Decision rule {rule.get('id')} links to {links}; "
                    f"every link must name an included mechanism: {sorted(mechanism_ids)}"
                )
            kinds = rule.get("decisions")
            if not isinstance(kinds, list) or not kinds or any(kind not in DECISION_KINDS for kind in kinds):
                raise ValueError("Decision rule has an unknown decision scope")
        return cls(data, catalog, commanders, identity)

    @staticmethod
    def _rule_lines(rule: dict) -> list[str]:
        return [
            f"RULE {rule['id']} ({', '.join(rule['decisions'])}; supports: {', '.join(rule['mechanisms'])})",
            f"  Usual heuristic: {rule['baseline']}",
            f"  When: {rule['when']}",
            f"  Prefer instead: {rule['prefer']}",
            f"  Unless: {rule['unless']}",
        ]

    def decision_context(self, state: dict) -> str:
        """Keep the relevant learned exceptions close to the immediate choice."""
        context = state.get("decision_context") or {}
        request = " ".join(
            str(value)
            for value in (
                context.get("type"),
                context.get("context"),
                state.get("pending_decision"),
                state.get("_bridge_request_type"),
                (state.get("turn") or {}).get("phase"),
            )
        ).lower()
        kinds = {kind for needle, kind in _DECISION_HINTS if needle in request}
        if "combat" in kinds:
            # Trading a body changes the same resources as sacrifices and
            # recovery choices, though the actual trigger events still differ.
            kinds.update(("sacrifice", "commander_zone"))
        if context.get("commander_return"):
            kinds.add("commander_zone")
        rules = [
            rule
            for rule in self.data["decision_rules"]
            if not kinds or "all" in rule["decisions"] or kinds.intersection(rule["decisions"])
        ]
        lines = [
            "DECK DECISION RULES FOR THIS WINDOW (conditional, not automatic overrides):",
            f"Primary plan: {self.data['primary_plan']}",
            f"Backup plan: {self.data['backup_plan']}",
            "Check their conditions against the live state. Compare the strongest alternative; "
            "explain which rule applies or which constraint defeats it. Mechanical combat scores "
            "omit trigger/recovery value. Do not reduce these decisions to generic token/body value.",
        ]
        casts_by_card = state.get("commander_casts") or {}
        for gid in self.commanders:
            card = self.catalog[gid]
            casts = casts_by_card.get(gid, casts_by_card.get(str(gid)))
            tax = (
                f"{{{2 * casts}}} ({casts} prior command-zone casts)"
                if isinstance(casts, int) and not isinstance(casts, bool) and casts >= 0
                else "UNKNOWN; do not assume zero"
            )
            lines.append(
                f"Live commander recovery cost — {card.get('name', 'Unknown')} [grp:{gid}]: "
                f"printed {card.get('mana_cost') or 'UNKNOWN'} plus commander tax {tax}. "
                "Other cost modifiers and Arena's current payable options still apply."
            )
        for policy in self.data["commanders"]:
            lines.append(
                f"Commander policy [grp:{policy['card']}]: {policy['preserve_or_reuse']} "
                f"Constraints: {policy['constraints']}"
            )
        for rule in rules:
            lines.extend(self._rule_lines(rule))
        linked = {mid for rule in rules for mid in rule["mechanisms"]}
        for mechanism in self.data["mechanisms"]:
            if mechanism["id"] in linked:
                lines.append(
                    f"Mechanism {mechanism['id']}: {mechanism['effect']} "
                    f"Requires: {'; '.join(mechanism['requires'])}. "
                    f"Avoid: {'; '.join(mechanism['avoid'])}."
                )
        return "\n".join(lines)

    def render(self) -> str:
        def name(gid):
            return f"{self.catalog[gid].get('name', 'Unknown')} [grp:{gid}]"

        data = self.data
        lines = [
            f"DECK PLAYBOOK v{PLAYBOOK_VERSION}: {data['archetype']}",
            "Model-derived strategy; quoted Oracle text and live state govern its conditions.",
            f"Primary plan: {data['primary_plan']}",
            f"Backup plan: {data['backup_plan']}",
        ]
        for policy in data["commanders"]:
            lines += [
                f"COMMANDER {name(policy['card'])}",
                f"  Deploy: {policy['deployment']}",
                f"  Ongoing value after entry: {policy['ongoing_value']}",
                f"  Replay value / losses: {policy['replay_value']}",
                *(
                    f"  Resource comparison {key}: {value}"
                    for key, value in policy["resource_comparison"].items()
                ),
                f"  Preserve/reuse: {policy['preserve_or_reuse']}",
                f"  Constraints: {policy['constraints']}",
            ]
        for rule in data["decision_rules"]:
            lines.extend(self._rule_lines(rule))
        for mechanism in data["mechanisms"]:
            lines.append(f"MECHANISM {mechanism['id']}: {mechanism['effect']}")
            lines.extend(f"  Requires: {item}" for item in mechanism["requires"])
            lines.extend(f"  Avoid: {item}" for item in mechanism["avoid"])
            lines.extend(
                f"  Oracle — {name(e['card'])}, rule {e['rule']}: {oracle_rules(self.catalog[e['card']])[e['rule'] - 1]}"
                for e in mechanism["evidence"]
            )
        lines.extend(
            f"{phase.title()}: {data['phases'][phase]}" for phase in ("early", "mid", "late", "recovery")
        )
        lines.append("KEY CARD ROLES (conditional on the mechanisms and current state):")
        lines.extend(
            f"  {name(gid)}: {data['card_roles'][str(gid)]}"
            for gid in sorted(int(gid) for gid in data["card_roles"])
        )
        return "\n".join(lines)

    def export(self) -> dict:
        return {
            "version": PLAYBOOK_VERSION,
            "identity": self.identity,
            "data": self.data,
            "catalog": self.catalog,
            "commanders": self.commanders,
        }

    @classmethod
    def restore(cls, saved: dict, state: dict):
        if saved.get("version") != PLAYBOOK_VERSION or saved.get("identity") != deck_identity(state):
            raise ValueError("Deck playbook version or deck/commander identity changed")
        catalog = {int(gid): card for gid, card in saved["catalog"].items()}
        expected = set(state.get("deck_cards") or []) | set(commander_ids(state))
        if not expected or set(catalog) != expected:
            raise ValueError("Saved playbook catalog differs from the current deck")
        return cls.parse(json.dumps(saved["data"]), catalog, commander_ids(state), saved["identity"])


DECISION_KINDS = {
    "combat",
    "sacrifice",
    "discard",
    "development",
    "mana",
    "targeting",
    "tutor",
    "commander_zone",
    "mulligan",
    "all",
}
_DECISION_HINTS = (
    ("combat", "combat"),
    ("attack", "combat"),
    ("block", "combat"),
    ("sacrifice", "sacrifice"),
    ("discard", "discard"),
    ("main", "development"),
    ("actionsavailable", "development"),
    ("mana", "mana"),
    ("pay", "mana"),
    ("target", "targeting"),
    ("search", "tutor"),
    ("mulligan", "mulligan"),
)
