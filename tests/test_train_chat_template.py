"""WP-0.2 accept test: decoded SFT training samples are byte-identical to the
ProxyBackend.complete() wire format (serve-side chat-template render) followed
by the target response and the template's turn-end suffix.

Requires the google/gemma-4-12B-it tokenizer (HF cache or hub). Skipped when
it can't be loaded so the suite stays runnable on machines without the model.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest
from tools.training.formatting import (
    derive_turn_end,
    format_sft_example,
    render_serve_prompt,
)

MODEL_ID = "google/gemma-4-12B-it"

SYSTEM = "You are an expert MTG coach providing real-time advice."
USER = "On the play. Opening hand (7 cards): Mountain, Shock.\nMulligan options: KEEP, MULLIGAN to 6.\n"
RESPONSE = '{"actions": [{"action_type": "mulligan_keep", "reasoning": "two lands, cheap spells"}]}'


@pytest.fixture(scope="module")
def tokenizer():
    transformers = pytest.importorskip("transformers")
    try:
        return transformers.AutoTokenizer.from_pretrained(MODEL_ID)
    except Exception as e:  # no cache and no network
        pytest.skip(f"tokenizer for {MODEL_ID} unavailable: {e}")


def test_sample_starts_with_exact_serve_prompt(tokenizer):
    """The training sample must begin with the byte-exact prompt the server
    renders for ProxyBackend's [system, user] messages."""
    sample = format_sft_example(tokenizer, SYSTEM, USER, RESPONSE)
    serve = render_serve_prompt(tokenizer, SYSTEM, USER)
    assert sample.startswith(serve), (
        "training sample diverges from serve-side prompt render:\n"
        f"serve tail: {serve[-120:]!r}\nsample head after prompt: {sample[: len(serve)][-120:]!r}"
    )


def test_completion_region_is_response_plus_turn_end(tokenizer):
    """Everything after the serve prompt must be exactly the response plus
    the template's turn-end suffix — the tokens the model would generate."""
    sample = format_sft_example(tokenizer, SYSTEM, USER, RESPONSE)
    serve = render_serve_prompt(tokenizer, SYSTEM, USER)
    completion = sample[len(serve) :]
    assert completion == RESPONSE + derive_turn_end(tokenizer)


def test_generation_prompt_channel_priming_is_preserved(tokenizer):
    """gemma-4 primes a pre-closed thought channel in the generation prompt.

    If the template does this, the training sample must contain it too —
    the naive assistant-turn render drops it, shifting every completion off
    the serve-time prefix. This is the regression WP-0.2 exists to catch.
    """
    serve = render_serve_prompt(tokenizer, SYSTEM, USER)
    sample = format_sft_example(tokenizer, SYSTEM, USER, RESPONSE)
    naive = tokenizer.apply_chat_template(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
            {"role": "assistant", "content": RESPONSE},
        ],
        tokenize=False,
    )
    if not naive.startswith(serve):
        # Template drops generation-prompt priming for stored assistant
        # turns; the formatter must have repaired it.
        assert sample.startswith(serve)
        assert sample != naive


def test_turn_end_is_stable_and_nonempty(tokenizer):
    end = derive_turn_end(tokenizer)
    assert end == derive_turn_end(tokenizer)
    assert end.strip() != ""
