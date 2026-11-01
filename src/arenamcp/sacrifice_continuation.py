"""Bounded public death -> sacrifice -> Tergrid continuations (#280).

The tree preserves each player's victim, trigger-order and optional-transfer
choices. Its consumer can choose a policy; enumeration itself never guesses a
victim. Unsupported effects and exhausted bounds discard the entire projection.
No hidden cards, activations, ETBs or replacement effects are invented.
"""

import re
from dataclasses import dataclass
from itertools import permutations

from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.death_bodies import death_is_suppressed


class UnsupportedContinuation(Exception):
    pass


def raw(card):
    return card.get("_card") or card


def seat(card):
    c = raw(card)
    return c.get("controller_seat_id", c.get("owner_seat_id"))


def identity(card):
    return card["instance_id"]


def token(card):
    c = raw(card)
    return bool(
        c.get("is_token")
        or "token" in str(c.get("object_kind") or "").lower()
        or "token" in str(c.get("type_line") or "").lower()
    )


def creature(card):
    c = raw(card)
    types = c.get("card_types")
    return (
        any(str(t).lower().removeprefix("cardtype_") == "creature" for t in types)
        if types is not None
        else "creature" in str(c.get("type_line") or "").lower()
    )


def lines(card):
    c = raw(card)
    text = re.sub(r"<[^>]*>", "", str(c.get("oracle_text") or "")).lower().replace("’", "'")
    text = re.sub(r"\([^)]*\)", "", text)
    name = str(c.get("name") or "").lower()
    if name:
        text = text.replace(name, "~")
    return list(dict.fromkeys(" ".join(line.split()) for line in text.splitlines() if line.strip()))


_PACT = re.compile(
    r"whenever (?P<self>(?:~|this creature) or )?(?P<which>a|another) creature you control dies, "
    r"each (?:other player|opponent) sacrifices a creature\."
)
_TAKE = (
    "whenever an opponent sacrifices a nontoken permanent or discards a permanent card, "
    "you may put that card from a graveyard onto the battlefield under your control."
)
_KEYWORDS = {
    "flying",
    "reach",
    "vigilance",
    "haste",
    "trample",
    "deathtouch",
    "lifelink",
    "menace",
    "defender",
    "indestructible",
    "hexproof",
    "shroud",
}


def abilities(card):
    if raw(card).get("face_down") or raw(card).get("is_phased_out"):
        return (), 0
    pacts, takes = [], 0
    for line in lines(card):
        if m := _PACT.fullmatch(line):
            pacts.append(bool(m["self"]) or m["which"] == "a")
        elif line == _TAKE:
            takes += 1
    return tuple(pacts), takes


def relevant(battlefield):
    return any(
        re.search(
            r"\b(?:when|whenever) [^,\n]+(?:dies?|put into a graveyard)[^,\n]*,\s*[^.\n]*\bsacrific(?:e|es)\b",
            line,
        )
        for c in battlefield
        if not raw(c).get("is_phased_out") and not raw(c).get("face_down")
        for line in lines(c)
    )


def unsupported(battlefield):
    """Reasons the known board exceeds this continuation's explicitly bounded rules."""
    cards = [c for c in battlefield if not raw(c).get("is_phased_out") and not raw(c).get("face_down")]
    if not relevant(cards):
        return ()
    reasons = set()
    takes = any(abilities(c)[1] for c in cards)
    names = [
        str(raw(c).get("name"))
        for c in cards
        if "legendary" in str(raw(c).get("type_line")).lower()
        or any("legendary" in str(s).lower() for s in raw(c).get("supertypes") or [])
    ]
    if len(names) != len(set(names)):
        reasons.add("legendary state-based deaths within sacrifice continuation")
    if any(raw(c).get("face_down") and creature(c) for c in battlefield):
        reasons.add("face-down sacrifice victim characteristics")
    for c in cards:
        text = " ".join(lines(c))
        if raw(c).get("is_commander") or raw(c).get("isCommander"):
            reasons.add("commander graveyard/command-zone choices")
        if seat(c) is None:
            reasons.add("sacrifice source/victim controller unknown")
        if any(has_combat_keyword(c, k) for k in ("first strike", "double strike")):
            reasons.add("sacrifices between combat damage steps")
        if "additional combat" in text or c.get("_atk_extra"):
            reasons.add("sacrifice continuations across extra combats")
        if ("instead" in text and any(w in text for w in ("die", "graveyard", "sacrific"))) or (
            "can't" in text and "sacrific" in text
        ):
            reasons.add("death/sacrifice replacement or prevention")
        if raw(c).get("removed_abilities"):
            reasons.add("modified sacrifice-trigger abilities")
        if re.search(
            r"(?:creatures|permanents).*\b(?:get|have|enter|enters)\b", text
        ) and not death_is_suppressed([raw(c)]):
            reasons.add("continuous effects on transferred permanents")
        if "sacrifice" in text and ":" in text:
            reasons.add("activated sacrifice outlets")
        for line in lines(c):
            if _PACT.fullmatch(line) or line == _TAKE:
                continue
            if "die" in line or ("sacrific" in line and line.startswith(("when", "whenever"))):
                reasons.add("additional or conditional death/sacrifice triggers")
            if "sacrific" in line:
                reasons.add("sacrifices outside the modeled death-trigger clauses")
            if takes and re.search(r"\b(?:when|whenever|as)\b.*\benters?\b", line):
                reasons.add("enters effects after sacrifice transfers")
            if takes and creature(c) and not token(c):
                if not all(k.strip().rstrip(".") in _KEYWORDS for k in line.split(",")):
                    reasons.add("unmodeled abilities on a possible transferred creature")
        if takes and creature(c) and not token(c):
            original = raw(c)
            if (
                type(original.get("printed_power")) is not int
                or type(original.get("printed_toughness")) is not int
                or original.get("printed_toughness", 0) <= 0
                or original.get("is_copy")
                or "creature" not in str(original.get("type_line") or "").lower()
            ):
                reasons.add("printed characteristics unavailable for a possible sacrifice transfer")
    return tuple(sorted(reasons))


def model_reasons(model):
    if not relevant(model.battlefield):
        return ()
    reasons = list(unsupported(model.battlefield))
    if model.spells or model.our_stack_doomed or model.our_stack_bodies or model.has_pending_stack:
        reasons.append("sacrifice continuation with spell/stack transitions outside combat")
    if model.unknown_bodies:
        reasons.append("unknown creature characteristics in sacrifice choices")
    if model.commanders:
        reasons.append("commander graveyard/command-zone choices")
    from arenamcp.token_lifetime import dies_anyway

    if any(dies_anyway(c) for c in model.battlefield):
        reasons.append("delayed token sacrifices outside combat")
    return tuple(reasons)


@dataclass(frozen=True)
class Outcome:
    bodies: tuple
    sacrificed: tuple = ()
    transfers: tuple = ()  # (old instance, fresh instance, new controller)


@dataclass(frozen=True)
class Choice:
    controller: int | None
    label: str
    options: tuple = ()
    outcome: Outcome | None = None

    @property
    def branching(self):
        return len(self.options) > 1 or any(c.branching for c in self.options)

    def outcomes(self):
        if self.outcome is not None:
            return [self.outcome]
        return [o for choice in self.options for o in choice.outcomes()]

    def choose(self, score, local):
        """Public-state minimax at each actual controller's choice; stable ties."""
        if self.outcome is not None:
            return self.outcome
        results = [c.choose(score, local) for c in self.options]
        return (max if self.controller == local else min)(results, key=score)


@dataclass(frozen=True)
class Event:
    controller: int
    kind: str
    target: int  # player for sacrifice, graveyard identity for optional transfer


def fresh_capture(card, controller, new_id, turn):
    """Fresh printed creature, with no old counters, damage, grants or attachments."""
    from arenamcp.board_assessment import _body

    original = raw(card)
    fresh = {
        k: v
        for k, v in original.items()
        if k
        in (
            "name",
            "oracle_text",
            "type_line",
            "card_types",
            "subtypes",
            "colors",
            "owner_seat_id",
            "printed_power",
            "printed_toughness",
            "mana_cost",
            "mana_value",
            "grp_id",
        )
    }
    fresh.update(
        instance_id=new_id,
        controller_seat_id=controller,
        power=original["printed_power"],
        toughness=original["printed_toughness"],
        turn_entered_battlefield=turn,
        counters={},
        attachments=[],
        damage=0,
        is_tapped=False,
        is_attacking=False,
        is_blocking=False,
    )
    return _body(fresh, turn, {})


def resolve(battlefield, survivors, dead, *, active, other, turn, max_states=512):
    """Resolve all legal public choices after simultaneous ordinary combat deaths.

    Permanents that died in that batch still see the batch (last known
    information). Sources sacrificed later stop generating new triggers;
    already queued triggers survive. New triggers go above pending ones with
    APNAP ordering, and each controller orders their own simultaneous triggers.
    """
    if reason := unsupported(battlefield):
        raise UnsupportedContinuation("; ".join(reason))
    if active == other or any(
        seat(c) not in (active, other)
        for c in [*battlefield, *survivors, *dead]
        if not raw(c).get("is_phased_out") and not raw(c).get("face_down")
    ):
        raise UnsupportedContinuation("sacrifice continuation requires two known controllers")
    static = [
        c
        for c in battlefield
        if not creature(c) and not raw(c).get("is_phased_out") and not raw(c).get("face_down")
    ]
    remaining = max_states

    def tick():
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise UnsupportedContinuation("sacrifice choice/state bound exceeded")

    def events(dying, live_before, sacrificed=False):
        sources = [*static, *live_before]
        suppressed = death_is_suppressed([raw(c) for c in sources])
        result = []
        for source in sources:
            pacts, takes = abilities(source)
            for victim in dying:
                if not suppressed and seat(source) == seat(victim):
                    for includes_self in pacts:
                        if includes_self or identity(source) != identity(victim):
                            result.append(
                                Event(seat(source), "sacrifice", other if seat(source) == active else active)
                            )
                if sacrificed and not token(victim) and seat(source) != seat(victim):
                    result.extend(Event(seat(source), "take", identity(victim)) for _ in range(takes))
        return result

    def schedule(new, stack, live, grave, sacrificed, transfers):
        def ordered(index, pending):
            tick()
            if index == 2:
                return run(pending, live, grave, sacrificed, transfers)
            controller = (active, other)[index]
            group = [e for e in new if e.controller == controller]
            if len(group) > 6:
                raise UnsupportedContinuation("simultaneous sacrifice trigger-order bound exceeded")
            orders = list(dict.fromkeys(permutations(group)))
            choices = tuple(ordered(index + 1, pending + order) for order in orders)
            return (
                choices[0]
                if len(choices) == 1
                else Choice(controller, "order simultaneous triggers", choices)
            )

        return ordered(0, stack)

    def run(stack, live, grave, sacrificed, transfers):
        tick()
        if not stack:
            return Choice(None, "resolved", outcome=Outcome(tuple(live), sacrificed, transfers))
        event, pending = stack[-1], stack[:-1]
        if event.kind == "sacrifice":
            victims = [c for c in live if seat(c) == event.target and not raw(c).get("is_phased_out")]
            if not victims:
                return run(pending, live, grave, sacrificed, transfers)
            choices = []
            for victim in sorted(victims, key=identity):
                iid = identity(victim)
                left = [c for c in live if identity(c) != iid]
                yard = grave if token(victim) else {**grave, iid: victim}
                continuation = schedule(
                    events([victim], live, True), pending, left, yard, sacrificed + (iid,), transfers
                )
                choices.append(Choice(event.target, f"sacrifice {iid}", (continuation,)))
            return Choice(event.target, "choose sacrifice victim", tuple(choices))
        victim = grave.get(event.target)
        if victim is None:
            return run(pending, live, grave, sacrificed, transfers)
        decline = run(pending, live, grave, sacrificed, transfers)
        new_id = -9_000_000 - abs(event.target) * 100 - len(transfers)
        taken = fresh_capture(victim, event.controller, new_id, turn)
        yard = {i: c for i, c in grave.items() if i != event.target}
        accept = run(
            pending, [*live, taken], yard, sacrificed, transfers + ((event.target, new_id, event.controller),)
        )
        return Choice(event.controller, f"may take {event.target}", (decline, accept))

    initial = events(dead, [*survivors, *dead])
    yard = {identity(c): c for c in dead if not token(c)}
    return schedule(initial, (), list(survivors), yard, (), ())
