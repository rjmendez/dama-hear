# Crack–blast interval — range without phase (2026-09-05 live fire)

Three arrays on this project have failed to close a bearing, all for the same reason: they need
sub-millisecond phase agreement *between* microphones. The crack–blast interval needs none. It is
two arrival **times on one channel** — the shock off the bullet's Mach cone, then the muzzle blast
arriving at `c` from the firing point — and their separation encodes range.

    dt = (R/c) * (1 - sin(theta + mu)),   mu = asin(1/M),   valid for theta <= 90 - mu

`hear/solve/crackblast.py`, 24 tests in `tests/test_crackblast.py`.

## What the interval buys, and what it does not

`dt` is largest **on** the trajectory, so with no angle knowledge at all it still gives a rigorous
**lower bound** on range, `R >= c*dt/(1 - 1/M)` (`range_lower_bound`). Turning it into a range
needs `theta`, and `dlnR/dtheta` is **2.97 %/deg** at the small angles where the method is
sharpest. So the interval does not remove the need for a bearing — it changes what the bearing has
to be good for, from sub-millisecond phase across a 0.53 m baseline to a couple of degrees of
shock arrival angle. `theta` is the angle to the **trajectory**; feeding it a bearing to the
muzzle blast is a different angle and gives a different, wrong range.

## Measured — surveyed string 20:04:45, the one event with both endpoints known

Shooter on the RTK `target` flag, robot RTK-fixed and stationary, **R = 36.26 m**. 5 rounds ×
6 ESP mics. The surveyed flags `target / inline 1 / inline 0` are collinear and the robot sits
4.2 m off that line at 35.7 m along it, so if the rifle pointed down the flag line
**theta = 6.62 deg** — an assumption about aim, not a measurement.

| | |
|---|---|
| predicted `dt` (theta 6.6–9.1 deg, M 2.4–2.8) | **46.7 – 56.5 ms** |
| hard ceiling for R = 36.26 m (theta = 0, M = 2.61) | **64.81 ms** |
| measured `dt`, 12-member estimator family (frame 2–12 ms × rise 25/50/75 %) | **53.2 – 58.0 ms** |
| same-board replicate sigma (2 mics 38 mm apart) | **0.16 – 0.55 ms** |
| round-to-round sd (5 rounds) | **3.1 – 3.5 ms** — real aim change, ≈2 deg, not error |
| angle-free lower bound (family × M 2.4–2.8) | **28.6 – 34.3 m**, always ≤ 36.26 ✅ |
| range at theta = 6.62 deg, M = 2.61 | **35.8 – 39.0 m** (−1.2 % to +7.6 %) over the whole estimator family; **36.2 – 37.2 m** (−0.2 % to +2.6 %) for the two headline detectors |
| `theta` implied by the known range | **4.1 – 7.0 deg** (family), **5.7 – 6.7 deg** (headline) vs 6.62 predicted |

Stacked over 30 traces, band power at 100–1000 Hz referred to each channel's own pre-event floor:
crack peak +62 dB, monotone decay to a trough of **+24.6 dB at 40 ms**, then a step back up to
**+46.6 dB at 64 ms**, decaying past 150 ms. The second arrival is **22 dB above the trough it
sits in** and is **4.3 ± 6.1 dB more low-frequency-tilted** than the crack — a different radiator,
not an echo of the same one.

The bullet-impact alternative is excluded by timing, not by taste: impact into the `inline 0` rise
predicts **+106 ms**, the muzzle blast **+54 ms**, measurement **+53 to +58 ms**.

## Measured — full-auto burst 21:05:33 (5 of the operator's 8 "crack+thump" labels)

7 rounds at ~86 ms (700 rpm) × 6 mics. `dt` = **32.1 – 35.5 ms** across the estimator family,
sd across rounds **0.7 – 1.25 ms**. Angle-free bound **R >= 17.0 – 19.9 m** (family × M 2.61–2.85): the shooter was
tens of metres away, not beside the robot. Range needs an angle; at a 10 m miss it would be
≈32–35 m.

## The cross-microphone bound — the first cross-board check that needs no ring offsets

`dt_i` is a difference of two times on the same channel, so a constant per-channel clock offset
**cancels exactly** — including the ESP ring's per-board offsets, which on 2026-09-05 were
−67.3 / −38.6 / +28.5 ms and drifting. What does not cancel is geometry:

    dt_i - dt_j = tau_blast(i,j) - tau_crack(i,j),   so   |dt_i - dt_j| <= 2 * d(i,j) / c

`interval_consistent()` gates on that, via the validity gate's `physically_possible`. Additivity
has nothing to say here — `dt` is one number per mic, so every loop closes by construction; the
bound is the whole test. A tolerance is **mandatory**: two mics on one ESP board are 38 mm apart,
bounding them at 0.22 ms, below any onset noise this detector achieves.

Results at `sigma_s = 0.25 ms`: surveyed **4 / 5 rounds pass**, burst **2 / 7**, and of the three
other operator "crack+thump" events, 19:20:15 and 19:35:42 pass (`dt` 65.5 and 66.7 ms, so those
shots were ≥ 36.6 m out) while **21:03:47 is refused** — its board medians disagree by 24.6 ms
against a 3.09 ms bound. The **Kinect bar is refused outright**: interval spread 8.5–39.6 ms
across a 0.167 m array whose bound is 0.97 ms.

## ⚠️A rise is not a detection

`second_arrival` returns the maximum of its search window, so `rise_db` is **not zero when there
is nothing there** — band power over ~100 frames of noise routinely peaks ~10 dB above its own
trough. Measured on the robot's own shot-free audio (~3000 anchors per capture):
**p50 5 dB, p90 8 dB, p99 12 dB, p99.9 17–31 dB**. The surveyed string's second arrival rose
20–26 dB, clear of that. The burst's rose only **6–15 dB per trace — not clear**; what carries
that detection is repeatability over 7 rounds × 6 mics and the cross-mic bound, not single-trace
SNR. `rise_reference()` computes the curve; nothing in the module hard-codes a threshold.

## Range precision this actually supports

`dlnR/dln(dt) = 1` exactly, so a fractional interval error is a fractional range error. At the
surveyed geometry (`dt` 53.8 ms, R 36.26 m, M 2.61):

| term | 1σ | contribution to R |
|---|---|---|
| interval, replicate only | 0.25 ms | **0.5 %** |
| interval, onset-definition systematic | 1.5 ms | **2.8 %** |
| `theta` at 2 deg (a good bearing) | 2 deg | **5.9 %** |
| `theta` at 15 deg (what this array does today) | 15 deg | **44.6 %** |
| muzzle velocity ±40 m/s | ±40 m/s | **3.1 %** |
| sound speed ±1.5 % (no thermometer on 09-05) | 1.5 % | **2.6 %** (coefficient 1.71 — `c` enters path *and* Mach angle) |

**Totals: ±5.1 % (±1.9 m) with theta surveyed, ±7.7 % (±2.8 m) with theta to 2 deg, ±44.8 %
(±16.2 m) with the angle this array can currently produce.** The timing is not the limit. The
angle is.
