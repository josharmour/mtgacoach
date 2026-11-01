"""Reciprocal damage for an identified creature's simple enters-fight ability.

#84/#262, 814296ac: the target died, but the prospective 7/6 blocker also
received lethal damage. A useful trade is still available; only its false
survival premise asks for another decision. No combat first/double strike.
"""

import re
from dataclasses import dataclass

from arenamcp.combat_keywords import has_combat_keyword, is_creature_now
from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import find_source
from arenamcp.ward import ward_of


def _text(card):
    return re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).replace("’", "'")


def _self_fight(card, oracle=None):
    name = "|".join(
        re.escape(n) for n in {str(card.get("name") or ""), str(card.get("name") or "").split(",", 1)[0]}
    )
    return re.search(
        r"(?:^|\n)when (?:cardname|this creature|"
        + name
        + r") enters(?: the battlefield)?, it fights (?:up to one |another )?target creature you don't control\.",
        _text(card) if oracle is None else oracle,
        re.I,
    )


def _stat(card, name):
    return card.get("modified_" + name, card.get(name))


def _uncertain(state, cards, prospective):
    for card in state.get("battlefield") or []:
        text = _text(card)
        if re.search(
            r"\bprevent\b[^.]*damage|damage[^.]*\binstead\b|\b(?:double|triple|twice)\b[^.]*damage|"
            r"\b(?:when|whenever)\b[^.]*\bdies?\b[^.]*return[^.]*battlefield",
            text,
            re.I,
        ):
            return True
        if prospective and re.search(
            r"\bcreatures?\b[^.]*\b(?:get|gets|have|has)\b|\b(?:when|whenever)\b[^.]*\b(?:creature|permanent)\b[^.]*enter[^.]*\b(?:counter|damage|gets|gains)\b|"
            r"\b(?:don't|doesn't|cannot|can't) cause abilities",
            text,
            re.I,
        ):
            return True
    for card in cards:
        if (
            not is_creature_now(card)
            or card.get("is_phased_out")
            or any(type(_stat(card, stat)) is not int for stat in ("power", "toughness"))
            or _stat(card, "toughness") <= 0
            or type(card.get("damage", 0)) is not int
            or card.get("protection_from_players")
            or re.search(r"\b(?:protection|wither|infect|shield|persist|undying)\b", _text(card), re.I)
            or any(
                str(k).lower().startswith(("protection", "wither", "infect", "ward"))
                for k in card.get("keywords") or []
            )
            or (
                isinstance(card.get("counters"), dict)
                and any("shield" in str(k).lower() for k in card["counters"])
            )
        ):
            return True
    return False


@dataclass(frozen=True)
class FightOutcome:
    option_id: str
    source: dict
    target: dict
    source_damage: int
    target_damage: int
    source_dies: bool
    target_dies: bool

    @property
    def reason(self):
        return (
            f"Fight with current stats and no intervening response: {self.source['name']} #{self.source['instance_id']} "
            f"deals {self.source_damage} to {self.target['name']} #{self.target['instance_id']}, which "
            f"{'dies' if self.target_dies else 'survives'}; {self.target['name']} simultaneously deals "
            f"{self.target_damage} back, so {self.source['name']} {'also dies and is not a surviving blocker' if self.source_dies else 'survives'}. "
            "First strike and double strike do not change fight damage. A trade can still be useful."
        )


def _dies(card, damage, dealer):
    return not has_combat_keyword(card, "indestructible") and (
        card.get("damage", 0) + damage >= _stat(card, "toughness")
        or (damage > 0 and has_combat_keyword(dealer, "deathtouch"))
    )


def fight_outcomes(decision, state):
    local = get_local_seat_id(state)
    if local is None:
        return ()
    board = state.get("battlefield") or []
    opponents = [c for c in board if c.get("controller_seat_id") not in (None, local)]
    sources = []
    prospective = decision.request_type == "ActionsAvailable"
    if prospective:
        if state.get("stack"):
            return ()
        for option in decision.options:
            if option.payable is False or option.meta.get("actionType") != "ActionType_Cast":
                continue
            source = find_source(state, option.meta)
            if source and _self_fight(source):
                sources.append((option.option_id, source))
    elif decision.request_type == "SelectTargets":
        context = state.get("decision_context") or {}
        payload = state.get("_bridge_request_payload") or {}
        sid = payload.get("sourceId") or context.get("source_id") or context.get("sourceId")
        ability = next((c for c in state.get("stack") or [] if c.get("instance_id") == sid), None)
        if not ability or any(c is not ability for c in state.get("stack") or []):
            return ()
        parent = ability.get("parent_instance_id") or context.get("source_parent_instance_id")
        source = next((c for c in board if c.get("instance_id") == parent), None)
        if not source or not _self_fight(source, _text(ability)):
            return ()
        offered = {
            o.meta.get("instanceId"): o.option_id for o in decision.options if o.option_id.startswith("tgt:")
        }
        opponents = [c for c in opponents if c.get("instance_id") in offered]
        sources = [("", source)]
    else:
        return ()
    outcomes = []
    for option_id, source in sources:
        if source.get("controller_seat_id") != local:
            continue
        for target in opponents:
            if _uncertain(state, (source, target), prospective):
                continue
            if any(has_combat_keyword(target, k) for k in ("hexproof", "shroud")):
                continue
            ward = ward_of(target, board)
            if ward is not None and (not ward.modelled or ward.mana != 0 or ward.life or ward.discard):
                continue
            outgoing = max(0, _stat(source, "power"))
            incoming = max(0, _stat(target, "power"))
            outcomes.append(
                FightOutcome(
                    option_id if prospective else offered[target["instance_id"]],
                    source,
                    target,
                    outgoing,
                    incoming,
                    _dies(source, incoming, target),
                    _dies(target, outgoing, source),
                )
            )
    return tuple(outcomes)


def _aliases(card, board):
    name = str(card.get("name") or "")
    prefix = name.split(",", 1)[0].split(" ", 1)[0]
    return [name] + (
        [prefix]
        if len(prefix) >= 4
        and sum(str(c.get("name") or "").split(",", 1)[0].split(" ", 1)[0] == prefix for c in board) == 1
        else []
    )


def _mentioned(card, clause, board):
    return any(
        re.search(r"\b" + re.escape(n) + r"(?=$|[\s,.'’])", clause, re.I) for n in _aliases(card, board) if n
    )


def fight_claim_concerns(decision, state, chosen, reasoning):
    board = [*(state.get("battlefield") or []), *(state.get("hand") or [])]
    clauses = re.split(r"[.;!?]", str(reasoning or "").replace("’", "'"))
    concerns = []
    for outcome in fight_outcomes(decision, state):
        if decision.request_type == "ActionsAvailable" and outcome.option_id not in chosen:
            continue
        for clause in clauses:
            conditional = re.search(
                r"\b(?:not|won't|cannot|can't|doesn't|don't|if|unless|after|protect|protection|indestructible|pump)\b",
                clause,
                re.I,
            )
            source_survives = any(
                re.search(r"\b" + re.escape(alias) + r"\s+survives?\b", clause, re.I)
                for alias in _aliases(outcome.source, board)
                if alias
            )
            source_claim = (
                not conditional
                and outcome.source_dies
                and _mentioned(outcome.target, clause, board)
                and _mentioned(outcome.source, clause, board)
                and (
                    source_survives
                    or re.search(r"\bleaves? (?:a |the )?(?:\d+/\d+ )?(?:blocker|body)\b", clause, re.I)
                )
            )
            target_claim = False
            if outcome.target_dies:
                for alias in _aliases(outcome.target, board):
                    match = (
                        re.search(r"\b" + re.escape(alias) + r"\s+survives?\b", clause, re.I)
                        if alias
                        else None
                    )
                    if not match:
                        continue
                    # Another target's indestructibility does not protect this one.
                    prefix = re.split(r"\band\b|,", clause[: match.start()])[-1]
                    tail = re.split(r"\band\b", clause[match.end() :])[0]
                    if not re.search(r"\b(?:if|unless|not|after)\b", prefix, re.I) and not re.search(
                        r"\b(?:if|unless|after|protect|protection|indestructible|pump)\b", tail, re.I
                    ):
                        target_claim = True
            if source_claim or target_claim:
                concerns.append(outcome.reason)
                break
    return tuple(dict.fromkeys(concerns))
