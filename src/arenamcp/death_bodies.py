"""Fixed self-death token replacements for bounded line-search projections."""

import re


def token_specs_on_death(card):
    from arenamcp.line_search_moves import token_specs

    raw = card.get("_card") or card
    text = str(raw.get("oracle_text") or "").lower().replace(str(raw.get("name") or "").lower(), "~")
    found = re.search(r"(?:^|[.\n]\s*)when (?:~|this creature) dies, (create [^.]+\.)", text)
    return token_specs(found[1]) if found else []


def death_is_suppressed(battlefield):
    return any(
        re.search(
            r"creatures (?:entering(?: the battlefield)? or )?dying don't cause abilities",
            str(c.get("oracle_text") or "").lower(),
        )
        for c in battlefield
    )


def removal_causes_death(reach, bounce=False):
    if bounce:
        return False
    # Exile replacement (including 'if ... would die ... exile it instead')
    # is conservative: never create a death token for an exiling spell.
    return "exile" not in str((reach or {}).get("oracle_text") or "").lower()
