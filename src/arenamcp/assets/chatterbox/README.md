# Chatterbox reference speakers

Atlas uses CMU ARCTIC BDL (male US English); Mira uses CMU ARCTIC SLT
(female US English). These are app display names, not the speakers' names.
Both recordings were made for speech synthesis. No Kokoro audio is used.

Source: http://festvox.org/cmu_arctic/
Authors: John Kominek and Alan W Black, Carnegie Mellon University.
Copyright (c) 2003 Carnegie Mellon University.
Permission and disclaimer: see CMU-LICENSE.txt, distributed alongside the WAVs.

Each file concatenates the original mono, 16-bit, 16 kHz recordings
arctic_a0001.wav, arctic_a0002.wav, and arctic_a0003.wav, without other
processing. Concatenation is the modification made by ArenaOnAir.
Original URLs follow this pattern:
http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_bdl_arctic/wav/arctic_a0001.wav
http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_slt_arctic/wav/arctic_a0001.wav

SHA-256:
- atlas.wav: cef09a538dca0edf415ce8fecf3702da6eec9c4632ed397d5e00f76ce5075a84
- mira.wav: 64f362fb768b2800a0e6c2884a6a79be722c1d3b8987a5be83cee40549022790

Chatterbox extracts and caches speaker conditioning for these clips once per
model instance, then switches that conditioning between commentary turns.
The original model voice remains selectable as "Chatterbox original voice".
