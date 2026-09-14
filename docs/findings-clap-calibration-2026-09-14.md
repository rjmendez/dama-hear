# Clap calibration attempt, 2026-09-14: blocked on clock discipline, not on the solver

> **Correction (follow-up analysis, same day, after this doc's original commit landed in #130):**
> two mistakes were made in the analysis below, both since fixed. See
> "## Correction: real methodology + real numbers" at the end of this doc for the redone
> analysis and its (more specific, more actionable) root cause. The clock-sync-budget finding
> below is still correct and still the reason calibration is blocked; the *mechanism* is now
> understood far more precisely (stale PPS/GPS anchor + free-running crystal, not merely "weak
> antenna siting"), and the earlier non-convergence was largely an artifact of feeding the
> solver the wrong node geometry, not proof the clock issue alone made it unsolvable.
>
> **Correction (third follow-up):** the first correction's own root-cause paragraph made a new
> mistake, citing `gps.ubx_pvt: 0` as proof these nodes never got a valid GPS fix. That's the
> wrong metric for this hardware class (`esp32s3-i2s-gps` speaks PMTK, not UBX, so `ubx_pvt` is
> structurally always 0 regardless of GPS health). See the "### Real root cause" section's own
> inline correction note for the PMTK-appropriate metrics and the corrected diagnosis: `gold`/
> `ageev` do have a real (if weak) autonomous fix; `kasami` is in dead-reckoning/estimated mode
> (`gps.fix == 6`), which is worse and distinct.

## What was attempted

The operator clapped near the uncalibrated ESP32 fleet nodes (`gold`, `ageev`, `kasami`,
class `esp32s3-i2s-gps`) around 2026-09-14 13:15:56-13:16:16 local time, less than 1 m from
each node. The goal: recover per-node mic capture-path bias (`path_bias_s`) with
`tools/calibrate_claps.py` (`hear.sim.ClapCalibrator`, landed in #126).

A real clap-like event was found in production: `/pool/corpus/tdoa/arrivals/2026-09-14/node.jsonl`
shows 6 near-simultaneous detections across all 6 fleet nodes (`mach`, `nyquist`, `rankine`,
`gold`, `ageev`, `kasami`) repeating every ~3-4 s in that window, consistent with several claps.

## Why it did not produce a calibrated bias

**1. No surveyed position existed for `gold`/`ageev`/`kasami`.** They were absent from
`survey.json` entirely (only `nyquist`/`mach`/`rankine`/`puc` were listed). Added as
provisional entries in this change, using each node's own GPS mean (`pos.mean_lat/mean_lon`
from `/status`), converted to the site ENU frame.

**2. Those GPS fixes are not survey-grade.** Horizontal accuracy (`hacc_m`) from live `/status`:

| node | fix | sats | hacc_m |
|---|---|---|---|
| gold | 1 (weak) | 3 | 40.0 |
| ageev | 1 (weak) | 3 | 16.6 |
| kasami | 6 (weak) | 2 | **250.0** |

`kasami`'s position is essentially unconstrained (250 m, larger than the whole array footprint).
Feeding these into the joint clap solver as fixed geometry — the only way `calibrate_claps.py`
currently accepts node positions — could not converge; `scipy.optimize.least_squares` exhausted
its evaluation budget rather than returning a result, which the CLI correctly refuses to accept
(it errors instead of writing a bogus `calibrated_node_biases.json`, per the confidence gate
design in #124/#126).

**3. The real, harder blocker: clock sync is ~1000x over budget.** Live `/status` on all three
nodes at the time of this check:

| node | `time.sync_sigma_ns` | vs `MAX_SYNC_SIGMA_NS` (0.5 ms) |
|---|---|---|
| gold | 999,129,192 ns (≈ **1.0 s**) | ×1998 over |
| ageev | 1,332,172,209 ns (≈ **1.3 s**) | ×2664 over |
| kasami | 479,974,858 ns (≈ **0.48 s**) | ×960 over |

This is not a fluke of one bad reading: `pool/corpus/tdoa/state/tdoa_heartbeat.json`'s
`buckets_ever` already carries `sync_sigma_exceeds` and `stamp_sigma_over_class_budget` history
for these nodes across multiple days — the production association pipeline has been refusing
their arrivals for exactly this reason. The raw arrival-time offsets extracted for the observed
clap window (hundreds of ms to >1 s between nodes) are the same order of magnitude as these
sync sigmas, i.e. the signal is dominated by clock-to-UTC anchor error, not by microsecond-scale
mic capture latency. `ClapCalibrator` is built to resolve a bias term at the tens-of-microseconds
scale (`admissibility_tolerance_s` default 30 µs); it cannot distinguish real mic bias from
second-scale clock noise, and correctly has no confidence-gate path that would let such input
through.

**Root cause on the node side:** all three report `gps.fix` 1 or 6 (both weak/degraded fix
states) with only 2-3 satellites, versus a healthy fix needing more satellites and a stronger
solution. A weak PVT solution widens the anchor-to-UTC error even though the PPS edge itself can
still look clean (e.g. `gold`'s `pps.spread_us` is only 57, but its `time.sync_sigma_ns` is
still ~1 s because the GNSS time solution behind that PPS edge is poorly constrained).

## What this means

Calibrating `gold`/`ageev`/`kasami` is blocked on **GPS reception quality at their current
siting**, not on missing pipeline capability. The calibration pipeline (solver, CLI, nodeclass
gating, confidence gate) is complete and already refuses to produce a number it can't stand
behind — which is exactly what happened here.

## What would unblock it

1. Improve GPS antenna siting/view of sky for these three nodes until `gps.fix` reaches a
   strong 3D solution with more satellites and `time.sync_sigma_ns` drops under the
   `MAX_SYNC_SIGMA_NS` budget (0.5 ms) — the same bar the other fleet nodes already clear.
2. Re-run the co-located-clap procedure (nodes within ~1 m, several sharp claps) once clocks
   are disciplined.
3. Re-survey `gold`/`ageev`/`kasami` positions once a strong GPS fix is available (current
   `sigma_m` of 16.6-250 m in `survey.json` is a provisional placeholder, not a real survey).

No `config/calibrated_node_biases.json` was written by this attempt — writing one from this
input would have meant either accepting a non-converged solve or silently trusting clock noise
as mic bias, either of which the confidence gate exists specifically to prevent.

## Correction: real methodology + real numbers

Two errors in the analysis above were caught after the fact:

**1. Wrong node geometry.** `gold`/`ageev`/`kasami` were physically brought within about 1 m of
the other fleet nodes for this test (per the operator) — the test was near-field/co-located, the
same procedure documented in `dama-gotchi/docs/acoustic-clap-calibration.md`. The positions fed
to the solver above, however, were each node's *far-field, permanently-surveyed* (or
GPS-mean-derived) position — `mach`/`rankine` are 10-17 m from `nyquist` in `survey.json`.
`ClapCalibrator` treats node positions as exact, fixed inputs with no positional-uncertainty
term, so feeding it positions that don't match reality is a geometry error independent of, and
compounding, the clock issue below. (These provisional `survey.json` entries are still correctly
disclaimed as "NOT CLAP-CALIBRATION GRADE" and are not otherwise affected by this correction —
`tools/calibrate_claps.py` takes its own `--survey` file, separate from the deployed
`survey.json`, so no production position data needed to change.)

**2. Naive clap-to-detection association.** The operator clapped several times, ~3-4 s apart.
The first pass grouped raw detections into claps by naive nearest-timestamp clustering across
all 6 nodes, which risks matching a `gold`/`ageev`/`kasami` detection to the wrong clap when that
node's own offset is comparable to or larger than the inter-clap spacing (true here: offsets of
0.36-1.31 s against a 3-4 s clap cadence). The corrected method instead: (a) clusters only the
three clean-clock reference nodes' (`mach`/`nyquist`/`rankine`) detections to build clap times,
requiring at least 2 of 3 to agree (rejects spurious/ambient singleton detections), then (b)
matches each `gold`/`ageev`/`kasami` detection to its nearest confirmed clap time within a wide
(2 s) window. This produced 6 confirmed claps in the window (one earlier single-node-only
candidate was correctly rejected).

### Redone run

With node positions set to a small (~1 m spread) co-located cluster reflecting the actual test
geometry, and the corrected clap associations above, `tools/calibrate_claps.py` **converges**
(it did not before). Recovered biases (`--tolerance-us` loosened to 2,000,000 for this
diagnostic run only, since the true values are far outside the 30 µs production gate and are not
being written to `config/calibrated_node_biases.json`):

| node | recovered bias | sigma_b | direct clap-offset check (mean ± stdev, n) |
|---|---|---|---|
| gold | -442.7 ms | 105 ms | -444 ms ± 23.6 ms (n=8 raw detections) |
| kasami | +363.3 ms | 106 ms | +374 ms ± 13.3 ms (n=8) |
| ageev | +1307.5 ms | 82 ms | +1310 ms ± 2.7 ms (n=5) |
| mach | +22.3 ms | 89 ms | (reference-grade node, near zero as expected) |
| rankine | -1.1 ms | 109 ms | (reference-grade node, near zero as expected) |

The solver's `sigma_b_s` (82-109 ms) is inflated relative to the direct per-clap offset spread
(2.7-24 ms) because only 6 claps at near-degenerate (co-located) node geometry poorly constrain
the joint clap-position/emission-time unknowns; the direct clap-offset numbers are the more
trustworthy uncertainty estimate here. Either way, both are 100-1000x over the 30 µs
admissibility gate — **this is conclusively not mic capture-path latency**, and no bias for
these three nodes should go into `config/calibrated_node_biases.json` from this data.

### Real root cause: stale PPS/GPS anchor, not "weak antenna siting"

> **Correction (second follow-up):** the paragraph originally here cited `gps.ubx_pvt: 0` as
> "zero valid GNSS PVT fixes ever received" and treated that as proof these nodes never get a
> real GPS solution. That's wrong for this specific class: `gold`/`ageev`/`kasami` are
> `esp32s3-i2s-gps` boards, which speak **PMTK** (MediaTek), not UBX (u-blox) —
> `firmware/boards/esp32s3_i2s_gps.h` sets `GPS_PROTO GPS_PMTK`. `ubx_pvt` only ever increments
> on the UBX code path (`firmware/hear_node/hear_node.ino`); it is **structurally always 0** on
> a PMTK node regardless of GPS health, the same way `mach`/`nyquist`/`rankine` (class
> `xiao-s3-pps`, genuinely UBX) show a real, informative `ubx_pvt` count. Citing it here as
> evidence of failure was citing a metric that doesn't apply to this hardware at all — see the
> corrected version below using the metrics that actually mean something for a PMTK node.

The PMTK-side equivalents of `ubx_pvt` are: `gps.fix` (raw NMEA GGA fix-quality field —
firmware treats any value `>= 1` as "has fix" for PMTK, per `gps_has_fix()` in
`hear_node.ino`), `gps.pmtk_ack`/`gps.pmtk_nak` (module accepted/rejected our config — both show
`pmtk_ack: 5, pmtk_nak: 0` on all three nodes, so the module itself is alive and talking), and
`gps.pmtk_glitch` (increments when an RMC sentence's decoded UTC-second delta doesn't match the
PPS pulse-count delta since the last accepted anchor — the PMTK equivalent of a PPS/time
consistency check, see `pmtk_parse_rmc()`). Re-reading live `/status` with the *correct* fields:

| node | `gps.fix` | `gps.sats` | `gps.pmtk_ack`/`nak` | `gps.pmtk_glitch` |
|---|---|---|---|---|
| gold | 1 (a real, weak, autonomous fix) | 3 | 5 / 0 | 188 |
| ageev | 1 (a real, weak, autonomous fix) | 3 | 5 / 0 | 70 |
| kasami | **6 (estimated / dead-reckoning per NMEA GGA fix-quality semantics — not a live satellite solution at all)** | 2 | 5 / 0 | 424 |

So `gold` and `ageev` do have a genuine (if weak, 3-satellite) autonomous GPS fix — they are not
"never fixed." `kasami` is a distinct and worse case: fix quality 6 means its last accepted
position is extrapolated/dead-reckoned, not derived from live satellites, consistent with it
having the fewest satellites (2) and the highest `pmtk_glitch` count (424) of the three.

The large `pmtk_glitch` counts (70-424) are the real explanation for the stale-anchor behavior:
each glitch is a rejected RMC/PPS-edge pair (the anchor-update code only accepts a pair when the
decoded UTC-second delta matches the counted PPS-edge delta exactly), so a high glitch rate means
accepted anchor updates are rare even though the check logic itself is sound — the anchor can sit
stale for a long time between the infrequent pairs that do pass. This still predicts the observed
bias sign and order of magnitude via each node's own measured crystal drift rate
(`esp_clock.ppm_vs_gps`) times time since the last accepted anchor (`time.since_edge_us`):

| node | `ppm_vs_gps` | `since_edge_us` | predicted drift | measured offset |
|---|---|---|---|---|
| gold | -11.552 | 51,611.26 s | -0.596 s | -0.443 s |
| ageev | +22.897 | 68,263.48 s | +1.563 s | +1.307 s |
| kasami | +26.136 | 25,653.66 s | +0.670 s | +0.363 s |

This is the failure mode already anticipated in `hear/nodeclass.py`'s docstring for this class (a
node whose GPS UART/module has effectively died, or whose fix is too weak/glitchy to pass the
anchor-consistency check often enough, continues stamping from a stale PPS anchor and free-runs
on the ESP crystal in between). The module itself is communicating (`pmtk_ack`/`nak` prove that),
and `gold`/`ageev` do get a real (if weak) autonomous fix — the problem is specifically that fix
is weak/glitchy enough (low satellite count, high `pmtk_glitch`) that anchor updates are too rare
to keep the PPS-derived clock disciplined, and for `kasami` the fix itself has degraded to
dead-reckoning. This still points at antenna/reception quality and possibly firmware anchor-retry
behavior, not a dead/miswired module — a real correction to the previous (also already-corrected)
claim that this was "not a matter of improving antenna placement."

### Updated "what would unblock it"

1. Investigate GPS reception quality on `gold`/`ageev`/`kasami` directly (antenna siting/view of
   sky, satellite count) — the goal is `gps.sats` high enough and `gps.pmtk_glitch` growing slowly
   enough that anchor updates happen often enough to keep `time.since_edge_us` small, and for
   `kasami` specifically, `gps.fix` reaching 1 or 2 (a live solution) instead of 6
   (dead-reckoning/estimated).
2. Once `time.sync_sigma_ns` is back under `MAX_SYNC_SIGMA_NS` (0.5 ms), repeat the co-located
   clap procedure using the corrected methodology above (near-field node geometry passed to
   `--survey`, reference-node-consensus clap association) to get a real mic-bias calibration.
3. `survey.json`'s permanent (far-field) positions for these three nodes are unaffected by this
   correction and remain correctly disclaimed as provisional/not-calibration-grade.

## Is this data recoverable, or do we need another test?

I re-ran the corrected calibration inputs in `docs/data/clap-calibration-2026-09-14/` and got the
same basic result as above: the solve converges, the recovered mean biases agree with the direct
per-node offsets, and the remaining error floor is still far above the 30 µs admissibility gate.
That points away from a solver bug and toward the timestamps themselves being too noisy for a
usable mic-path-bias calibration.

### What the 18 ms residual is, and is not

- The reproduced all-clap solve stays at `residual_rms_s = 17.95 ms`.
- Sample-rate quantisation is much smaller than that:
  - 16 kHz: 62.5 µs per sample
  - 48 kHz: 20.8 µs per sample
  Even at the slower rate, one sample is about **290x smaller** than the 18 ms residual, so
  sample quantisation is not the dominant term here.
- The recovered mean biases still line up with the direct offsets (`gold` about -443 ms,
  `kasami` about +363 ms, `ageev` about +1307 ms), so there is no evidence that a sign error or
  offset-bookkeeping bug is what is leaving 18 ms behind.

The strongest evidence that the residual is real measurement noise, not a recoverable logic bug,
comes from the **reference nodes themselves**. In the co-located survey used for this run, the
largest clean-reference baseline is `mach`↔`rankine` at 0.583 m, so the largest physically
possible true acoustic arrival difference between those two nodes is only 1.70 ms
(`0.583 / 343`). But the measured spreads among the clean-clock reference detections were:

| clap_id | ref nodes present | measured spread | physical bound from this geometry |
|---|---:|---:|---:|
| clap2 | 3 | 52.7 ms | ≤ 1.70 ms |
| clap3 | 3 | 4.68 ms | ≤ 1.70 ms |
| clap4 | 3 | 4.68 ms | ≤ 1.70 ms |
| clap5 | 2 (`mach`,`nyquist`) | 2.89 ms | ≤ 0.92 ms |
| clap6 | 2 (`mach`,`nyquist`) | 4.63 ms | ≤ 0.92 ms |
| clap7 | 3 | 99.7 ms | ≤ 1.70 ms |

So even before asking the broken-clock nodes to agree, the clap picks already violate the
array's own geometry by factors of about **3x to 59x**. That is exactly what you'd expect from a
hand clap in a reflective room with onset-picking jitter / chatter, and not what you'd expect
from a solvable constant-bias problem. The repo's earlier onset work also measured millisecond-ish
scatter (`docs/findings-2026-09-06.md` cites "onset scatter near 2 ms"), which is the same order
as the quieter claps here and still about **70x** over the 30 µs target.

### What happens if the two noisy clap clusters are removed?

The two "chatter"/reverb-heavy clusters are `clap2` and `clap7`: they are the only two confirmed
claps whose clean-reference spreads are 52.7 ms and 99.7 ms respectively.

Removing those two claps helps the residual, but **does not salvage the calibration**:

| arrivals used | solver residual RMS | `sigma_b_s` gold | `sigma_b_s` kasami | `sigma_b_s` ageev |
|---|---:|---:|---:|---:|
| all 6 claps | 17.95 ms | 105 ms | 106 ms | 81.8 ms |
| drop `clap2`,`clap7` | 10.20 ms | 4204 ms | 8738 ms | 5669 ms |

Interpretation:

- Yes, `clap2`/`clap7` are genuinely bad and do contribute to the 18 ms residual.
- No, they are **not** the only problem: even after dropping them, the residual floor is still
  10.2 ms, i.e. still about **340x** over the 30 µs gate.
- The solver covariance actually gets much worse after removing them, because only 4 claps remain
  and the near-co-located geometry is already weakly informative. So "just filter the two worst
  claps" does not produce an admissible calibration.

This is also visible in the direct-offset statistics. Using the corrected doc's own per-node
offset spreads:

| node | observed offset stdev | n claps | rough standard error `stdev/sqrt(n)` | vs 30 µs gate |
|---|---:|---:|---:|---:|
| gold | 23.6 ms | 8 | 8.34 ms | 278x over |
| kasami | 13.3 ms | 8 | 4.70 ms | 157x over |
| ageev | 2.7 ms | 5 | 1.21 ms | 40x over |

Even the **best-case node** (`ageev`) is still about **1.2 ms**, not 30 µs. If this same noise
process stayed stationary and unbiased, the number of independent claps needed to average down to
30 µs would be on the order of:

- `ageev`: about 8.1e3 claps
- `kasami`: about 2.0e5 claps
- `gold`: about 6.2e5 claps

That is not a realistic "just collect a few more repetitions" gap. The per-clap noise floor
itself has to come down.

### Verdict

**Not recoverable from this recorded dataset. A new, better-controlled test is required.**

What this dataset can still tell us reliably is the coarse, stable clock-offset story: each bad
GNSS node is free-running with a roughly constant offset of hundreds of milliseconds to seconds.
What it cannot support is a **sub-30-µs mic capture-path-bias calibration**, because the floor is
set by millisecond-scale onset/reverb/geometry uncertainty that is already visible on the
clean-clock references. That floor is 2-3 orders of magnitude above the admissibility bar, so no
plausible re-filtering of these six claps will turn it into a production-grade bias number.

The next test should therefore focus on lowering the **per-clap noise floor**, not merely adding
more claps of the same kind: fix GNSS PVT on the ESP32 nodes first, use a controlled impulse
source or speaker rather than a hand clap, measure the co-located geometry more precisely, and
prefer the highest practical sample rate / cleanest onset extraction available.
