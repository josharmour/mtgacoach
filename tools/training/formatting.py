"""Chat-template formatting shared by train.py and the parity tests (WP-0.2).

The training sample for an (system, user, response) SFT example must be
byte-identical to what production serving sees: ProxyBackend.complete() sends
``[{role: system}, {role: user}]`` and the inference server renders it with
the model's chat template using ``add_generation_prompt=True``. The loss is
then taken over exactly the tokens the model would generate at serve time:
``response`` followed by the template's turn-end suffix.

Rendering the assistant turn through ``apply_chat_template`` directly is NOT
equivalent for every template: gemma-4's template primes a pre-closed empty
thought channel (``<|turn>model\\n<|channel>thought\\n<channel|>``) in the
generation prompt but omits it when re-rendering a stored assistant message,
which silently shifts the whole completion off the serve-time token prefix.
So we anchor on the generation-prompt render and splice the response onto it.
"""

from __future__ import annotations

_SENTINEL = "\x00TURN_END_SENTINEL\x00"


def derive_turn_end(tokenizer) -> str:
    """Return the text the template appends after an assistant message.

    Rendered from a sentinel assistant turn so it works for any template
    without hardcoding model-specific control tokens.
    """
    full = tokenizer.apply_chat_template(
        [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": _SENTINEL},
        ],
        tokenize=False,
    )
    if not full or _SENTINEL not in full:
        return getattr(tokenizer, "eos_token", None) or ""
    return full.split(_SENTINEL, 1)[1]


def render_serve_prompt(tokenizer, system: str, user: str) -> str:
    """Render exactly what the inference server feeds the model for a
    ProxyBackend.complete(system, user) call."""
    return tokenizer.apply_chat_template(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tokenize=False,
        add_generation_prompt=True,
    )


def format_sft_example(tokenizer, system: str, user: str, response: str) -> str:
    """Build one SFT training sample with serve-time byte parity."""
    if not hasattr(tokenizer, "apply_chat_template"):
        return (
            f"<system>\n{system}\n</system>\n<user>\n{user}\n</user>\n<assistant>\n{response}\n</assistant>"
        )

    prompt = render_serve_prompt(tokenizer, system, user)
    full = tokenizer.apply_chat_template(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": response},
        ],
        tokenize=False,
    )
    if full.startswith(prompt):
        return full
    return prompt + response + derive_turn_end(tokenizer)
