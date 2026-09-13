# Module: bioacoustic

Cicadas and katydids — the original reason for putting microphone arrays on the robot.

It is a separate module rather than a mode of the supersonic one because the signal and the
geometry genuinely differ:

| | supersonic | cicada / katydid |
|---|---|---|
| signal | impulsive, broadband, ~100–300 µs | sustained, narrowband, tonal, periodic |
| source | moving, radiates off a Mach cone | stationary point |
| detector | level gate + impulse shape | band energy + periodicity |
| solver | `hear/solve/shockwave.py` | `hear/solve/point.py` — plain TDoA hyperbolae |
| output | line of fire, miss distance | position, and with it a census |

What carries over unchanged: PPS-disciplined per-node time, the survey and local ENU frame, the
labelling loop with active-learning triage, and the classifier training pipeline.

## What a sustained tonal source makes easier — and what it does not

It is periodic, so you can integrate over many cycles instead of living or dying on a single
100 µs transient. That buys **timing**: an arrival time estimated from a second of correlated
signal beats one estimated from one edge, so sample rate stops being the binding constraint *on
the time of arrival*, which is where it binds the supersonic module hardest.

⚠️**It buys nothing on bandwidth, and this README used to imply otherwise.** The old wording —
"sample rate stops being the binding constraint" — was true of timing and read as a claim about
the band. It is not one. Integration improves the estimate of energy that reached the anti-alias
filter; it cannot recover energy the filter removed. Two ceilings apply and the lower one always
wins:

- **Nyquist.** The node samples at 16 kHz, so there is nothing above **8 kHz** in the data at all.
  No detector, no integration time and no amount of averaging puts it back.
- **The microphone.** The planned I2S part, the ICS-43434 (`docs/node-hardware.md`, "What the
  ICS-43434 can and cannot do"), is 50 Hz – 15 kHz and low-passed above 24 kHz: **no ultrasonic
  content at any sample rate**. Re-clocking the part does not change this; only a different part
  does.

So:

| | reachable? | why |
|---|---|---|
| cicadas, 4–8 kHz | **yes** — but 8 kHz *is* the Nyquist wall | inside both ceilings, just |
| crickets and the low katydids, 3–8 kHz | **yes** | inside both ceilings |
| ultrasonic katydids, 15–60 kHz | **no** | above Nyquist at 16 kHz, and above the ICS-43434 at any rate |

`detect.band_limit()` returns which of the two ceilings bound a requested band, because the
remedies differ: `"nyquist"` is fixed by sampling faster, `"microphone"` is not fixed by anything
short of a new part. `TonalGate` refuses a band that is entirely unreachable rather than
returning silence.

Two things are reported about walls, and they are **not** the same kind of fact:

| | scope | what it says |
|---|---|---|
| `TonalGate.band_truncated`, and `limit` on each event | the **gate**, fixed at construction | the band you asked for did not fit under the ceilings, and which ceiling bit |
| `at_band_edge` / `edge` on each event | the **event** | *this* event's spectral peak sits within one edge-width of the band's own wall |

The distinction is not pedantry. `band_truncated` used to be copied onto every event as
`band_limited` and read as a per-event finding, but at the shipped band (4–8 kHz) and rate
(16 kHz) `f_hi` is *always* clamped to 7840 Hz — so it was `True` on every event the gate could
ever emit. A flag that cannot be false carries no information; it is a property of the
configuration, and it now lives there.

## Contents

- `detect.py` — `TonalGate`, a streaming detector for sustained band-limited structured sound.
  Band energy against a tracked in-band floor, gated on duration and on structure: spectral
  flatness (narrowband) *or* envelope autocorrelation (pulsed). Reports a confidence and says
  when it is at the edge of what the hardware can see.

## Why not `hear/node/detect.py` with different numbers

Measured on the 2026-09-07 capture (`~/dama-hear-capture-2026-09-07/final/`). It ran **12.08 h**:
`health.csv` has 1450 rows, its `utc_us` spans 07:16:23.7 → 19:21:15.9 UTC and its `uptime_s` runs
28 s → 43521 s, which is 12.081 h either way. That gate's floor is 800 counts against a median
`ambient` of **28.95** over those 1450 rows — 20·log₁₀(800/28.95) = **28.8 dB** over the
background, and 20·log₁₀(800/84.11) = **19.6 dB** over ambient p95 (84.11). A chorus a plausible
10 dB over ambient sits ~19 dB below it and cannot fire it at any hour.

Its envelope is also broadband, and the capture's own sketches price that. **Recipe, so the numbers
below can be checked rather than believed:** `dets.csv` holds 62 rows. 14 carry `uptime_s == 12` —
the boot-time burst — and 11 of those 14 also set flags bit 1 (*insufficient context*: the sketch
is of a ring that had not filled). Dropping the 14 leaves the **48 in-run detections**, and on
this file the two filters agree: all 48 survivors have `flags == 0`. Decode each `frame_hex` with
`hear.sketch.unpack`, take the `db` field (20 bands × 8 frames) and reduce each band to its
**maximum over the 8 frames**. Then:

- **All 48 peak in mel band 0**, and band 0 exceeds band 1 in **48 of 48** (median 5.50 dB) — the
  spectrum is still climbing where the filterbank stops. Band 0's only nonzero rfft bins at
  fs = 16 kHz, nfft = 256 are **312.5–500 Hz** (bins 5–8; its mel triangle nominally spans
  300.0–526.6 Hz). That is as precisely as the peak can be located: a peak in band 0 is equally
  consistent with a source *inside* 312–500 Hz and with one below 300 Hz that the filterbank
  cannot see. The conclusion that survives is the weaker one — **the energy sat at the bottom edge
  of what the node represents**.
- **Band 0 runs a median 24.6 dB above the 4–8 kHz bands**, where "the 4–8 kHz bands" means the
  mean of bands 16–19, the four whose triangular support lies entirely above 4 kHz (mel edges
  4425–7840 Hz, nonzero bins 4437.5–7812.5 Hz).

The reduction moves the dB but not the count: per-band *mean* over frames instead of max still
puts all 48 in band 0, at 17.6 dB, and comparing against the *loudest* of bands 16–19 rather than
their mean gives 19.0 dB under the max reduction and 14.0 dB under the mean. Earlier revisions of
this file claimed "all 45 … a median 22.3 dB" and then "46 of the 48 … 20.4 dB", with "45 of 48 …
16.5 dB" and "17.2 dB" for the two alternative reductions. None of those five figures reproduces
under any reduction tried, and the row-count recipe they rested on ("59 rows; 11 set flags bit 1
… leaving 48") never worked either: the file has 62 rows and 62 − 11 = 51.

So the insect band is masked before the threshold is consulted. And the gate triggers on peak
amplitude, which a low-crest-factor sustained sound does not produce. Three independent reasons;
none is fixed by retuning `floor`.

## What the caller is on the hook for

`TonalGate.process(block, block_start)` takes the **absolute** index of `block[0]`, and honours it
on every call. This is a contract, not a convenience:

- A `block_start` **ahead** of where the last block ended is a hole in the stream. The gate
  force-closes any open run with `end_reason "stream_gap"` (so its `duration_s` is a bound, and
  `truncated` is `True`), throws away the residual samples it can no longer frame against the new
  ones, re-anchors, and counts the hole in `n_gaps` / `n_gap_samples` / `n_discarded_samples`.
  **A gap never passes silently**, because a dropped block used to shift every subsequent `index`,
  `t_s` and `end_index` with nothing in the output saying so — and holes are not hypothetical: the
  2026-09-07 capture had 20 one-second windows come up short (`health.csv`, last row: `drop_s=20`,
  `drop_samples=57597`, both monotonic over the file) — **3.60 s** of audio at 16 kHz, not 20 s —
  over 12.08 h. (An earlier revision quoted `drop_s=18` / `drop_samples=42749` / 2.7 s as the
  final values. That pair is real but mid-run: it first appears 1259 rows in, at `uptime_s=37818`
  of 43521.)
- A `block_start` **behind** it raises `ValueError`. Overlapping blocks offer two values for one
  sample and picking one is an invention.

## Two numbers that are about the detector, not about the site

- **`n_unstructured` counts stretches; `n_unstructured_segments` counts discards.** A shapeless
  sound longer than `max_duration_s` is chopped into segments and each is judged separately, so a
  60 s wind gust scores 6 at the shipped 10 s limit and 3 at 20 s (measured on 60 s of
  `_noise(seed=8) + _band_noise(gain=8.0)` after 3 s of warm-up; `n_unstructured` stays 1 for
  both, and the tests pin the same signal at 25 s, where it is 3 and 2). Only the stretch count is
  a statement about the site. Emitted events carry `continues` for the same reason: a chopped song
  must be merged before it is counted.
- **`floor_rise_db` replaced a `floor_absorbed` flag that could never fire.** The flag claimed to
  report a song being absorbed into the rising floor, but at the shipped `floor_tau_up_s` (300 s)
  and `max_duration_s` (10 s) the up-limb closes only 3.27 % of the gap before the run is
  force-closed anyway — clearing `close_hyst_db` would take 91.7 dB of in-band SNR. Measured on
  3 s of noise then 30 s of a buzz at the shipped defaults, the replacement number `floor_rise_db`
  comes to 0.41/0.39/0.38 dB across the three segments at 12 dB SNR, and still only
  2.96/2.87/2.76 dB at a physically absurd 90 dB — under the 3.0 dB it would have to clear. What
  absorption really looks like is a
  *sequence*: successive segments whose `floor_db` climbs and whose `snr_db` shrinks (measured at
  the defaults on a 25 s 9 dB buzz: 79.40 → 79.72 → 80.02 dB, and 9.55 → 9.25 → 8.92 dB). There is
  no single event on which it can be seen, so no single event claims to show it.

## Before you trust this anywhere

The structure thresholds are fitted to **synthetic** references, measured on this box and quoted
inline in `detect.py`, each beside the recipe that produces it. The capture contains no biological
example to fit them to: sorting its 48 in-run detections by `utc_us`, 16 of the 47 inter-detection
intervals are under a second, so grouping at a 1 s gap gives **32 events** from 48 triggers (1.50
triggers per event — the gate retriggering on a decay tail). All 48 peak in mel band 0, i.e. at or
below 500 Hz — nothing anywhere near the insect band. An earlier revision of this line said "46 of
the 48 … and the other two in bands 9 and 12"; there are no other two. It also called the events
*impulsive*, which the sketches do not support: reducing each row's `db` to a per-frame mean over
the 20 bands, the median spread between the loudest and quietest of the 8 frames is only 2.01 dB,
so the energy fills the whole 44 ms the sketch covers rather than spiking inside it. These
thresholds remain provisional, to be refitted the first time a person labels something real.
