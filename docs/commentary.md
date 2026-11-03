# Commentary and speech development

The desktop’s **Speech & voices** controls select spoken Coach advice or two ArenaOnAir announcers. The commentary engine consumes observed game events and available coaching explanations. Player decisions and urgent prompts take priority over background narration. Local Chatterbox has a separate [runtime setup](../runtime/chatterbox/README.md).

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
