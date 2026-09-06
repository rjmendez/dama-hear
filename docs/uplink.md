# Uplink over Meshtastic

## What a node sends

Not audio, not a verdict: a **log-mel sketch** — a coarse spectrogram of the event, quantised to
int8, sized to fit one Meshtastic packet.

```
 4 B  node_us     microseconds within the PPS second (the second comes from the mesh clock)
 2 B  ref_db x4   per-event reference level, so absolute amplitude survives quantisation
 2 B  peak        raw int16 peak, for a clipping check the central side can trust
 1 B  bands       20
 1 B  frames      8
 2 B  flags
160 B sketch      20 mel bands x 8 frames, int8, 0.5 dB per step
----
172 B total, against a 237 B Meshtastic payload
```

## Why a sketch and not a learned embedding

A learned embedding is only as general as the classes it was trained on. Add katydids and it is
stale. A sketch commits to no interpretation: the node ships a coarse spectrogram and the central
side trains whatever it likes on the same bytes, forever. That is what "hard maths elsewhere"
actually buys.

## It is not a downgrade — it is better

Measured on the same 228 operator-labelled events. ⚠️**NESTED** grouped CV — the regularisation
strength is chosen *inside* each outer fold, so the number is not selected on its own test
statistic. The earlier figures on this page were single-level CV with `C` tuned against the
score they reported, and they are superseded:

| | nested AUC | superseded figure |
|---|---|---|
| sketch + the six features together | **0.9668** | — |
| **172 B sketch**, absolute dB | **0.9634** | 0.9631 |
| hand-crafted 6 features | 0.9584 | 0.9589 |
| band/frame summaries (36 dims) | 0.9554 | — |
| sketch shape *without* `ref_db` | 0.9450 | 0.9519 |

⚠️**EVERY NUMBER IN THIS TABLE IS PEAK-ALIGNED AND THE NODE NO LONGER IS.** They were fitted on
sketches whose first frame begins at the envelope PEAK. `hear/node/detect.py` now reports a
constant-fraction ONSET and `pipeline.py` starts the sketch there, so frame 0 is the rise rather
than the blast. The bytes are different and the AUC is unknown until it is refitted on the same
228 hand-labelled events. This applies to the nested figures above as much as to the ones they
replaced — do not quote either against current node code.

⚠️**The sketch does NOT measurably beat the features it replaces.** Paired bootstrap over the 69
groups: sketch − hand features = **+0.0054, 95 % CI [−0.0098, +0.0232]**, P(sketch better) 0.75.
That is a tie, and the previous wording ("the sketch beats the features it replaces") was reading
noise. At n = 228 with 69 independent groups this corpus cannot separate them; more data would
settle it, more features will not.

The case for the sketch was never accuracy:

1. it is what fits in a Meshtastic packet;
2. it commits to no interpretation — the same bytes retrain for cicadas next year;
3. a node cannot reliably compute `rise`/`decay`/`crest`. The envelope work behind those six
   scalars is precisely what does not fit on the node.

**What does not help, measured, so nobody retries it:** round-to-round interval capped at 250 ms
so it cannot encode an operator's pause — 0.9634 → 0.9630, and *alone* it scores AUC **0.32**,
anti-predictive, because short intervals mark retriggers and retriggers are labelled not-shot.
Burst structure carries nothing on this corpus. Uncapped (the leaky `gap`) it is 0.9627, so the
leak was not buying anything either.

⚠️`ref_db` must travel. Amplitude alone reaches 0.90; a sketch normalised per-event and sent
without its reference would discard the single strongest cue this project has measured.

## Airtime

172 B plus Meshtastic framing, at the spreading factors that stay inside the FCC Part 15.247
400 ms dwell limit:

| SF | bitrate | airtime |
|---|---|---|
| 7 | 5470 bps | ~250 ms |
| 8 | 3125 bps | ~440 ms — marginal |
| 9 | 1760 bps | ~780 ms — **exceeds dwell** |

⚠️One sketch per **event** at SF9+ will not fit the dwell limit. Options: send one sketch per
*string* (the loudest round) plus bare timestamps for the rest; drop to SF7; or split the sketch
across two packets and accept the reassembly.

The bare-timestamp fallback is ~8 B per extra round, so a 10-round string is one sketch plus
~72 B — which is the shape the trajectory solver actually needs anyway, since it wants arrival
times from many nodes and spectra from only one.

## Node compute

20 bands x 8 frames of 256-point real FFT is ~0.5 ms on a 64 MHz Cortex-M4F with CMSIS-DSP,
computed once per detection rather than continuously. The nRF52840 in a fakeTec board can do
this; it cannot do 192 kHz capture. Its I2S tops out near 50 kHz -- and cannot produce
48000 Hz at all, since LRCK = MCK/RATIO over a fixed divider list. Use **50.000 kHz**
(MCKFREQ 32MDIV10, RATIO 64X), which is exact. PDM is worse and capped at 16 kHz.
See `docs/faketec-pin-budget.md`.
