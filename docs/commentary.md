# Commentary and speech development

The desktop’s **Speech & voices** controls select spoken Coach advice or two ArenaOnAir announcers. The commentary engine consumes observed game events and available coaching explanations. Player decisions and urgent prompts take priority over background narration. Local Chatterbox has a separate [runtime setup](../runtime/chatterbox/README.md).

## Broadcast behavior

There are two styles: **Hype** and **Technical**. Both aim to be interesting,
funny and informative. Hype uses playful reactions and stronger delivery on
consequential events; Technical uses measured delivery and dry wit. Old Esports
preferences migrate to Hype; Classic, Test-match and Pro Tour migrate to
Technical. The selected voice pair stays independent of style.

The play-by-play caster orients the audience to a change and can ask a specific
question that the analyst answers in the same exchange. The analyst develops
both players' routes to winning: pressure versus stabilization, resources,
interaction windows, engines and changes in the race. Humor should arise from
the position and conversational rapport, without invented facts or recurring
catchphrases. A card-rule explanation or isolated action motive alone is not a
strategic insight. No particular phrase is blacklisted.

Generation includes a compact view of both battlefields, the public game read,
card rules and, with the hand/deck option, the existing deck plan. Sixteen
successfully delivered analyst lines are retained independently of the last 24
spoken lines. The prompt reserves up to 900 bytes for the most recent analyst
ideas before allocating space to card rules. The audit checks factual support
and whether the analyst adds a fresh game-level implication. Revisiting a point
is useful when a premise or consequence changes; paraphrasing it is not. These
are model judgments, not a guarantee of entertaining or always-correct analysis.

Fallback commentary uses rule-grounded interactions and position observations;
it does not read out a saved action rationale as a substitute for analysis.
Repeated fallback insights are suppressed using the analyst history. Silence is
preferable to filling the second voice with an old point. Finished commentary
is recorded in the existing Coach log as `Commentary spoken`, separately from
proposals and failed/interrupted playback, so actual output can be reviewed.

Excitement now survives worker IPC and desktop scheduling. Technical caps
delivery at normal; Hype allows electric calls and tense analyst replies.
Chatterbox receives the explicit tier and energy (0.3/0.5/0.65/0.8). The server's
installed engine determines whether that uses expressive Original synthesis or
Turbo's punctuation/cadence treatment. Local Turbo receives the tier through
its native utterance. Kokoro has no emotion parameter, so it uses modest
cadence changes and punctuation. Speed remains the user-selected base; pitch
and volume are not artificially boosted.

## Runtime requirement

Generated exchanges require the independent `arenaonair` core in the commentary
worker's Python environment (or an explicitly configured source checkout).
Coach's current managed packaging does not include that dependency. Without it,
the two style controls, voice delivery and built-in strategic fallback work,
but the generated analyst's semantic freshness audit and conversational humor
are unavailable. A release advertising generated dual-caster analysis must
include and verify a pinned core runtime; local Torch/Chatterbox extras are not
needed when speech uses Kokoro or the hosted service.

## Coverage research and local evidence

The [Pro Tour Lorwyn Eclipsed Top 8 transcript](https://www.twitchtranscripts.com/channel/magic/2686225791)
includes the Larsen–Sánchez quarterfinal and Larsen–Portolan final. Its automatic
transcription contains name/card errors; use it for conversational structure,
not card rules. The booth alternates short calls with questions, implications,
corrections and callbacks. It follows changing win conditions and interaction
constraints across turns instead of introducing every card afresh. Situational
humor occurs around that shared analysis. The
[official finals coverage](https://magic.gg/news/the-finals-of-pro-tour-lorwyn-eclipsed)
corroborates the changing pressure, resources and interaction windows.

Three saved local ArenaOnAir transcripts from October 28, 2026 showed analyst
lines frequently reciting individual abilities (prowess, damage, cast and entry
triggers) without carrying a strategic thread forward. November 4 Coach logs
recorded delivery outcomes but not line text. The changes above target that
content/context failure rather than banning a symptom sentence. Synthetic
conversation tests do not establish live audience enjoyment or audio quality.

| Feature | Code or check |
| --- | --- |
| Event context, commentary generation, and scheduling | [Commentary package](../src/arenamcp/commentary/) |
| Hosted trial access and match accounting | [ArenaOnAir service](../website/ARENAONAIR.md) |
| Comparing commentary off and on | [Qualification evaluator](../src/arenamcp/commentary/qualification.py) |
| Desktop speech controls and announcer behavior | [Speech settings](images/aoa-simple-speech.png) and [two-announcer example](images/aoa-two-announcers.png) |

## Qualify a change

Run the [ArenaOnAir tests](../tests/aoa/) and the tests for the speech engine or UI you changed. For a timing change, record the same replay, platform, strategy setting, and load with commentary off and on. Supply at least 20 matching ordered decisions with latency, timeout, and fallback outcomes, plus the enabled run’s speech and stale-utterance measurements required by the evaluator.

```sh
python -m arenamcp.commentary.qualification baseline.json commentary.json
```

Record the actual audio device, speech backend, and platform when checking playback, interruption, or contention. A simulated replay cannot establish live device or shared inference capacity. [Original acceptance evidence](https://github.com/josharmour/mtgacoach/blob/adbaca97f9076b8ee8be86689154e67410203911/docs/aoa-integration-progress.md)
