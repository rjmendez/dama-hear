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
  `firmware/night_node/watch.py` announces. Now the drop-free cumulative rate, reported with the
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
