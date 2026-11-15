"""Read-only coverage of one private deck study, bound to a single model's weights."""

import math
import re

from arenamcp.deck_card_study import prepare_card_pass
from arenamcp.deck_local_guidance import TOPICS, _guidance_input, topic_coverage
from arenamcp.deck_local_interactions import interaction_coverage
from arenamcp.deck_study import coverage


def local_report(book, state, directory, digest):
    saved = coverage(book.data, book.catalog)
    lines = [
        "Deck study coverage",
        f"Saved playbook • {saved['legacy_nonland_roles']} of {saved['nonland_cards']} known nonland prints have earlier role notes. {saved['unresolved_types']} printed types unresolved.",
    ]
    if digest is None or digest == "":
        lines.append(
            "Choose local model weights to measure their saved card, interaction and strategy passes."
        )
        return "\n".join(lines)
    if not isinstance(digest, str) or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", digest):
        raise ValueError("Invalid local model weights identity")
    digest = digest.removeprefix("sha256:")
    lines += ["", f"Selected model weights • {digest[:12]}"]
    try:
        cards = prepare_card_pass(book, directory / "local_card_studies", digest)
    except (ValueError, TypeError, KeyError, OSError):
        cards = None
        lines.append(
            "Card-role coverage withheld because the saved study and current prints could not be verified."
        )
    if cards is not None:
        lines += [
            f"Current card roles • Local hypotheses cover {cards['cached']} of {cards['nonlands']} known nonland cards. Structurally validated saved entries cover {cards['admitted']} of {cards['nonlands']}.",
            f"{len(cards['pending'])} remaining local roles, {cards['withheld']} withheld on print checks, {cards.get('unsupported_payment', 0)} unsupported commander payments.",
        ]
    try:
        current, context = _guidance_input(book, state, "interactions")
        folder = directory / "local_deck_guidance"
        links, usage = interaction_coverage(current, context, folder, digest)
        lines.extend(
            [
                "",
                f"Relationships • {links['reviewed_prints']} of {len(current.catalog)} current prints have independently reviewed relationship records. "
                f"{links['covered_links']} of {links['links']} reviewed in-deck links occur in local interactions, {links['minor_links']} marked minor.",
                f"Reviewed groups studied • {links['complete_groups']} of {links['groups']}. Whole-deck search • {'complete' if links['discovery'] else 'pending'}.",
                f"Local proposals • {links['synergies']} synergies, {links['combos']} combos.",
            ]
        )
        for scope, label in (("sequencing", "Sequencing and recovery"), ("switches", "Opponent switches")):
            hits, metrics = topic_coverage(
                current, {**context, "topics": list(TOPICS[scope])}, scope, folder, digest
            )
            usage.extend(metrics)
            lines.append(
                f"{label} • {len(hits)} of {len(TOPICS[scope])} topics"
                + (f" ({', '.join(TOPICS[scope][k] for k in TOPICS[scope] if k in hits)})" if hits else "")
                + "."
            )
    except (ValueError, TypeError, KeyError, OSError):
        usage = []
        lines.append("Full-deck sections withheld because their current source data could not be verified.")
    cached = cards["cached"] if cards else 0
    total = cached + len(usage)
    lines += ["", f"Verified cached model passes • {total}"]
    if total:
        for field, unit, missing in (
            ("seconds", "seconds of build time", "unreported_seconds"),
            ("generated_tokens", "generated tokens", "unreported_generated"),
            ("thinking_tokens", "thinking tokens", "unreported_thinking"),
        ):
            values = []
            for record in usage:
                n = record.get(field) if isinstance(record, dict) else None
                if (
                    type(n) is int or (field == "seconds" and type(n) is float and math.isfinite(n))
                ) and n >= 0:
                    values.append(n)
            recorded = cached - (cards.get(missing, 0) if cards else 0) + len(values)
            value = (cards.get(field, 0) if cards else 0) + sum(values)
            if recorded:
                lines.append(f"{round(value, 1)} reported {unit} from {recorded} of {total} passes.")
            else:
                lines.append(f"{unit.capitalize()} unreported for all {total} passes.")
    lines.append(
        "Usage covers retained, validated local passes. Strategic judgments still require semantic review."
    )
    return "\n".join(lines)
