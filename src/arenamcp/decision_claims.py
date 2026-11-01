"""Check explicit selected-action contradictions, not the merits of a strategy (#329)."""

import re
from dataclasses import dataclass

from arenamcp.play_safety import find_source


@dataclass(frozen=True)
class ActionClaim:
    reason: str
    # A land remains a legitimate play even when the model calls it a spell.
    land_fact: str = ""


def _text(value):
    return str(value or "").replace("’", "'")


def _selected_face(option, state):
    source = find_source(state, option.meta)
    if not source:
        return {}
    gid = option.meta.get("grpId")
    faces = [source, *(source.get("related_faces") or [])]
    exact = [c for c in faces if gid and c.get("grp_id") == gid]
    return exact[0] if len(exact) == 1 else source if not gid or not source.get("grp_id") else {}


def action_claim_mismatch(decision, state, chosen, reasoning):
    """Require exact source identity and an affirmative current-action claim.

    Cast/hold disagreements are questions for one recheck: protection, life
    gain, cast triggers and library-search modes may justify the same cast.
    They are never evidence that the card is intrinsically a useless play.
    """
    if decision.request_type != "ActionsAvailable" or len(chosen) != 1:
        return None
    option = decision.find(chosen[0])
    if option is None:
        return None
    action = str(option.meta.get("actionType") or "").removeprefix("ActionType_").lower()
    source = _selected_face(option, state)
    name = _text(source.get("name"))
    if not name:
        return None
    text = _text(reasoning)
    if action in {"play", "playmdfc"} and "land" in str(source.get("type_line") or "").lower():
        # "Play this land to cast the commander later" is a future plan, not
        # an assertion that the selected land option itself casts a spell.
        commanders = ["(?:the |our |my )?commander"]
        commanders.extend(re.escape(_text(c["name"])) for c in state.get("command") or [] if c.get("name"))
        immediate_cast = re.search(
            r"(?:^|[.;!?]\s*)(?:(?:I|we) (?:will |can )?)?cast(?:ing)?\s+(?:" + "|".join(commanders) + r")\b",
            text,
            re.I,
        )
        if immediate_cast and not re.match(
            r"\s+(?:next turn\b|later\b|after\b|once\b|if\b)", text[immediate_cast.end() :], re.I
        ):
            fact = f"Play {name} as a land; this selected action does not cast the commander."
            return ActionClaim(
                f"{option.option_id} is the offered land play for {name}, not a commander cast. "
                "Use only offered action IDs. A land play does not create the commander's bodies or "
                "prove it was cast; any later cast must be a separately offered and paid action.",
                fact,
            )
        return None
    if action != "cast":
        return None
    named = re.escape(name)
    # An explicit current cast justification resolves an earlier hold thought.
    # This also preserves holding another copy after casting this one.
    if re.search(r"\bcast(?:ing)?\s+" + named + r"\b", text, re.I):
        return None
    for clause in re.split(r"[.;!?]", text):
        if re.search(r"\b(?:if|could|might|would|don't|do not|not|never|after|then)\b", clause, re.I):
            continue
        if re.match(r"\s*(?:hold(?:ing)?|save|keep)\s+" + named + r"\b", clause, re.I):
            return ActionClaim(
                f"You selected Cast {name}, but the stated reason explicitly holds that same card. "
                f"Its actual rules are: {_text(source.get('oracle_text'))}. "
                "Reconsider the selected action and explain its current value if casting, or select "
                "Pass if holding. Life gain, protection now and cast-trigger value can justify casting; "
                "do not infer that a protection spell must always be held."
            )
    if (
        re.match(r"\s*" + named + r"\b", text, re.I)
        and re.search(r"\b(?:it |this spell )?does nothing\b", text, re.I)
        and re.search(r"[.;]\s*better to (?:attack|pass|hold|wait)\b", text, re.I)
        and not re.search(r"\b(?:if|could|might|would|cast anyway)\b", text, re.I)
    ):
        return ActionClaim(
            f"You selected Cast {name}, but the reason rejects its value and explicitly prefers another action. "
            f"Read all its actual rules before deciding: {_text(source.get('oracle_text'))}. "
            "Reconsider the action and rationale together; an empty hand does not by itself invalidate "
            "a library-search mode or a combination of modes. Keep the cast if its actual effects justify it."
        )
    return None
