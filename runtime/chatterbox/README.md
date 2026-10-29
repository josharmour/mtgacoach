# Optional, isolated Chatterbox Turbo runtime

Coach's default installation does not include Torch or Chatterbox. The desktop
uses a separate Python process for local synthesis and remains the only playback
owner. Remote Chatterbox does not need this environment or local model weights.
Nothing in this directory is automatically installed or downloaded by Coach.

## Install after live matches finish

Use a separate Python **3.11** environment. Python 3.12 is represented in the
universal dependency lock but still needs Windows packaging/device qualification.
For example (adjust paths on Windows):

```sh
uv venv --python 3.11 ~/venvs/coach-chatterbox
uv pip sync --python ~/venvs/coach-chatterbox/bin/python --require-hashes runtime/chatterbox/requirements.lock
~/venvs/coach-chatterbox/bin/python runtime/chatterbox/prepare_model.py --cache-dir ~/models/coach-chatterbox
```

`requirements.lock` is a complete hash-locked resolution of Chatterbox 0.1.7 and
PyAV 16.0.1, generated with uv 0.11.29. The published 0.1.7 wheel was inspected to
confirm `ChatterboxTurboTTS.from_local(ckpt_dir, device)` exists. Dependencies were
resolved, **not installed or benchmarked** as part of this change.

The downloader pins `ResembleAI/chatterbox-turbo` to
`749d1c1a46eb10492095d68fbcf55691ccf137cd`. Copy its printed snapshot directory into
**Tools → Arena On Air & Voice → Runtime & connections**. The snapshot directory's
name must equal the configured revision. Select the separate Python executable,
the Arena On Air source folder (or install a compatible AOA package in that
interpreter), and CPU/MPS/CUDA. The render worker loads from that local directory
with Hugging Face/Transformers offline mode enabled. It never calls
`from_pretrained` or downloads missing weights.

Atlas and Mira are shipped under `src/arenamcp/assets/chatterbox/` with their CMU
license and modification notes. Both reference conditionings are cached at model
startup. The model default is also selectable. Original/hybrid models and
speculative audio preparation remain off, as the integration plan requires until
qualification. CUDA load OOM gets one CPU fallback; a generation error/timeout
ends the worker and requires an explicit preview to retry. Essential messages
bypass Chatterbox for warm Kokoro/system speech.

## Explicit benchmark, without playback

Do not run this alongside the current ranked matches. After setup:

```sh
python runtime/chatterbox/benchmark.py \
  --python ~/venvs/coach-chatterbox/bin/python \
  --aoa-source ~/repos/arenaonair \
  --model-dir ~/models/coach-chatterbox/models--ResembleAI--chatterbox-turbo/snapshots/749d1c1a46eb10492095d68fbcf55691ccf137cd \
  --device cpu > /tmp/aoa-local-benchmark.json
```

This records cold/warm requests, both voices, a longer line, worker timing,
process peak RSS when available, and CUDA peak allocated memory. It creates no
playback stream. Cold initialization has a 90-second ceiling; each synthesis has
a six-second ceiling and can be cancelled by essential speech. A slow system
fails qualification instead of silently queuing more synthesis. MPS device memory,
Windows process memory, frame time, and shared GPU utilization require external
platform measurements. These metrics are not inferred from CPU timings.

For provenance, see the [published package](https://pypi.org/project/chatterbox-tts/0.1.7/),
[upstream implementation](https://github.com/resemble-ai/chatterbox/blob/master/src/chatterbox/tts_turbo.py),
and [pinned model](https://huggingface.co/ResembleAI/chatterbox-turbo/tree/749d1c1a46eb10492095d68fbcf55691ccf137cd).
