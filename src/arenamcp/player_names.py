"""Public player identity and local, bounded username pronunciation.

No model calls: display identity stays verbatim while speech gets a best-effort
word reading, never a long alphabet recital. Names are data, not instructions.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from functools import lru_cache

_DIGITS = ("oh", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_TITLES = {
    "mr": "mister",
    "mrs": "missus",
    "ms": "miss",
    "dr": "doctor",
    "prof": "professor",
    "jr": "junior",
    "sr": "senior",
    "mc": "mac",
    "tv": "tee vee",
}
_ONSETS = set(
    [
        "bl",
        "br",
        "ch",
        "cl",
        "cr",
        "dr",
        "dw",
        "fl",
        "fr",
        "gh",
        "gl",
        "gn",
        "gr",
        "kh",
        "kl",
        "kn",
        "kr",
        "ng",
        "ph",
        "pl",
        "pr",
        "ps",
        "rh",
        "sc",
        "sh",
        "sk",
        "sl",
        "sm",
        "sn",
        "sp",
        "st",
        "sw",
        "th",
        "tr",
        "ts",
        "tw",
        "wh",
        "wr",
        "zh",
        "sch",
        "schm",
        "schl",
        "schn",
        "schr",
        "schw",
        "scr",
        "shr",
        "spl",
        "spr",
        "str",
        "thr",
        "cth",
    ]
)


def _word_reading(word):
    if word in _TITLES:
        return _TITLES[word]
    if not word.isascii():
        return word
    consonants = "bcdfghjklmnpqrstvwxz"
    if not re.search(r"[aeiouy]", word):
        word = word.replace("q", "k").replace("x", "z")
        return "a".join(word) if len(word) > 1 else word + "ay"
    onset = re.match("[" + consonants + "]+", word)
    if onset and len(onset[0]) > 1 and onset[0] not in _ONSETS:
        word = "a".join(onset[0]) + word[len(onset[0]) :]
    # Keep readable syllables while softening an opaque consonant run.
    return re.sub("[" + consonants + "]{5,}", lambda m: m[0] if m[0] == "ngths" else "a".join(m[0]), word)


def clean_name(value):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c for c in value if c.isprintable()).split())[:80]


def clean_names(value):
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for seat, raw in list(value.items())[:8]:
        if type(seat) is int or isinstance(seat, str) and seat in {"1", "2", "3", "4"}:
            number = int(seat)
            name = clean_name(raw)
            if 1 <= number <= 4 and name:
                result[number] = name
    return result


def snapshot_names(snapshot):
    names = clean_names(snapshot.get("player_names"))
    # Older checkpoints have only the known opponent name. Never assign it
    # without a resolved opposing seat or guess the local identity.
    local = snapshot.get("local_seat_id")
    opponent = snapshot.get("opponent_seat_id")
    seats = {p.get("seat_id") for p in snapshot.get("players", ()) if isinstance(p, Mapping)}
    if opponent is None and local in seats and len(seats) == 2:
        opponent = next(s for s in seats if s != local)
    name = clean_name(snapshot.get("opponent_name"))
    if type(opponent) is int and opponent != local and name:
        names.setdefault(opponent, name)
    return names


def actor_name(public, seat):
    names = clean_names(public.get("player_names"))
    if seat in names:
        return names[seat]
    local = public.get("local_seat_id")
    return "You" if local and seat == local else "Your opponent" if local and seat else "A player"


@lru_cache(maxsize=256)
def spoken_name(name):
    text = unicodedata.normalize("NFKC", clean_name(name))
    # Cosmetic handles, word boundaries, and common titles.
    wrapped = re.fullmatch(r"[xX]{2}(.+)[xX]{2}", text)
    if wrapped:
        text = wrapped[1]
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"^(mrs)(?=[a-zA-Z ._-])", "Missus ", text, flags=re.I)
    text = re.sub(r"^(mr)(?=[a-zA-Z ._-])", "Mister ", text, flags=re.I)
    for title, word in (("dr", "Doctor"), ("prof", "Professor"), ("ms", "Miss")):
        text = re.sub(r"^" + title + r"(?:[ ._-]+)", word + " ", text, flags=re.I)
    # Interior leetspeak only; numeric suffixes remain spoken digits.
    substitutions = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}
    text = re.sub(r"(?<=[A-Za-z])[013457](?=[A-Za-z])", lambda m: substitutions[m[0]], text)
    tokens = re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*|[0-9]+", text, flags=re.UNICODE)
    words = []
    for token in tokens:
        if token.isdecimal():
            words.extend(_DIGITS[int(c)] for c in token)
            continue
        words.append(_word_reading(token.lower()))
    result = " ".join(words)
    return result[:1].upper() + result[1:] if result else "Player"


def pronounce_players(text, names):
    """Replace only exact known handles, once; never card names or game numbers."""
    names = sorted(set(clean_names(names).values()), key=len, reverse=True)
    if not names:
        return text
    replacements = {name.casefold(): spoken_name(name) for name in names}
    pattern = r"(?<!\w)(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)"
    return re.sub(pattern, lambda m: replacements.get(m[0].casefold(), spoken_name(m[0])), text, flags=re.I)
