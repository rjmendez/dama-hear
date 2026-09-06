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

Measured on the same 228 operator-labelled events, grouped 5-fold CV:

| | AUC | accuracy |
|---|---|---|
| **172 B sketch** | **0.9631** | **0.8816** |
| hand-crafted 6 features | 0.9589 | 0.8684 |
| `ref_db` alone (amplitude) | 0.9007 | — |
| sketch shape *without* `ref_db` | 0.9519 | — |

The sketch beats the features it replaces, and the shape carries strong signal independent of
loudness — which is exactly what a six-scalar summary was throwing away.

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
