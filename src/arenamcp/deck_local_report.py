"""Read-only coverage of one private deck study, bound to a single model's weights."""

import math
import re

from arenamcp.deck_card_study import prepare_card_pass
from arenamcp.deck_local_guidance import TOPICS, _guidance_input, retained_chapter
from arenamcp.deck_local_interactions import retained_interactions
from arenamcp.deck_study import coverage
from arenamcp.deck_study_assembly import assemble, save_review_copy


def study_report(book, state, directory, digest, *, save_review=False, current_selection=None):
    saved = coverage(book.data, book.catalog)
    lines = [
        "Deck study coverage",
        f"Saved playbook • {saved['legacy_nonland_roles']} of {saved['nonland_cards']} known nonland prints have earlier role notes. {saved['unresolved_types']} printed types unresolved.",
    ]
    if hasattr(book, "native_profile"):
        lines[1] = (
            f"Current Arena prints • No saved deck strategy. {saved['nonland_cards']} known nonland prints, {saved['unresolved_types']} printed types unresolved."
        )
    if digest is None or digest == "":
        lines.append(
            "Choose local model weights to measure their saved card, interaction and strategy passes."
        )
        return {"detail": "\n".join(lines), "review_ready": False}
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
    ready = False
    try:
        current, context = _guidance_input(book, state, "interactions")
        folder = directory / "local_deck_guidance"
        links, usage, parts = retained_interactions(current, context, folder, digest)
        chapters = {}
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
            hits, metrics = retained_chapter(
                current, {**context, "topics": list(TOPICS[scope])}, scope, folder, digest
            )
            chapters[scope] = hits
            usage.extend(metrics)
            lines.append(
                f"{label} • {len(hits)} of {len(TOPICS[scope])} topics"
                + (f" ({', '.join(TOPICS[scope][k] for k in TOPICS[scope] if k in hits)})" if hits else "")
                + "."
            )
        if (
            cards
            and not cards["unresolved"]
            and not cards["withheld"]
            and not cards["pending"]
            and not cards.get("unsupported_payment")
            and links["discovery"]
            and all(set(rows) == set(TOPICS[scope]) for scope, rows in chapters.items())
        ):
            try:
                packet = assemble(book, current, context, directory, digest, links, parts, chapters, usage)
                ready = True
                jobs = packet["jobs"]
                thinking = sum(
                    job.get("thinking_mode") in ("enabled", "low", "medium", "high", "xhigh") for job in jobs
                )
                lines += [
                    "",
                    "All local study passes assembled for review.",
                    f"Explicit thinking requested in {thinking} of {len(jobs)} retained passes. Independent strategic review is pending.",
                ]
                if save_review:
                    target = save_review_copy(
                        packet, directory, current=current, current_selection=current_selection
                    )
                    lines.append(f"Saved a private review copy on this device at {target}.")
            except (ValueError, TypeError, KeyError, OSError) as error:
                ready = False
                lines.append(f"Local review copy withheld. {error}")
        else:
            lines.append("Local review copy pending complete current card, interaction and chapter coverage.")
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
    return {"detail": "\n".join(lines), "review_ready": ready}


def local_report(book, state, directory, digest):
    return study_report(book, state, directory, digest)["detail"]
