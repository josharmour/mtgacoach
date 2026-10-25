"""Terminal combat-hit abilities, separate from ordinary life loss."""

import re


def loses_on_combat_hit(card: dict) -> bool:
    """Recognize unconditional player-loss hits and permanent Saga chapter grants.

    Do not treat printed future chapters or conditional abilities (Vraska's
    finisher, for example) as ordinary damage. A Saga's lore must have reached
    the grant; temporary/activated grants require live ability evidence.
    """
    # Arena's live granted ability for Zantetsuken (ledger #247).
    abilities = [*(card.get("ability_grp_ids") or []), *(card.get("granted_abilities") or [])]
    if any(str(a) == "19443" for a in abilities):
        return True
    text = str(card.get("oracle_text") or "").lower()
    if "loses the game" not in text:
        return False
    text = re.sub(r"<[^>]*>", "", text)
    trigger = (
        r"whenever (?:this creature|"
        + re.escape(str(card.get("name") or "").lower())
        + r") deals combat damage to a player, that player loses the game\."
    )
    for line in text.splitlines():
        if not re.search(trigger, line):
            continue
        chapter = re.match(r"\s*(i{1,3}|iv|v)\s*[—-]", line)
        if chapter:
            if 'this creature gains "whenever' not in line or "until" in line:
                continue
            lore = next(
                (v for k, v in (card.get("counters") or {}).items() if str(k).lower().endswith("lore")), 0
            )
            try:
                if int(lore) >= {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}[chapter[1]]:
                    return True
            except (TypeError, ValueError):
                pass
        elif re.match(trigger, line.strip()):
            return True
    return False
