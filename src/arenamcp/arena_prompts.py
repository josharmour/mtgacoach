"""Arena's own words for a GRE request: the prompt id's text from the MTGA card DB.

Ledger #79 (2026-10-19, match 772233af T3, 11:35:23): Unscrupulous Agent's
"target opponent exiles a card from their hand" reached us as a SelectNReq over
our six hand cards with ``prompt.promptId`` 1117 and ``sourceId`` 353. 1117 is
"Exile a card." in the card DB's ``Prompts`` table, but the id was dropped and
the decision showed no source, so the model read the list as "pick a spell to
cast" and exiled Shock ("Shock is the cheapest efficient answer").

The text is an ICU message: ``{1, CardId}`` names the card in the prompt's first
parameter, ``{2, Number}`` is a count and ``{2, plural, =1 {...} other {...}}``
picks the singular or plural sentence. Parameters are numbered from 1 in the
order the request lists them. Accepts the log's shape (``numberValue``), the
native Mac bridge's (``value`` + ``valueCase``) and the BepInEx plugin's
reflected PascalCase one. Pure apart from the card DB lookup, which uses only an
already-initialized database (never loads one inside a decision window).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _field(container: dict, *names: str) -> Any:
    for name in names:
        if name in container and container[name] is not None:
            return container[name]
    return None


def prompt_id(prompt: Any) -> int:
    """The prompt's ``promptId`` (0 when absent or not a structured prompt)."""
    if not isinstance(prompt, dict):
        return 0
    try:
        return int(_field(prompt, "promptId", "PromptId", "prompt_id") or 0)
    except (TypeError, ValueError):
        return 0


def prompt_parameters(prompt: Any) -> dict[int, tuple[str, int | None]]:
    """{1-based position: (parameter name, numeric value or None)} of a structured prompt."""
    out: dict[int, tuple[str, int | None]] = {}
    if not isinstance(prompt, dict):
        return out
    for position, param in enumerate(_field(prompt, "parameters", "Parameters") or [], start=1):
        if not isinstance(param, dict):
            continue
        name = str(_field(param, "parameterName", "ParameterName") or "")
        raw = _field(param, "numberValue", "NumberValue", "value", "Value")
        try:
            value = int(raw) if raw is not None and not isinstance(raw, bool) else None
        except (TypeError, ValueError):
            value = None
        out[position] = (name, value)
    return out


def _closing(text: str, start: int) -> int:
    """Index of the brace closing the one at ``start``, or -1."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _branches(text: str) -> dict[str, str]:
    """'=1 {a card.} other {{1, Number} cards.}' -> {'=1': 'a card.', 'other': '{1, Number} cards.'}."""
    out: dict[str, str] = {}
    index = 0
    while index < len(text):
        brace = text.find("{", index)
        if brace < 0:
            break
        end = _closing(text, brace)
        if end < 0:
            break
        out[text[index:brace].strip()] = text[brace + 1 : end]
        index = end + 1
    return out


def render_prompt(
    template: str,
    parameters: dict[int, tuple[str, int | None]],
    resolve_instance: Callable[[int], str] | None = None,
) -> str:
    """Fill an ICU prompt template from the request's parameters (unknown parts read generically)."""

    def placeholder(body: str) -> str:
        head, _, rest = body.partition(",")
        try:
            position = int(head.strip())
        except ValueError:
            return ""
        kind, _, options = rest.strip().partition(",")
        kind = kind.strip().lower()
        value = parameters.get(position, ("", None))[1]
        if kind == "cardid":
            name = resolve_instance(value) if resolve_instance and value else ""
            return name or "a card"
        if kind == "number":
            return str(value) if value is not None else "some"
        if kind in ("plural", "select"):
            choices = _branches(options)
            chosen = choices.get(f"={value}") if value is not None else None
            if chosen is None:
                chosen = choices.get("other") if value not in (None, 1) else None
            if chosen is None:
                chosen = choices.get("=1", choices.get("other", ""))
            return render(chosen)
        return ""

    def render(text: str) -> str:
        out: list[str] = []
        index = 0
        while index < len(text):
            if text[index] == "{":
                end = _closing(text, index)
                if end < 0:
                    out.append(text[index:])
                    break
                out.append(placeholder(text[index + 1 : end]))
                index = end + 1
            else:
                out.append(text[index])
                index += 1
        return "".join(out)

    text = render(re.sub(r"<[^>]*>", "", template or ""))
    return " ".join(text.split())


def _lookup_template(identifier: int) -> str:
    """The raw template from the already-loaded card DB ('' when unknown or not loaded)."""
    try:
        from arenamcp import card_db

        db = card_db._card_db
        getter = getattr(db, "get_prompt_text", None) if db is not None else None
        return str(getter(identifier) or "") if callable(getter) else ""
    except Exception as error:  # a missing prompt text must never break a decision
        logger.debug("prompt %s lookup failed: %s", identifier, error)
        return ""


def prompt_text(prompt: Any, resolve_instance: Callable[[int], str] | None = None) -> str:
    """Arena's text for a structured GRE prompt, parameters filled; '' when it is unknown.

    Ids whose text Arena marks deprecated ("#Deprecated PromptMessage_...", id 1)
    say nothing and come back ''.
    """
    identifier = prompt_id(prompt)
    if identifier <= 0:
        return ""
    template = _lookup_template(identifier)
    if not template or template.lstrip().startswith("#"):
        return ""
    try:
        return render_prompt(template, prompt_parameters(prompt), resolve_instance)
    except Exception as error:
        logger.debug("prompt %s render failed: %s", identifier, error)
        return ""


def request_prompt(*containers: Any) -> dict | None:
    """The first structured prompt (one with a promptId) among the request containers given."""
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("prompt", "Prompt"):
            prompt = container.get(key)
            if isinstance(prompt, dict) and prompt_id(prompt) > 0:
                return prompt
    return None
