# Clap calibration attempt, 2026-09-14: blocked on clock discipline, not on the solver

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
