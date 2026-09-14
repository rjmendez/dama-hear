# Clap calibration attempt, 2026-09-14: blocked on clock discipline, not on the solver

> **Correction (follow-up analysis, same day, after this doc's original commit landed in #130):**
> two mistakes were made in the analysis below, both since fixed. See
> "## Correction: real methodology + real numbers" at the end of this doc for the redone
> analysis and its (more specific, more actionable) root cause. The clock-sync-budget finding
> below is still correct and still the reason calibration is blocked; the *mechanism* is now
> understood far more precisely (stale PPS/GPS anchor + free-running crystal, not merely "weak
> antenna siting"), and the earlier non-convergence was largely an artifact of feeding the
> solver the wrong node geometry, not proof the clock issue alone made it unsolvable.

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

Live `/status` on all three nodes shows `gps.ubx_pvt: 0` — **zero valid UBX PVT fixes have ever
been received since boot** — alongside `time.ubx_silent_s` approximately equal to `uptime_s`
(i.e., the node has *never* gotten a GNSS-qualified time solution in its entire uptime, not just
"currently weak"). Each node instead free-runs on its ESP32 crystal from a frozen/never-refreshed
PPS anchor, drifting at its own measured `esp_clock.ppm_vs_gps` rate. That rate, multiplied by
time since the last anchor (`time.since_edge_us`), predicts the observed bias sign and order of
magnitude for all three nodes:

| node | `ppm_vs_gps` | `since_edge_us` | predicted drift | measured offset |
|---|---|---|---|---|
| gold | -11.552 | 51,611.26 s | -0.596 s | -0.443 s |
| ageev | +22.897 | 68,263.48 s | +1.563 s | +1.307 s |
| kasami | +26.136 | 25,653.66 s | +0.670 s | +0.363 s |

This is exactly the failure mode already anticipated in `hear/nodeclass.py`'s docstring for this
class (a node whose GPS UART/module has effectively died continues stamping from a frozen PPS
anchor and free-runs on the ESP crystal). It is a firmware/GNSS-communication problem (the module
is not delivering usable PVT fixes at all, on any of the three nodes), not a matter of improving
antenna placement — `gps.sats`/`gps.fix` still report *some* signal, but it's never reaching a
UBX PVT-qualified solution, so the PPS anchor never refreshes.

### Updated "what would unblock it"

1. Investigate GNSS module communication on `gold`/`ageev`/`kasami` directly (wiring, baud rate,
   UBX-vs-NMEA protocol configuration, module firmware) — the goal is a nonzero `gps.ubx_pvt`
   count and `time.ubx_silent_s` that resets to a small number, not just more satellites in view.
2. Once `time.sync_sigma_ns` is back under `MAX_SYNC_SIGMA_NS` (0.5 ms), repeat the co-located
   clap procedure using the corrected methodology above (near-field node geometry passed to
   `--survey`, reference-node-consensus clap association) to get a real mic-bias calibration.
3. `survey.json`'s permanent (far-field) positions for these three nodes are unaffected by this
   correction and remain correctly disclaimed as provisional/not-calibration-grade.
