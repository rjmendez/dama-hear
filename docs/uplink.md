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
| **172 B sketch**, absolute dB, one-hop onset | **0.9728** | 0.9631 |
| sketch, 15 bands (whole fleet) | 0.9665 | — |
| sketch, peak-aligned | 0.9634 | — |
| hand-crafted 6 features | 0.9584 | 0.9589 |
| band/frame summaries (36 dims) | 0.9554 | — |
| sketch, onset clamped to the 25 ms guard | 0.9443 | — |
| sketch shape *without* `ref_db` | 0.9450 | 0.9519 |

⚠️**WHERE THE SKETCH STARTS IS WORTH MORE THAN ANYTHING ELSE MEASURED HERE.** The gate's onset
is walked back to the 25 ms re-trigger guard, which is right for the TIMESTAMP and wrong for a
33 ms feature window — it slides the window off the event and scores **0.9443**, worse than not
walking back at all. Clamped to one hop it is **0.9732**. That 2.9-point spread is larger than
every representation difference on this page put together, and it is a constant doing two jobs
with one number (`detect.SKETCH_BACK_S`).

⚠️**The onset itself is now referred to the local floor, not to zero.** A round landing inside
the previous round's decay tail never sees its envelope fall to 20 % of the *new* peak, so the
walk ran to the clamp edge and returned it: **42 of 228 events (18.4 %)** timestamped exactly
25 ms early, **8.6 m of range**. Against known synthetic truth at that condition the error goes
from **−21.62 ms to +0.43 ms**. On the real corpus the never-timed count goes 42 → 0, and
**27.6 % of all events move by more than 1 ms**. Cost to the sketch: 0.9732 → **0.9728** at 20 bands and
0.9705 → **0.9665** at 15 — inside the noise band either way, and worth one definition of "onset"
across node, phone and training script. (An earlier draft of this line quoted 0.9712/0.9690;
those were measured before the interior-trough guard and are superseded by the refit.)

### ⚠️ NFFT: the case for shortening it died with the window fix

Re-measured on the same 228 events with the **current onset-aligned window**, nested grouped CV:

| geometry | AUC | wire bytes |
|---|---|---|
| **NFFT 256, 20×8** (shipped) | **0.9728** | 172 |
| NFFT 128, 20×12 | 0.9677 | 252 |
| NFFT 128, 20×8 | 0.9667 | 172 |
| NFFT 128, 15×8 | 0.9663 | 132 |
| NFFT 64, 20×8 | 0.9444 | 172 |

A 2026-09-08 research pass costed NFFT 128/64 at **+0.0104 to +0.0171** and ranked it worth
doing. That gain was measured against the **pre-fix** window, which ended 2 ms *before* the
trigger — with the event at the very edge, finer time resolution helps. Once the window is placed
correctly the frequency resolution of NFFT 256 matters more, and every shorter NFFT is worse.

⚠️ 256 vs 128 is 0.006, inside the ±0.017 noise band, so the honest claim is *not better*, not
*worse*. NFFT 64 at 0.9444 is outside the band and genuinely worse. Either way there is no longer
a measured case for changing NFFT, and it would have cost new profile ids, regenerated goldens,
both ports and a model refit.

⚠️**The sketch cannot resolve a crack's rise, and should stop being asked to.** One analysis
frame is NFFT/fs = 5.33 ms; the measured peak-to-onset distance is a **median of 1.56 ms**. Frame
0 contains the peak at every rise from 1 ms to 20 ms. If the rise must be resolved the lever is
NFFT/HOP_S, not the onset clamp.

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

### ⚠️The table that was here was BW125, and BW125 is not a Meshtastic preset for this fleet

Kept, struck, because the reasoning that produced it will otherwise be produced again:

> | SF | bitrate | airtime |
> |---|---|---|
> | ~~7~~ | ~~5470 bps~~ | ~~\~250 ms~~ |
> | ~~8~~ | ~~3125 bps~~ | ~~\~440 ms — marginal~~ |
> | ~~9~~ | ~~1760 bps~~ | ~~\~780 ms — **exceeds dwell**~~ |

Two things are wrong with it and they compound.

**It is `payload_bits / bitrate`, not LoRa airtime.** 5470/3125/1760 bps are `SF·BW/2^SF · 4/5`
at **BW 125 kHz**, and 172·8 divided by each gives exactly 251.6 / 440.3 / 782.8 ms. That
arithmetic omits the preamble, the explicit header, the CRC and the fact that payload symbols
come in whole `(CR+4)`-sized groups. It therefore **under-reports by 10–17 %**. Recomputed with
the real formula, 172 B, CR 4/5, 16-symbol preamble:

| SF at BW125 | the old row | actually |
|---|---|---|
| 7 | ~250 ms | **284.9 ms** |
| 8 | ~440 ms | **508.4 ms** |
| 9 | ~780 ms | **914.4 ms** |

**And no US Meshtastic preset uses BW 125 with CR 4/5 at those SFs at all.** From
`src/mesh/MeshRadio.h:216-300`, the presets offered in the standard region set are BW 250 or
500 kHz except `LongModerate`/`LongSlow`, which are BW 125 **at CR 4/8**. So the table above
answers a question the fleet never asks. Naming an "SF" without its bandwidth and coding rate is
the whole defect: **SF alone does not determine airtime.**

### The preset table, which is the one that maps onto the radio

Computed at **193 B on-air** — the 173 B v2 frame (`hear/wire.py`) plus Meshtastic's 16 B header
(`src/mesh/RadioInterface.h:21`) and the protobuf `Data` wrapper — with the 16-symbol preamble
Meshtastic actually ships (`src/mesh/RadioInterface.h:106-107`; **8 is the LoRa default and
Meshtastic does not use it**), low-data-rate optimise engaged where `Tsym > 16 ms`:

| preset | SF | BW kHz | CR | airtime | vs. 400 ms dwell |
|---|---:|---:|---|---:|---|
| **ShortTurbo** | 7 | 500 | 4/5 | **78.9 ms** | fits |
| ShortFast | 7 | 250 | 4/5 | 157.8 ms | fits |
| MediumTurbo | 9 | 500 | 4/5 | 254.2 ms | fits |
| ShortSlow | 8 | 250 | 4/5 | 279.8 ms | fits |
| MediumFast | 9 | 250 | 4/5 | 508.4 ms | over |
| MediumSlow | 10 | 250 | 4/5 | 914.4 ms | over |
| LongTurbo | 11 | 500 | 4/8 | 1.30 s | over |
| LongFast *(Meshtastic default)* | 11 | 250 | 4/5 | 1.71 s | over |
| LongModerate | 11 | 125 | 4/8 | 6.10 s | over |
| LongSlow | 12 | 125 | 4/8 | 11.15 s | over |

⚠️The dwell column carries forward the 400 ms FCC Part 15.247 figure this page already used.
Whether a 500 kHz LoRa carrier is regulated as a hopping system or as digital modulation is
**not settled here** and must not be decided from this table.

### What it means

The original conclusion stands and is now much safer: **one sketch per event fits, on
ShortTurbo, with an order of magnitude to spare.** At the operator's 3.5-acre site the longest
baseline is ~170 m and the link margin at SF7/BW500 is ~64 dB, so nothing forces a slower preset
— see `docs/esp32s3-lora-node.md` §7 for the link budget and the duty-cycle arithmetic.

⚠️The default preset is the trap, not the SF. `LongFast` is what a Meshtastic node boots on and
it is **1.71 s per sketch**, 21× ShortTurbo. A node left on defaults will not carry this traffic.

If a slower preset is ever forced (range, not payload): send one sketch per *string* — the
loudest round — plus bare timestamps for the rest. That is ~8 B per extra round, so a 10-round
string is one sketch plus ~72 B, which is the shape the trajectory solver wants anyway, since it
needs arrival times from many nodes and spectra from only one.

## Node compute

20 bands x 8 frames of 256-point real FFT is ~0.5 ms on a 64 MHz Cortex-M4F with CMSIS-DSP,
computed once per detection rather than continuously. The nRF52840 in a fakeTec board can do
this; it cannot do 192 kHz capture. Its I2S tops out near 50 kHz -- and cannot produce
48000 Hz at all, since LRCK = MCK/RATIO over a fixed divider list. Use **50.000 kHz**
(MCKFREQ 32MDIV10, RATIO 64X), which is exact. PDM is worse and capped at 16 kHz.
See `docs/faketec-pin-budget.md`.
