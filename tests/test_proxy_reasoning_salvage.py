"""Chain-of-thought must never be spoken as advice.

Field report ``~/.arenamcp/bug_reports/bug_20260729_225652.json``: at the start
of a turn the coach said

    "Alternatively, we could just pass. But we have a land drop available and a
     shell we can cast."

referring to an alternative the user was never told about. ``standalone.log``
line 18015 shows why::

    22:56:33 | WARNING | arenamcp.backends.proxy | [PROXY] Empty streamed
    content — using answer salvaged from reasoning text (286 chars)

deepseek-v4-flash streamed empty ``content`` with the text in
``reasoning_content``. ``_salvage_reasoning_answer`` took the last paragraph of
286 chars of deliberation, and ``_COT_MARKERS`` had no entry starting with
"alternatively", so the fragment passed the guard and reached the TTS.
"""

from __future__ import annotations

import pytest

from arenamcp.backends.proxy import _salvage_reasoning_answer

# Verbatim from the field report's advice_history[1].
FIELD_REPORT_ADVICE = (
    "Alternatively, we could just pass. But we have a land drop available and a shell we can cast."
)


def test_field_report_regression_orphan_alternative_is_rejected():
    """THE regression test for bug_20260729_225652.

    Reconstructs the shape the salvage function actually received: a first
    paragraph of deliberation followed by the 93-char paragraph that was spoken.
    """
    reasoning = (
        "The board is empty and we're on turn 3 with one Forest untapped. "
        "Ozolith wants counters and Michelangelo is our payoff, so tempo matters here.\n\n"
        + FIELD_REPORT_ADVICE
    )
    assert _salvage_reasoning_answer(reasoning) == "", (
        "advice must never open by referring to an alternative the user never heard"
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "Alternatively, we could just pass.",
        "However, holding the removal is better here.",
        "But we have a land drop available.",
        "On the other hand, attacking loses the Ozolith.",
        "Instead, cast the two-drop.",
        "That said, blocking costs us the creature.",
        "Or we could float mana and wait.",
        "And attack with everything.",
        "Which means we should hold priority.",
        "Then again, they might have a counterspell.",
    ],
)
def test_orphan_reference_openers_are_rejected(candidate):
    """Each of these presupposes something the listener never heard."""
    reasoning = f"Weighing the options for this turn.\n\n{candidate}"
    assert _salvage_reasoning_answer(reasoning) == "", f"should reject: {candidate!r}"


@pytest.mark.parametrize(
    "candidate",
    [
        "Play Forest, then cast Ozolith to start banking counters.",
        "Attack with both creatures — they cannot block profitably.",
        "Hold the removal for their four-drop.",
        "Cast Michelangelo's Technique targeting Turtle Van.",
        "Pass and keep mana up for the instant.",
    ],
)
def test_real_advice_still_salvaged(candidate):
    """The guard must not eat legitimate final answers.

    Over-rejection is the cheaper failure but it is not free: it degrades the
    turn to deterministic fallback advice, which reads like bare legal-action
    strings.
    """
    reasoning = f"Considering the board and our curve.\n\n{candidate}"
    assert _salvage_reasoning_answer(reasoning) == candidate


def test_closed_think_block_still_yields_the_answer():
    """Pre-existing behaviour must not regress: tagged CoT is stripped."""
    reasoning = "<think>Hmm, maybe pass? No — tempo.</think>\nPlay Forest and cast Ozolith."
    assert _salvage_reasoning_answer(reasoning) == "Play Forest and cast Ozolith."


def test_unclosed_think_block_rejected():
    assert _salvage_reasoning_answer("<think>Let me weigh the lines and") == ""


def test_deliberation_opener_still_rejected():
    """The original _COT_MARKERS path must keep working."""
    reasoning = "Sizing up the board.\n\nLet me check whether they can block."
    assert _salvage_reasoning_answer(reasoning) == ""


def test_empty_and_whitespace():
    assert _salvage_reasoning_answer("") == ""
    assert _salvage_reasoning_answer("   \n\n  ") == ""
