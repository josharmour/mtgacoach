"""Resolve card shorthand only within a grounded, unambiguous dependency phrase."""

import re


def library_dependencies(text, state, library_only):
    from arenamcp.game_plan import _mentions, _plain

    catalog = state.get("deck_catalog") or {}
    cards = list(catalog.values()) if isinstance(catalog, dict) else []
    for zone in ("hand", "battlefield", "graveyard", "exile", "stack", "command"):
        cards.extend(state.get(zone) or [])
    # A differently named card on the current menu can disambiguate an alias
    # even when its zone identity has not reached the snapshot yet.
    for action in state.get("_bridge_actions") or state.get("legal_actions_raw") or []:
        cards.append(action)
        gid = action.get("grpId")
        if isinstance(catalog, dict):
            cards.append(catalog.get(gid) or catalog.get(str(gid)) or {})
    names = {_plain(c.get("name")) for c in cards if isinstance(c, dict) and c.get("name")}
    needed = {
        name for name in library_only if (name in names or len(name.split()) > 1) and _mentions(text, name)
    }
    aliases = {}
    for name in names:
        for word in set(name.split()):
            if len(word) >= 4:
                aliases.setdefault(word, set()).add(name)
    for alias, identities in aliases.items():
        if len(identities) != 1:
            continue
        identity = next(iter(identities))
        if identity not in library_only:
            continue
        # Capitalization and dependency grammar both matter: "Will alpha"
        # and "for Will" name a card, while "Will attack" is ordinary prose.
        pattern = rf"\b(?:{re.escape(alias.title())}|{re.escape(alias.upper())})\b"
        for mention in re.finditer(pattern, str(text)):
            before = str(text)[max(0, mention.start() - 40) : mention.start()]
            after = str(text)[mention.end() :]
            if re.search(
                r"\b(?:for|as|cast|casting|hold|save|resolve|using|with|into|enable|enables)\s+(?:the\s+)?$",
                before,
                re.I,
            ) or re.match(r"\s+(?:enabler|alpha|finisher|lethal|dual.mode)\b", after, re.I):
                needed.add(identity)
                break
    return sorted(needed)
