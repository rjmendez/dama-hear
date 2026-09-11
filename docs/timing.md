# Node timing: what is measured, what is bounded, what is open

Every arrival this array produces is a sample index turned into a UTC time. This file is the
budget for that conversion on the `xiao-s3-pps` class, the arithmetic behind the constants that
enforce it, and -- at the end -- the two things the current telemetry **cannot** separate.

Measurements are from the 2026-09-07/08 captures on nyquist and mach: `health.csv` off both
cards (2661 and 2647 rows over 22.3 h, 8 reboots each), the drained `dets.csv`/`scene.csv`, and
read-only `GET /status` against both live nodes.

## The two paths, which are not the same path

**Detection path (`dets.csv`, the arrivals a solver consumes).** `utc = local_to_utc(
esp_timer_get_time() - back_us)`, where `back_us = (n-1-i)*1e6/fs_clean` moves the timestamp back
inside the 256-sample block that carried the peak. The timestamp is the PPS-disciplined
`esp_timer`; `fs_clean` only back-dates, at most 255 samples = 15.94 ms.

**Raw-ring path (`/audio`, `scene.csv` timestamps, clip WAV headers).** `sample_to_utc`
interpolates from the nearest `praw_mark` -- one (UTC, sample) pair per PPS edge -- with
`fs_clean` as the **slope**. Marks are ~1 s apart (97.8% coverage on nyquist, 96.8% on mach), so
a slope wrong by X ppm is X microseconds of error by the far end of a gap.

## Budget, detection path

| term | measured | 
|---|---|
| GPS tAcc | 21-31 ns |
| PPS interval spread / 2 | 5.00 us (nyquist spread 8-10 us; mach 41 us median, 2 probe resyncs) |
| esp_timer between anchors | 12.20 us (9.47-12.18 ppm vs GPS, re-zeroed every PPS second) |
| I2S block-quantisation residual, ~1 sample | 62.47 us |
| `fs_clean` back-date differential between nodes | 8.90 us |
| **RSS** | **64.47 us = 22.1 mm** |

against `hear/nodeclass.py`'s `t_sigma_s = 100 us = 34.4 mm`. It fits, and the dominant term is
the I2S block, which is what that constant's comment already said.

The PPS ISR's own interpolation (`since = (now - blk_end_us) * 2 / 125`) assumes exactly
16000 Hz. Clamped to one block, its worst error is 6.6 us = 2.3 mm, and it is **common-mode**
across nodes, so it cancels in a TDoA.

⚠️**That budget is for a node whose GPS is still talking, and the node cannot tell you whether
it is.** `time_valid` is set at one site -- the first NAV-PVT that names a PPS edge -- and is
never cleared, so `local_to_utc()` keeps converting from a frozen `(edge_local_us, edge_unix_us)`
pair after the UART dies. The "esp_timer between anchors" row above is that term with the anchor
one second old; with the anchor an hour old it is the same term times 3600. See below.

## Free run: what a stamp is worth when the anchor stops moving

The firmware now states it per detection, as `dets.csv` `sync_sigma_ns` (G6) and in `/status`
under `time.sync_sigma_ns` -- the same key and the same unit dama-gotchi publishes for the same
quantity, the 1-sigma uncertainty of a producer's clock-to-UTC anchor:

    sync_sigma_ns = STAMP_ANCHOR_SIGMA_US * 1000 + anchor_age_us * STAMP_DRIFT_PPM_MAX / 1000

`STAMP_ANCHOR_SIGMA_US = 25`. GPS tAcc is 25-38 ns live and negligible; the term is the PPS edge
latch, taken as half the measured interval spread exactly as the budget row above does. Spread
maxima over 4558 `health.csv` rows from all three nodes, 2026-09-10: **17 us** (nyquist),
**46 us** (rankine), **33 us at p99** (mach, one boot poisoned to 1145 us by a bring-up probe).
Half of 46 is 23; 25 rounds up. It is a **constant, not the node's live `pps_int_max -
pps_int_min`**, because that envelope is boot-cumulative, poisoned by one probe, and reset to
nothing by `gps_bringup()`.

⚠️It is the **clock anchor only**. The 62.47 us I2S block quantisation and the `fs_clean`
back-date differential are CAPTURE-path terms; they live in `nodeclass`'s `t_sigma_s` and
`path_bias_s`, and `sync_sigma_ns` means the clock on the phone side too. One column, one meaning.

`STAMP_DRIFT_PPM_MAX = 20`. MEASURED 2026-09-10/11 by differentiating the cumulative `esp_ppm`
column of `health.csv` and `health-prev.csv` from all three nodes -- `esp_ppm` is a running mean
over `pps` intervals, so `sum = n*(1e6+ppm)` and the rate over a window is
`(n1*(1e6+p1) - n0*(1e6+p0))/(n1-n0) - 1e6`. Windows containing a `pps_gaps` change discarded:

| window | n | min | max |
|---|---|---|---|
| >= 900 s | 148 | 4.359 ppm | 10.566 ppm |
| >= 300 s | 461 | 4.194 ppm | 11.671 ppm |
| >= 120 s | 1149 | -4.770 ppm | 12.450 ppm |

The single negative is 120 s of PPS jitter, not a rate. **Every other window is positive**, on
every node, at every temperature seen: `esp_timer` runs fast, so an unrefreshed anchor stamps
**late** -- about 30 ms per hour, which is the field figure. The rate is a clean function of the
node's own board temperature over the 21.61-44.65 C the archives cover:

    ppm = 15.920 - 0.2690 * T_C      n=148, residual sd 0.585 ppm, max residual 2.35 ppm

20 ppm is the **envelope**: above every measured window, and above the extrapolation of that fit
past the cold end -- 15.92 ppm at 0 C, 17.67 at fit + 3 sd. ⚠️That extrapolation is an
**inference**; nothing in this archive has been below 21.61 C.

⚠️**Not the node's own `esp_clock.ppm_vs_gps`.** During the outage this number exists for, that
figure is itself frozen; it moves ~6 ppm across the temperature range within one boot; and the
firmware does not CORRECT for it, so the whole rate is error rather than a residual. Correcting
at the fleet median and declaring the +-3.8 ppm residual would be about 3x tighter, and was
rejected: it would move timestamps already in a shipped pipeline.

**What it costs at the gate.** `hear/nodeclass.py` combines a stated sigma with the class's own
figure in RSS -- `sqrt(100**2 + sigma**2) <= 129.4 us` -- so a `xiao-s3-pps` may state at most
**82.1 us**. Minus the 25 us base, at 20 ppm, that is an anchor age of **2.86 s**. A healthy node
stamps at an anchor age of 0.57-0.66 s (`time.since_edge_us`, all three nodes, 2026-09-10), so it
passes with room; mach's 1016 s and 1317 s NAV-PVT-silent windows do not, and are refused **as
arrivals** within seconds of the link going quiet. The rows are still ingested -- they are real
acoustic events for classification and scene -- they just stop being arrival times.

## Why `fs_clean` is not a clock measurement

Its numerator advances only in whole 256-sample I2S reads, so every value it can take is exactly
`(k * BLOCK) / win_s`. Twelve readings from three independent sources were checked and all twelve
are exact -- e.g. 16006.0952 = 336128/21, 16000.8223 = 29189*256/467, and from `dets.csv`
15968.000 = 127744/8 and 16011.636 = 176128/11.

So its resolution is `BLOCK/win_s` Hz = **16000/win_s ppm**: 2000 ppm at the 8 s minimum window,
1143 ppm at 14 s, 100 ppm at 160 s. Consequences that were all read as physics before the
arithmetic was done:

* A 1118 ppm gap between the two nodes is **smaller than one quantisation step** of the coarser
  of the two readings.
* Both nodes' values moved about 1000 ppm in an hour at constant temperature.
* nyquist's own `dets.csv` spans 15968.000 to 16011.636 Hz within one run -- 2727 ppm of scatter
  on one unchanging crystal.
* Every observed 30 s interval rate lies on a ladder spaced 256/30 = 8.5333 Hz = 533 ppm.

**This is why `fs_timebase()` exists.** `FS_TIMEBASE_MIN_WIN_S = 160` is derived from
`t_sigma_s`: 160 s of window is a 100 ppm step, which is 100 us over a one-second mark gap.
Under that the node dates samples with `FS_NOMINAL`, which is wrong identically on every node and
therefore cancels in a TDoA, where an under-supported estimate does not.
`tests/test_firmware_timebase.py` holds the firmware constant and the class budget together.

Cost of not having done this, measured on the live nodes: a full 30 s `/audio` window pulled from
both differed between them by 33-41 ms = 11-14 m of sound. Round-tripping through
`X-Audio-From-Utc-Us` does not help -- the node computes it with the same wrong slope, so it
returns the requested time exactly while the audio underneath is shifted.

## The counters, and what they were doing instead

* **`drop_samples` under-reported its own loss.** The charge was one second's worth per pass, but
  a blocking handler can straddle many edges. The drain's own fetch stalled mach for 32 s --
  78,336 samples where 512,000 were due -- and 2 seconds were charged. That single interval was
  433,664 of the run's 437,504 total deficit. Now charged over the whole straddle.
* **Only the low side was ever tested.** A second delivering too MANY samples (catch-up after a
  stall, or an interval that is really two because an edge went missing) was certified into the
  rate: one such interval on mach ran 36.5 blocks long (16346 Hz, +21,630 ppm) and moved that
  node's best-available rate by 240 ppm -- 35x the resolution the estimate was quoted to. Now
  counted as `over_s` and excluded.
* **`i2s.measured_hz` was cumulative-over-cumulative**, so one stall poisoned it for the run. The
  two live nodes served 7984.6726 Hz (-500,958 ppm) and 15332.5601 Hz (-41,715 ppm) through it,
  and it is not an unread diagnostic: it is the headline of the node's own web page and what
  `firmware/hear_node/watch.py` announces. Now the drop-free cumulative rate, reported with the
  `clean_s` it averaged over.
* **`esp_clock.ppm_vs_gps` divided the whole span by `pps_count - 1`**, which counts edges seen,
  not seconds elapsed. mach read a median 3206 ppm and a maximum 37,988 ppm that way while
  nyquist -- which had never resynced -- read 9.5-12.2 ppm. Now averages only intervals that were
  really one second; the rest are counted as `pps_gaps`.

## ⚠️OPEN: a single lost block is invisible, and it looks exactly like a slow clock

A PPS second carries 62 or 63 blocks (15,872 or 16,128 samples) depending on where the phase
falls -- at 16006.6 Hz it is 62.53 blocks, so about half of all seconds carry 63. A 63-block
second that loses one block delivers exactly `62 * 256 = 15,872`, which is bit-identical to a
legal 62-block second. **No fixed per-second sample-count threshold can separate those two
cases**: 0.97 * FS_NOMINAL = 15,520 and even 0.985 * FS_NOMINAL = 15,760 sit below 15,872. The
proposed "lower the fraction to 0.985" fix is therefore refused in source, with the arithmetic
beside it, because it looks right and cannot work.

What this leaves open: the two nodes' best-available drop-free rates are nyquist 15994.760 Hz
(1710 clean PPS seconds) and mach 16006.589-16010.413 Hz (the range is one interval its own
filter admits, so the selection uncertainty is +-240 ppm, not the +-6.8 ppm block resolution).
Restricted to intervals where the drop counter charged nothing, nyquist runs about 0.49 blocks
per 30 s low = 261 ppm -- real, but not enough to close a 470-980 ppm gap. Since every rate here
is derived from the same I2S read counter, "nyquist's PDM clock is slower" and "nyquist loses
blocks nothing counts" are the same number to all of them.

**What would settle it.** (a) A frequency counter, GPS-disciplined, on the PDM CLK pin of both
nodes. (b) Failing that, a counter that knows the expected block PHASE -- a running accumulator
of `round(k*fs) - round((k-1)*fs)` rather than a fixed fraction -- which would see a single lost
block in a 63-block second. (c) Cheapest end-to-end check: fetch the same 30 s `/audio` window
from both nodes during a shared loud transient and cross-correlate. If the lag differs from the
`dets.csv` `utc_us` difference by what the slope disagreement predicts, the ring defect is
confirmed end to end; if it does not, the marks are re-anchoring more often than the mark/edge
ratio suggests.

## Not established, kept here so it is not mistaken for a finding

mach's PPS interval spread (41 us median vs nyquist's 8 us) and its 2 probe resyncs are real and
measured, and no mechanism has been established for either. Both stay well inside the 100 us
budget, so nothing downstream turns on them.
