"""Priority decisions for the observed 91-Mountain Laelia Brawl list."""

import re

from arenamcp.deck_strategy import commander_ids, deck_identity

# Exact main list and commander from the Windows match report.
_DECK = "707b787427d782f34c18013695301a1dc5abb2780cc6ac8deb91b9bcead6edab"
_LAELIA = 83081
_FAVOR = 87300
_DISCOVERY = 87291


def is_laelia_list(state):
    return _LAELIA in commander_ids(state) and deck_identity(state) == _DECK


def analysis_note(identity):
    if identity != _DECK:
        return ""
    return (
        "Starting-list facts: 92/99 lands (92.9%): 91 Mountains and Hidden Volcano. "
        "Cascade and discover exile cards one at a time. Laelia gets one counter per card exiled, "
        "even when no eligible spell remains. An Appraiser cast can discover Favor if it is still "
        "in the library. Once Favor enchants Laelia its own discover 3 has no eligible card in "
        "this list. Its trample and counters convert into combat damage after blockers. "
        "The attack trigger's play permission uses normal costs and land drops."
    )


def validate_generated_plan(playbook):
    """Reject the two false assertions found in the regenerated Windows analysis."""
    if playbook.identity != _DECK:
        return
    data = playbook.data
    claims = [data.get(key, "") for key in ("primary_plan", "backup_plan", "spoken_summary", "archetype")]
    claims.extend(data.get("phases", {}).values())
    claims.extend(data.get("card_roles", {}).values())
    claims.extend(m.get("effect", "") for m in data.get("mechanisms", []))
    claims.extend(r.get("prefer", "") for r in data.get("decision_rules", []))
    for claim in claims:
        if re.search(r"(?i)(?:~?10(?:\.0)?%\s+land(?:s| density)?|land density (?:is |of )?~?10%)", claim):
            raise ValueError(analysis_note(playbook.identity))
        if re.search(
            r"(?i)(?:cascade|discover) (?:revealing|exiling) (?:five|multiple|several|\d+) cards (?:is|as|gives) (?:only )?(?:one|a single) trigger",
            claim,
        ):
            raise ValueError(analysis_note(playbook.identity))


def priority_note(decision, state):
    if decision.request_type != "ActionsAvailable" or not is_laelia_list(state):
        return ""
    return (
        "Laelia list: 91 Mountains and Hidden Volcano in the 99-card main deck. With Laelia present, "
        "each card exiled by cascade or discover triggers a counter, even if there is no spell hit. "
        "Before passing Main1, compare a payable discover/cascade spell or Hidden Volcano's offered "
        "discover activation against this attack. Favor on Laelia gives trample and discovers through "
        "the entire remaining library. Daring Discovery can choose zero to three blockers. "
        "Without Laelia, check her offered command-zone cast before spending an enabler. "
        "Account for removal, countermagic, blockers and survival. Use Arena's confirmed payable option."
    )


def correct_pass(decision, state, chosen, reasoning):
    """Correct the demonstrated false premises only, on a confirmed payable menu."""
    if (
        chosen != ["pass"]
        or decision.request_type != "ActionsAvailable"
        or state.get("stack")
        or not is_laelia_list(state)
    ):
        return None
    local = state.get("local_seat_id")
    turn = state.get("turn") or {}
    if not local or turn.get("active_player") != local or turn.get("priority_player", local) != local:
        return None
    if str(turn.get("phase")).lower() not in {"main1", "phase_main1"}:
        return None
    text = str(reasoning or "").lower()
    if re.search(r"counterspell|countermagic|removal|board wipe|hold up|lethal on board", text):
        return None
    casts = {
        o.meta.get("instanceId"): o
        for o in decision.options
        if o.payable is True
        and o.meta.get("actionType") == "ActionType_Cast"
        and not o.meta.get("alternativeGrpId")
    }
    for card in state.get("command") or []:
        option = casts.get(card.get("instance_id"))
        if card.get("grp_id") == _LAELIA and card.get("owner_seat_id") == local and option:
            if "laelia" in text and re.search(
                r"(?:cannot|can.t|unable to) (?:afford|cast|recast)|only \d+ mana|short of mana|not enough mana",
                text,
            ):
                return (
                    option.option_id,
                    "Arena confirms the command-zone cast is payable. Recast Laelia before using the exile enabler.",
                )
    laelia = next(
        (
            c
            for c in state.get("battlefield") or []
            if c.get("grp_id") == _LAELIA and c.get("controller_seat_id") == local and not c.get("is_tapped")
        ),
        None,
    )
    if not laelia or not re.search(
        r"(?:only|mostly|just) (?:exile |hit |yield |find |reveal )*(?:lands|mountains)|no (?:eligible |legal )?(?:hit|spell to find)",
        text,
    ):
        return None
    sources = {
        c.get("instance_id"): c
        for zone in ("hand", "exile")
        for c in state.get(zone) or []
        if c.get("owner_seat_id") == local
    }
    for iid, option in casts.items():
        gid = sources.get(iid, {}).get("grp_id")
        if gid == _FAVOR:
            targets = {
                t.get("targetInstanceId")
                for group in option.meta.get("targets") or []
                for t in group.get("targets") or []
                if "cold" not in str(t.get("highlight", "")).lower()
            }
            if laelia.get("instance_id") in targets:
                return (
                    option.option_id,
                    "Cast Etali's Favor before combat: Laelia is an offered target and every card exiled grows her.",
                )
        if gid in {_DISCOVERY, 87301, 87325, 71491, 98652, 91109} and len(casts) == 1:
            return (
                option.option_id,
                f"Cast {sources[iid].get('name') or 'the exile enabler'} before combat. Exiling Mountains grows Laelia even if there is no spell hit.",
            )
    if not casts:
        for option in decision.options:
            meta = option.meta
            if (
                option.payable is True
                and meta.get("actionType") == "ActionType_Activate"
                and meta.get("grpId") == 87443
                and meta.get("abilityGrpId")
            ):
                from arenamcp.decisions import _default_ability_description

                _, ability = _default_ability_description(meta["abilityGrpId"])
                if re.search(r"\bdiscover 4\b", ability, re.I):
                    return (
                        option.option_id,
                        "Activate Hidden Volcano before combat. Each card exiled by discover 4 grows Laelia.",
                    )
    return None
