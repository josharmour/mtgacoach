"""What casting one of our own spells is worth besides its effect.

2026-10-19 (match 115944ce T8, #65): Iroh's Demonstration's "4 damage to
target creature" mode was aimed at a 7/7 Trostani. The kill check declined the
target, which cancels the whole cast, and the self-cancel guard then hid the
spell for the turn — losing the cast trigger it was cast for: two Slickshot
Show-Offs (+2/+0 each) and a prowess Emberheart Challenger, all about to
attack an opponent with no untapped creature. The spell's other mode (1
damage to each creature they control) needs no target.

- :func:`cast_trigger_attackers` — our creatures a noncreature spell cast now
  pumps for a combat still to come; while there are any, a no-kill target is
  no reason to give the spell back.
- :func:`untargeted_mode_instead` — for a modal spell, the one-sided mode
  without a target to choose when the chosen targeted damage mode kills
  nothing.
"""

from __future__ import annotations

import re
from typing import Any

# "Whenever you cast a noncreature spell" (Slickshot Show-Off), "an instant or
# sorcery spell", magecraft's "cast or copy".
_CAST_TRIGGER = re.compile(
    r"\bwhenever you cast (?:or copy )?(?:a|an) (?:noncreature|instant or sorcery|instant|sorcery) spell\b"
)
_PROWESS_LINE = re.compile(r"(?:^|\n)\s*(?:[a-z ,]+,\s*)?prowess\b")
_REMINDER = re.compile(r"\([^()]*\)")


def _text(card: dict) -> str:
    text = re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).lower()
    return _REMINDER.sub("", text)


def has_cast_trigger(card: dict) -> bool:
    """Prowess, or a "whenever you cast a noncreature/instant/sorcery spell" trigger of its own."""
    keywords = {str(k).lower() for k in card.get("keywords") or ()}
    text = _text(card)
    return "prowess" in keywords or bool(_PROWESS_LINE.search(text)) or bool(_CAST_TRIGGER.search(text))


def _local_seat(state: dict) -> Any:
    seat = state.get("local_seat_id")
    if seat is not None:
        return seat
    return next((p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None)


def cast_trigger_attackers(state: dict) -> list[dict]:
    """Our creatures whose cast trigger a spell cast now feeds into this turn's combat.

    Our turn, before combat damage: before attackers are declared, creatures
    that can still attack (:func:`combat_strategy.able_attackers`); once they
    are, the attacking ones.
    """
    turn = state.get("turn") or {}
    local = _local_seat(state)
    if local is None or turn.get("active_player") != local:
        return []
    phase = str(turn.get("phase") or "")
    step = str(turn.get("step") or "")
    if phase in ("Phase_Beginning", "Phase_Main1") or (
        phase == "Phase_Combat" and step in ("", "-", "Step_BeginCombat", "Step_DeclareAttack")
    ):
        from arenamcp.combat_strategy import able_attackers

        candidates = list(able_attackers(state))
    else:
        candidates = []
    if phase == "Phase_Combat" and step in ("Step_DeclareAttack", "Step_DeclareBlock"):
        candidates += [
            card
            for card in state.get("battlefield") or []
            if isinstance(card, dict)
            and card.get("is_attacking")
            and (card.get("controller_seat_id") or card.get("owner_seat_id")) == local
            and card not in candidates
        ]
    return [card for card in candidates if has_cast_trigger(card)]


# One-sided, untargeted: "each creature your opponents control", "each
# opponent", "each creature you don't control".
_ONE_SIDED = re.compile(
    r"\beach (?:creature|creature and planeswalker|nonland permanent|opponent)\b"
    r"(?: (?:your opponents control|you don't control|an opponent controls))?"
)
_HITS_OURS = re.compile(
    r"\beach (?:other )?creature\b(?! (?:your opponents control|you don't control|an opponent controls"
    r"|they control|that player controls))"
)


def _mode_text(option: Any) -> str:
    label = str(getattr(option, "label", "") or "")
    text = label.split(":", 1)[1] if ":" in label else ""
    return re.sub(r"<[^>]*>", "", text).strip().lower()


def untargeted_mode_instead(decision: Any, chosen: list[str], state: dict) -> tuple[str, str] | None:
    """(option id, reason) of the mode to choose instead of a no-kill targeted damage mode, else None.

    ``decision`` is a CastingTimeOptions decision whose modal options carry
    the mode's rules text in their label ("Mode 2: CARDNAME deals 4 damage to
    target creature."). Only a single chosen "N damage to target creature"
    mode that kills no opposing creature on the board gives way, and only to
    a mode that targets nothing and spares our side.
    """
    from arenamcp.play_safety import damage_would_kill, fixed_damage_removal

    if getattr(decision, "request_type", "") != "CastingTimeOptions" or len(chosen) != 1:
        return None
    options = list(getattr(decision, "options", ()) or ())
    modal = [o for o in options if (getattr(o, "meta", None) or {}).get("choiceKind") == "modal"]
    picked = next((o for o in modal if o.option_id == chosen[0]), None)
    if picked is None:
        return None
    parsed = fixed_damage_removal({"oracle_text": _mode_text(picked)})
    if parsed is None:
        return None
    damage = parsed[0]
    local = _local_seat(state)
    if local is None:
        return None
    theirs = [
        card
        for card in state.get("battlefield") or []
        if isinstance(card, dict)
        and (card.get("controller_seat_id") or card.get("owner_seat_id")) not in (None, local)
        and "creature" in str(card.get("type_line") or "").lower()
    ]
    if any(damage_would_kill(card, damage) is not False for card in theirs):
        return None  # it kills something (or might): the targeted mode stands
    for option in modal:
        text = _mode_text(option)
        if option is picked or not text or "target" in text:
            continue
        if not _ONE_SIDED.search(text) or _HITS_OURS.search(text):
            continue
        if theirs:
            biggest = max(theirs, key=lambda card: card.get("toughness") or 0)
            board = f"{biggest.get('name')} {biggest.get('power')}/{biggest.get('toughness')}"
        else:
            board = "no creature"
        return option.option_id, (
            f"{damage} damage to target creature kills nothing on their side ({board}); "
            f"choosing '{text}' instead keeps the cast and its triggers"
        )
    return None
