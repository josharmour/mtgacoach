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
            "If it dies, return it to the command zone; token copies cannot return there. "
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
            lines.append(
                f"Recovery comparison against {label(attacker)}: {label(card)} or "
                f"{', '.join(label(c) for c in alternatives)} would die in a single block under visible combat. "
                "When those blocks have equivalent combat outcomes, prefer losing the commander over a token "
                "if its next recast is affordable and its enters ability rebuilds more value. "
                "For a nontoken-only copy-creation trigger, recasting the original creates fresh copies; "
                "sacrificing an existing token forfeits that opportunity. Check commander tax, colored mana "
                "from the SURVIVING board after untapping, other planned spells, and the delay before new "
                "creatures can tap. Never count the dying commander as a source for its own recast. "
                "Preserve the commander when recasting is impractical; surviving combat takes precedence."
            )
    return lines
