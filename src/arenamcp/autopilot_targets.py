"""Target-name resolution helpers extracted from autopilot.py (pure move, no behavior change)."""

from typing import Any


def _match_target_in_battlefield(
    target_names: list[str],
    battlefield: list[dict[str, Any]],
    eligible: Any,
) -> tuple[int | None, str | None]:
    """Resolve a target-name hint to a battlefield instance_id.

    Tries exact (case-insensitive) match first, then substring match
    against either direction (helps when the LLM truncates "Wonderweave
    Aerialist" to "Aerialist"), then token-overlap as a last resort.
    `eligible(card)` filters the battlefield to only cards the bridge
    has flagged as legal targets.
    """
    if not target_names:
        return None, None

    candidates = [c for c in (battlefield or []) if eligible(c)]

    def _name(card: dict[str, Any]) -> str:
        return str(card.get("name") or "").strip()

    def _iid(card: dict[str, Any]) -> int | None:
        try:
            v = int(card.get("instance_id") or 0)
        except (TypeError, ValueError):
            return None
        return v or None

    for name in target_names:
        want = (name or "").strip().lower()
        if not want:
            continue
        for card in candidates:
            if _name(card).lower() == want:
                iid = _iid(card)
                if iid:
                    return iid, _name(card)

    # Substring match (either direction). Picks the longest matching
    # candidate name to prefer "Wonderweave Aerialist" over "Aerialist".
    for name in target_names:
        want = (name or "").strip().lower()
        if len(want) < 3:
            continue
        matches: list[tuple[int, str, int]] = []
        for card in candidates:
            cn = _name(card).lower()
            iid = _iid(card)
            if iid and cn and (want in cn or cn in want):
                matches.append((iid, _name(card), len(cn)))
        if matches:
            matches.sort(key=lambda x: -x[2])
            return matches[0][0], matches[0][1]

    # Token-overlap fallback (≥2 shared tokens, ignoring short stopwords).
    STOP = {"the", "of", "and", "a", "an", "in", "to", "on"}
    for name in target_names:
        tokens = {t.strip(",.:;\"'()[]").lower() for t in (name or "").split() if t and t.lower() not in STOP}
        tokens = {t for t in tokens if len(t) >= 3}
        if not tokens:
            continue
        best: tuple[int, int, str] | None = None
        for card in candidates:
            cn_tokens = {
                t.strip(",.:;\"'()[]").lower() for t in _name(card).split() if t and t.lower() not in STOP
            }
            cn_tokens = {t for t in cn_tokens if len(t) >= 3}
            overlap = len(tokens & cn_tokens)
            iid = _iid(card)
            if iid and overlap >= 2 and (best is None or overlap > best[1]):
                best = (iid, overlap, _name(card))
        if best:
            return best[0], best[2]

    return None, None


_PLANNER_CARD_NAME_PREFIXES = (
    "ability:",
    "activate ability:",
    "activate:",
    "play land:",
    "cast:",
    "cast spell:",
)


def _normalize_planner_card_name(name: str) -> str:
    """Strip leading legal-action-string labels the LLM sometimes leaves on.

    The legal_actions strings the planner reads are formatted like
    "Activate Ability: Promising Vein" / "Cast Lightning Bolt", and the
    schema instructs the LLM to put just the card name in `card_name`.
    Models occasionally keep the label prefix anyway. The bridge match
    path does case-insensitive equality against the bridge's resolved
    card name ("Promising Vein"), so an unstripped prefix silently
    breaks every type+name match for that ability.

    Strips one matching prefix, case-insensitively. Idempotent on
    already-clean names.
    """
    if not name:
        return name
    stripped = name.strip()
    lo = stripped.lower()
    for prefix in _PLANNER_CARD_NAME_PREFIXES:
        if lo.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped
