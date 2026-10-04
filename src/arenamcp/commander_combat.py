"""Ground commander-versus-copy blocking decisions in identity and recast cost."""

from arenamcp.combat_solver import _resolve_attacker


def commander_block_context(state: dict, context: dict) -> list[str]:
    """Describe recovery choices without pretending a future cast is payable now.

    The combat solver prices bodies and ongoing abilities. It does not model
    returning a commander or replaying its enters ability, so equal bodies can
    have very different recovery value. Only Arena's exact commander identity
    and legal blocking pairs authorize this comparison; a shared grpId does not.
    """
    player = next((p for p in state.get("players", []) if p.get("is_local")), {})
    seat = player.get("seat_id") or state.get("local_seat_id")
    commander_ids = set(player.get("commander_ids") or [])
    cards = {c["instance_id"]: c for c in state.get("battlefield", []) if c.get("instance_id")}
    legal = {
        b["blockerInstanceId"]: set(b.get("attackerInstanceIds") or [])
        for b in context.get("raw_blockers") or []
        if b.get("blockerInstanceId")
    }

    def token(card):
        return card.get("is_token") or "token" in str(card.get("object_kind", "")).lower()

    def label(card):
        return f"{'*' if token(card) else ''}{card.get('name', 'Creature')} [id:{card['instance_id']}]"

    def dies(card, attacker):
        if any(
            not isinstance(c.get(k), (int, float)) for c in (card, attacker) for k in ("power", "toughness")
        ):
            return False
        return bool(_resolve_attacker(attacker, [card]).blockers_died)

    lines = []
    for iid in sorted(commander_ids & legal.keys()):
        card = cards.get(iid, {})
        if (
            not card
            or token(card)
            or not seat
            or card.get("owner_seat_id") != seat
            or (card.get("controller_seat_id") or card.get("owner_seat_id")) != seat
        ):
            continue
        casts_by_grp = state.get("commander_casts") or {}
        grp = card.get("grp_id")
        casts = casts_by_grp.get(grp, casts_by_grp.get(str(grp), card.get("commander_casts")))
        cost = card.get("mana_cost") or "UNKNOWN"
        if isinstance(casts, int) and not isinstance(casts, bool) and casts >= 0:
            recast = f"printed {cost} plus {{{2 * casts}}} commander tax ({casts} prior command-zone casts)"
        else:
            recast = f"printed {cost} plus UNKNOWN commander tax; do not assume zero tax"
        lines.append(
            f"Commander recovery: {label(card)} is YOUR COMMANDER. Next command-zone cast costs {recast}. "
            "If it dies, command-zone recovery is available; token copies cannot return there. "
            "The combat solver does not price commander recovery or replaying enters abilities."
        )
        if card.get("oracle_text"):
            lines.append(f"Commander rules for the recast comparison: {card['oracle_text']}")
        copies = [
            copy
            for bid, copy in cards.items()
            if bid in legal
            and token(copy)
            and grp
            and copy.get("grp_id") == grp
            and (copy.get("controller_seat_id") or copy.get("owner_seat_id")) == seat
        ]
        for attacker_id in sorted(legal[iid]):
            attacker = cards.get(attacker_id)
            if not attacker or not dies(card, attacker):
                continue
            alternatives = [c for c in copies if attacker_id in legal[c["instance_id"]] and dies(c, attacker)]
            if not alternatives:
                continue
            # Enumerate the unchanged resources before evaluating replay
            # effects. A model comparing two alternatives can otherwise carry
            # the substitute's death into the commander-dies branch too.
            local_creatures = [
                c
                for c in cards.values()
                if (c.get("controller_seat_id") or c.get("owner_seat_id")) == seat
                and (
                    "creature" in str(c.get("type_line", "")).lower()
                    or "Creature" in (c.get("card_types") or [])
                )
            ]
            survivors = [c for c in local_creatures if c["instance_id"] != iid]
            lines.append(
                f"Single-trade ledger if {label(card)} dies: unchanged surviving creatures = "
                f"{', '.join(label(c) for c in survivors) or 'none'} ({len(survivors)} bodies). "
                f"An eventual recast adds the original back: {len(survivors) + 1} bodies BEFORE "
                "new cast/entry effects. Apply those effects to these survivors, not to the "
                "other alternative's board. This ledger isolates one trade; subtract other "
                "combat losses separately in BOTH alternatives."
            )
            for alternative in alternatives:
                survivors = [c for c in local_creatures if c["instance_id"] != alternative["instance_id"]]
                lines.append(
                    f"Single-trade ledger if {label(alternative)} dies instead: unchanged creatures = "
                    f"{', '.join(label(c) for c in survivors) or 'none'} ({len(survivors)} bodies); "
                    "no recast or new entry is caused by this alternative."
                )
            lines.append(
                f"Recovery comparison against {label(attacker)}: {label(card)} or "
                f"{', '.join(label(c) for c in alternatives)} would die in a single block under visible combat. "
                "Equivalent combat outcomes do not imply equal strategic value. Apply this deck's "
                "conditional decision rules: compare replayable cast/enter/death value with preserving "
                "ongoing abilities, counters or other invested resources. Do not automatically choose "
                "either a token or the commander. Check commander tax, colored mana "
                "from the SURVIVING board after untapping, other planned spells, and the delay before new "
                "creatures can tap. Never count the dying commander as a source for its own recast. "
                "Recasting is only a plan if its next recast is affordable and worthwhile; "
                "surviving combat takes precedence."
            )
    return lines
