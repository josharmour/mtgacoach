"""Bounded public outcome vectors for G04/#308, not a full-game ranking.

Compare life, cards and bodies separately: there is no justified universal
conversion between a discard, an engine and immediate survival. Every branch
assumes resolution with no further actions. Unknown trigger use branches both
ways; absence of an observed trigger is never proof it is unused.
"""

import re
from dataclasses import asdict, dataclass

from arenamcp.combat_keywords import has_combat_keyword, is_creature_now
from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import find_source
from arenamcp.public_sequence import _ONCE, _draw_entry, _text


@dataclass(frozen=True)
class SequenceValue:
    option_id: str
    play: str
    condition: str
    life: int = 0
    draws: int = 0
    opponent_discards: int = 0
    removed: tuple[int, ...] = ()
    survives: tuple[int, ...] = ()
    tapped: tuple[int, ...] = ()
    army_counters: int = 0

    def record(self):
        return asdict(self)

    def summary(self):
        return (
            f"{self.option_id} {self.play} [{self.condition}]: life {self.life:+d}, "
            f"draws {self.draws}, opponent discards {self.opponent_discards}; "
            f"removed IDs {list(self.removed)}, surviving IDs {list(self.survives)}, "
            f"tapped IDs {list(self.tapped)}, opposing Army counters +{self.army_counters}."
        )


# Reject continuous/replacement effects this small projection cannot apply.
_COMPLEX = re.compile(
    r"\b(?:instead|prevent|double|protection|ward|shield|toxic|infect|wither)\b|"
    r"\b(?:can't|cannot) (?:gain|lose|draw|be dealt)|"
    r"\b(?:creatures?|permanents?) (?:you control |your opponents control )?(?:get|have)|"
    r"\byou (?:have|gain) (?:hexproof|shroud|protection)|"
    r"\b(?:gets?|gains?) [+-]\d|\benters?[^.]*\bcounters?\b|"
    r"\b(?:don't|doesn't|cannot|can't) cause abilities|trigger[^.]*additional time",
    re.I,
)


def _complex(card):
    # Names may contain rules words (e.g. "Shield"); only effect text counts.
    name = str(card.get("name") or "")
    text = re.sub(re.escape(name), "this object", _text(card), flags=re.I) if name else _text(card)
    return _COMPLEX.search(text)


def _simple_target(card):
    return (
        is_creature_now(card)
        and not card.get("is_phased_out")
        and type(card.get("toughness")) is int
        and card["toughness"] > 0
        and not _complex(card)
        and not any(has_combat_keyword(card, k) for k in ("hexproof", "shroud", "indestructible"))
    )


def _protection(card):
    return re.search(
        r"(?:^|\n)discard a card:\s*(?:this creature|"
        + re.escape(str(card.get("name") or ""))
        + r") gains indestructible until end of turn\.(?P<tap>\s*tap it\.)?(?=\s*(?:\n|$))",
        _text(card),
        re.I,
    )


def _burn(card):
    # Exact fixed spell; no kicker, modes, additional cost or conditional gain.
    return (
        re.fullmatch(
            re.escape(str(card.get("name") or ""))
            + r" deals (?P<damage>\d+) damage to (?:any target|target creature)\."
            r"(?: You gain (?P<gain>\d+) life\.)?",
            " ".join(_text(card).split()),
            re.I,
        )
        if any(t in str(card.get("type_line") or "").lower() for t in ("instant", "sorcery"))
        else None
    )


def _punisher(card):
    # The ETB shares Bowmasters' trigger, but here only the draw event is projected.
    return re.search(
        r"whenever an opponent draws a card(?: except the first one they draw in each of their draw steps)?, "
        r"(?:this creature|"
        + re.escape(str(card.get("name") or ""))
        + r") deals 1 damage to any target\. Then amass Orcs 1\.",
        _text(card),
        re.I,
    )


def sequence_values(decision, state, *, used_draw_triggers=frozenset()):
    """At most one draw engine and punisher; same-current-board cast alternatives.

    This is a component-wise valuation, not permission to sequence two offered
    casts without paying for both. It never predicts hidden cards or combat.
    """
    local = get_local_seat_id(state)
    turn = state.get("turn") or {}
    if (
        decision.request_type != "ActionsAvailable"
        or local is None
        or state.get("stack")
        or turn.get("active_player") != local
        or str(turn.get("phase") or "").lower() not in {"main1", "main2", "phase_main1", "phase_main2"}
    ):
        return ()
    board = [c for c in state.get("battlefield") or [] if not c.get("is_phased_out")]
    if any(_complex(c) for c in board):
        return ()
    enemies = [c for c in board if c.get("controller_seat_id") not in (None, local)]
    protectors = [c for c in enemies if _protection(c)]
    punishers = [c for c in enemies if _punisher(c)]
    engines = [c for c in board if c.get("controller_seat_id") == local and _draw_entry(c)]
    if not protectors and not punishers:
        return ()
    hand = state.get("opponent_hand_count", (state.get("zones") or {}).get("opponent_hand_count"))
    locked = any(re.search(r"can't activate|cannot activate", _text(c), re.I) for c in board)
    values = []
    for option in decision.options:
        if option.payable is False or option.meta.get("actionType") != "ActionType_Cast":
            continue
        source = find_source(state, option.meta)
        if not source or source.get("controller_seat_id") != local:
            continue
        name, iid = source.get("name"), source.get("instance_id")
        burn = _burn(source)
        if burn:
            for target in enemies:
                if not _simple_target(target):
                    continue
                damage = target.get("damage", 0)
                if type(damage) is not int or int(burn["damage"]) + damage < target["toughness"]:
                    continue
                target_id = target.get("instance_id")
                if type(target_id) is not int:
                    continue
                gain = int(burn["gain"] or 0)
                play = f"{name} on {target.get('name')}"
                values.append(
                    SequenceValue(
                        option.option_id, play, "no protection response", life=gain, removed=(target_id,)
                    )
                )
                protection = _protection(target)
                if protection and type(hand) is int and hand > 0 and not locked:
                    values.append(
                        SequenceValue(
                            option.option_id,
                            play,
                            "opponent discards for indestructible",
                            life=gain,
                            opponent_discards=1,
                            survives=(target_id,),
                            tapped=(target_id,) if protection["tap"] else (),
                        )
                    )
        if re.search(
            r"\b(?:when|whenever|as)\b[^.]*\benter|\benters? (?:with|as)|\b(?:enlist|devour)\b",
            _text(source),
            re.I,
        ):
            continue  # this entry has additional effects not in the small outcome vector
        if not is_creature_now(source) or len(engines) != 1 or len(punishers) != 1 or type(iid) is not int:
            continue
        engine, punisher = engines[0], punishers[0]
        power = source.get("power")
        if type(power) is not int or power > int(_draw_entry(engine)[1]) or _complex(source):
            continue
        once = bool(_ONCE.search(_text(engine)))
        spent = engine.get("instance_id") in used_draw_triggers
        # A spent/possibly spent trigger leaves this body but gives no card.
        if once:
            values.append(
                SequenceValue(
                    option.option_id,
                    str(name),
                    "draw trigger already spent" if spent else "if draw trigger already spent",
                    survives=(iid,),
                )
            )
        if spent and once:
            continue
        prefix = "if draw trigger unused; " if once else ""
        values.append(
            SequenceValue(
                option.option_id,
                str(name),
                prefix + f"{punisher.get('name')} pings us",
                life=-1,
                draws=1,
                survives=(iid,),
                army_counters=1,
            )
        )
        if _simple_target(source) and source["toughness"] == 1:
            values.append(
                SequenceValue(
                    option.option_id,
                    str(name),
                    prefix + f"{punisher.get('name')} pings entering creature",
                    draws=1,
                    removed=(iid,),
                    army_counters=1,
                )
            )
    return tuple(values[:24])


def sequence_value_notes(values):
    if not values:
        return ()
    return (
        "BOUNDED PUBLIC OUTCOMES (parsed effects only, no further actions; separate conditional branches, not a full-game ranking). "
        "Unrelated effects are unvalued, not proved absent. Compare life, cards and surviving bodies. A surviving creature's future activation is unavailable in a branch where it dies. "
        "Removed opposing draw-punisher IDs cannot punish a later draw. Future draws/cards, combat and combined-cast affordability are not projected.",
        *(v.summary() for v in values),
    )
