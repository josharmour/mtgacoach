"""Tokens that leave at the next end step, read from the card that made them (ledger #185).

Match 4d934402 (2026-10-19 T9/T11): Lagomos, Hand of Hatred ("At the beginning
of combat on your turn, create a 2/1 red Elemental creature token with trample
and haste. Sacrifice it at the beginning of the next end step.") and
Urabrask's Forge ("... create an X/1 red Phyrexian Horror creature token ...
Sacrifice that token at the beginning of the next end step.") attacked with
tokens that were gone by the end of the turn anyway. The tokens themselves
carry only "Trample / Haste", so nothing that read the token's text
(``combat_solver.leaves_at_our_end_step``, ``line_search_moves._TOKEN_TEMPORARY``)
knew; the block solver valued killing them like real creatures and traded two
Rabbits for them into Vein Ripper's death drain.

The creator is a log fact: the token's ``objectSourceGrpId`` names the card
and the ``AnnotationType_TokenCreated`` affector is the creating ability
(its grp id and the permanent it came from). ``gamestate`` records them on
the token; the snapshot serializer turns them into ``token_creator`` and
``token_expires`` ("sacrifice" / "exile" / "return").
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# "Sacrifice it at the beginning of the next end step." / "Exile them at the beginning of the
# next end step." / "At the beginning of the next end step, sacrifice that token."
_EXPIRES_AFTER = re.compile(
    r"\b(?P<how>sacrifice|exile|return) (?:it|them|that token|those tokens|the token|the tokens)\b"
    r"[^.]*?\bat the beginning of (?:the )?next end step\b"
)
_EXPIRES_BEFORE = re.compile(
    r"\bat the beginning of (?:the )?next end step, (?P<how>sacrifice|exile|return) "
    r"(?:it|them|that token|those tokens|the token|the tokens)\b"
)
_CREATES_CREATURE_TOKEN = re.compile(r"\bcreate\b[^.]*\bcreature tokens?\b")


@lru_cache(maxsize=1024)
def token_expiry(creator_text: str) -> str:
    """How a creature token made by ``creator_text`` leaves at the next end step, or "".

    Only a sentence of the ability that creates creature tokens counts (the
    creating ability's own text when the log names it, else each line of the
    creator card that creates a creature token). Reminder text and markup are
    ignored.
    """
    text = re.sub(r"<[^>]*>", "", str(creator_text or "")).lower()
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    for line in text.splitlines():
        if not _CREATES_CREATURE_TOKEN.search(line):
            continue
        found = _EXPIRES_AFTER.search(line) or _EXPIRES_BEFORE.search(line)
        if found is not None:
            return found.group("how")
    return ""


def token_expires(card: Any) -> str:
    """The snapshot's ``token_expires`` mark of a token ("sacrifice" / "exile" / "return"), or "".

    Reads a planner body's ``_card`` too. Only tokens carry it.
    """
    if not isinstance(card, dict):
        return ""
    value = card.get("token_expires")
    if not value and isinstance(card.get("_card"), dict):
        value = card["_card"].get("token_expires")
    return str(value or "")


def dies_anyway(card: Any) -> bool:
    """The token is sacrificed at the next end step: it dies whether or not combat kills it."""
    return token_expires(card) == "sacrifice"
