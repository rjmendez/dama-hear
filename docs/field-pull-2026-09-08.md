# 2026-09-08 — first data pulled off `nyquist` and `mach`

⚠️**A RECORD OF WHAT WAS FLASHED THAT DAY, NOT OF THE CURRENT BUILD.** Every `MEL16_*` symbol,
`fs_code 2`, `fs=16000` and `valid_bands=15` below is what those two nodes were running on
2026-09-08 and is still what the rows they wrote declare. The node sketch has since moved to the
acquisition rate: the bank is `mel_impulse.h` / `MELIMP_*` at 48 kHz, `fs_code 7`, `valid_bands 20`,
and the frame is format-identical to a phone's. Nothing here was rewritten — the stored frames say
`fs_code 2` forever and this file is what explains them.


Both XIAO S3 Sense nodes are up and recording. `puc` is up but is not timing anything.

| node | addr | class | uptime | GPS | PPS | SD |
|---|---|---|---|---|---|---|
| `nyquist` | 172.16.100.105 | `xiao-s3-pps` | 9.4 h | fix 3, 13 sats, tAcc **26 ns**, 230400 | 33 800 edges, **0 glitches** | scene 11.0 MB, dets 62 KB |
| `mach` | 172.16.100.116 | `xiao-s3-pps` | 9.4 h | fix 3, 17 sats, tAcc **25 ns**, 115200 | 33 751 edges, 2 glitches, 2 resyncs | scene 10.8 MB, dets 113 KB |
| `puc` | 172.16.100.118 | `puc-ntp` | 10.6 h | **fix 0, 0 sats** | **not wired** | — |

⚠️ They are **not** discoverable by name from the k3s/WSL side: `nsswitch.conf` is `files dns`,
so `.local` does not resolve, and DHCP registers the chip hostname (`esp32s3-5B4B40` etc.), not
the mDNS name. Find them by `/status` on the `esp32s3-*` leases, not by hostname.

⚠️ `hear/nodeclass.py` says "mach ... its module currently decodes at no baud rate at all".
**Stale** — mach has a 3D fix on 17 satellites and 615 150 valid NMEA sentences at 115200.

## ⚠️ `dets.csv` was writing 11 columns under a 12-column header

The header declared `node,utc_us,…` and the writer's `snprintf` began at `utc_us`. **`node` was
named and never written**, so every field a consumer read by name was shifted one left — and the
column it lost was the one saying *which node the detection came from*, in the file that exists
for multi-node TDoA. 147 rows on nyquist and 268 on mach, every one 11 wide.

`scene.csv`, a thousand lines away in the same source file, writes its node name correctly. That
is what makes this an oversight rather than a convention.

Fixed, and `tests/test_firmware_csv_schema.py` compares each header constant against the format
string that writes its rows — it fails with *"DETS_HDR declares 12 columns, the writer emits 11"*
on the original source. ⚠️The header is renamed `node_id` **deliberately**: `csv_open()` rolls a
file aside only when the header string changes, so keeping the name would have appended
12-column rows under the same header as the 11-column ones already on every card in the field.

## The frames decode, and the shipped classifier refuses them — correctly

All 415 frames are 172 B, 20×8, self-consistent, and `hear.sketch.unpack` reads them. But they
declare `fs=None` and `layout=nyquist`: this firmware predates the sample-rate code (flags 8–11)
and the fixed band axis (bit 12). So `corpus.feature_matrix` **excludes** them and
`score_sketch` **refuses** them. That refusal is the design working — at 16 kHz the two layouts
are *not* the same bytes, so scoring them with a 48 kHz-trained model would be exactly the
0.9141 error the axis work exists to prevent.

**Bits 0–1 are taken** on the node (`retrigger`, `insufficient context`); **8–12 were free**.

**Now set.** ⚠️A node cannot just set the layout bit — that would ship a frame *claiming* the
shared axis while carrying rescaled data, which is worse than leaving it unset. The bit and the
filterbank are emitted together by `firmware/gen_mel.py` from one call to `hear.sketch`, so they
cannot disagree: `MEL16_FS_CODE 2`, `MEL16_LAYOUT_BIT 0x1000`, `MEL16_VALID_BANDS 15`.

At 16 kHz the fixed axis leaves **15 of 20 bands** below Nyquist; the other five get no bins and
quantise to the floor. The frame stays 20 bands and 172 B on purpose — a 15-band frame would be
smaller but would not stack with a phone's 20.

Verified without hardware by reimplementing `sketch_frame()` from the generated header:
filterbank matches `hear.mel_filterbank(layout=fixed)` to 4.8e-11, window matches `np.hanning`,
and the arithmetic reproduces `hear.sketch` **byte-for-byte over 6 cases**. A frame carrying
those flags decodes as `fs=16000, layout=fixed, valid_bands=15`, and `FLEET_SKETCH_MODEL`
now **accepts** it while the 20-band model still refuses it by name.

⚠️`gen_mel.py` wrote only `path_test/mel16.h` while `hear_node/` carried its own copy. They
happened to be identical; that was luck, and it now writes both.

**Compiled clean** (`esp32:esp32:XIAO_ESP32S3:PSRAM=opi`, core 3.3.11): 1 122 054 B of program
storage (**33 %**) and 120 460 B of RAM (**36 %**). Built against a throwaway `secrets.h` with
deliberately unusable credentials, so the image could never have joined anything; the stub and the
build tree were deleted immediately.

## ✅ FLASHED 2026-09-08 11:0x-11:2x local, both nodes

`~/.wifi` was on **mrpink**, not here; copied it, built locally, OTA'd via `flash.py`. Both came
back reporting their own identity, and both logged `boot marked healthy; failback counter
cleared`, so neither can be reverted by the boot counter.

| | before | after |
|---|---|---|
| `dets.csv` | 12-col header, **11-col rows** | **12/12, `node_id` populated** |
| frame | `fs=None, layout=nyquist` | **`fs=16000, layout=fixed, valid_bands=15`** |
| fleet model | refused every frame | **scores them** |

Both cards logged `sd rolled /dets.csv -> /dets-prev.csv (header changed)` — the rename did
exactly what it was renamed for, and the pre-flash rows are preserved on the card as well as
pulled to `~/hear-pull-2026-09-08/*_dets_preflash.csv`.

⚠️**mach's GPS took ~7 minutes to come back**, sitting at `fix=0 sats=0 baud=9600 acked=false`
while the baud/pin auto-detect re-ran — expected on the node whose GPS TX/RX is wired reversed,
but it looks alarming for several minutes. It settled to fix 3, 18 sats, tAcc 24 ns, 115200,
acked. nyquist relocked immediately. ⚠️mach's `probe_resyncs` went 2 → 5 during that scan.

⚠️The mrpink collector (`watch.py`, polling nyquist every 30 s for 1 d 10 h) **survived** the
reboot — it kept writing across the gap rather than dying.

**Superseded:** the note below was written before the flash.

⚠️**Not flashed, and cannot be from here.** `flash.py` regenerates `secrets.h` from `~/.wifi`,
which does not exist on this machine — `gen_secrets.py` exits 1 and `flash.py` aborts at step 1
before it builds. That is the tooling failing closed, and it is the right behaviour: flashing a
node with credentials for a network it cannot reach makes it unreachable and needs USB. The
nodes'  `/status` does not report the SSID they are on, so there is nothing here to reconstruct it
from.

⚠️**When.** ⚠️CORRECTED 2026-09-08 — the earlier text here called these "nodes" and put
detections at 20:00–08:00 local, peaking 22:00–00:00 (191 of 392). **Neither reproduces.** There
is no node class by that name (the two are `xiao-s3-pps`; `puc` is `puc-ntp`), and over the 638
anchored sketches in the pooled corpus **0 fall in 20:00–08:00 local** while **515 (81%) fall in
the 18:00 hour alone**. The old figure came from a different, earlier capture that is not in the
pool, and it cannot be checked.
⚠️It also inverted the advice: 18:00 sits inside the window the old text called safe to flash.
What the corpus can honestly say is that within its covered hours (10:00–20:00 local — it has no
night coverage at all, so night activity is untestable rather than absent) the busiest hour by a
wide margin is 18:00. Flash `nyquist` first, confirm it comes back and is
recording, then `mach` — never both at once, so a bad image never takes out both ears.
mach has **19 clips of budget left** and a reboot resets it, so it benefits either way.

The failback covers this change: an RTC boot counter flips the partition back after 3 unhealthy
boots, and only for an image that has never proven healthy. What it cannot save is a fault before
`setup()`; this change adds no global constructors, only a POD table and a format string. Until they are flashed they keep emitting `fs=None/layout=nyquist`. When they
are, their frames CHANGE (at 16 kHz the two layouts are genuinely different bytes), so old and
new frames are not comparable — the `dets.csv` header rename forces a roll at the same moment,
which separates them cleanly.

## The two nodes hear the same events — and the bound alone does not prove it

Baseline **16.873 m** → max |TDoA| **49.1 ms**. Over a 9.35 h overlap: **55 coincident pairs**
within the bound.

⚠️ That number means nothing on its own — the bound is wide and these nodes fire in bursts. A
**time-shift null** (mach's timestamps rotated by 1 min … span, 400 draws) gives a median of **0**
pairs, max 9, **P(null ≥ real) = 0.000**. Poisson expectation 0.0. So the coincidences are real.

**Sketch shape separates a true coincidence from a random pairing at AUC 0.9393** (median
correlation +0.905 against +0.658) — the sketch earning its keep for *association*, not
classification.

⚠️ **But it does not break a tie.** 13 nyquist events have more than one mach candidate inside
the bound, and the best candidate clears the runner-up by >0.15 in **0 of 13**: within one burst
the candidates are echoes of the same event and their sketches all look alike. Association inside
a burst remains unsolved, which is the same retrigger problem that broke the onset walk and
poisoned `gap`.

Data at `~/hear-pull-2026-09-08/` (22 MB, not committed).

## Timestamp recovery, and two asymmetries between the nodes

`tools/fix_fs_at_bias.py` undoes the latch's stamp bias in captures taken before the firmware
fix. The bias is deterministic, not estimated: the node back-dates each sample from the end of
its block by `(BLOCK−1−i)·1e6/fs_at`, so a rate 41 % too high makes the back-date too small and
every stamp too late by `(255 − sample mod 256) × 1e6 × (1/16000 − 1/fs_at)` — 18.3 µs per sample
of block position, capping at **4.67 ms**.

Applied to the captures:

| file | corrected | fs_at sound | never stamped |
|---|---|---|---|
| `mach_dets_preflash` | **59** (median 2.38 ms, max 4.58) | 201 | 10 |
| `drain0907_mach` | 0 | 2 | **346** |
| `nyquist_dets_preflash` | 0 | 154 | 13 |
| `drain0907_nyquist` | 0 | 375 | 7 |

⚠️ **nyquist was never affected** — the latch was mach-only and boot-scoped, matching the WAV
header evidence.

⚠️ **mach was essentially unanchored for the whole 09-07 drain**: 346 of 348 rows carry
`utc_us = 0` against 7 of 382 on nyquist. That is most of why mach contributes fewer usable
events than its raw detection count suggests, and it is a separate fault from the rate latch.

### The one-sided delay is real, and is not the rate latch

Anchored on nyquist the 30 coincidences give a median τ of **+25.04 ms, 90 % positive**; anchored
on mach, **−21.33 ms, 93 %** — near-negatives, as a genuine offset would be. The fs_at correction
leaves both **unchanged**: a 2.4 ms fix cannot account for 25 ms.

These coincidences are genuine, not accidents. With 526 and 262 events over a 15.0 h overlap and
a ±49.1 ms bound, the chance-coincidence expectation is **0.25 pairs**; 200 draws of independent
Poisson and clustered processes at the same rates produced too few matches to even form a null.
A synthetic process with a real +25 ms offset reproduces the signature (median +23.2 ms, 78 %
positive).

⚠️ It still cannot be split into instrument offset versus a source field on nyquist's side from
arrival times alone — but a *clock* offset is ruled out: both nodes are PPS-disciplined at
24–32 ns, seven orders of magnitude below 25 ms. What remains is an acoustic or processing path
difference, or geometry. The impulsive-source session from three surveyed positions is what
separates them.
