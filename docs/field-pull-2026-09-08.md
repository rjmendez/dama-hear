# 2026-09-08 — first data pulled off `nyquist` and `mach`

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

⚠️`gen_mel.py` wrote only `path_test/mel16.h` while `night_node/` carried its own copy. They
happened to be identical; that was luck, and it now writes both.

**Compiled clean** (`esp32:esp32:XIAO_ESP32S3:PSRAM=opi`, core 3.3.11): 1 122 054 B of program
storage (**33 %**) and 120 460 B of RAM (**36 %**). Built against a throwaway `secrets.h` with
deliberately unusable credentials, so the image could never have joined anything; the stub and the
build tree were deleted immediately.

⚠️**Not flashed, and cannot be from here.** `flash.py` regenerates `secrets.h` from `~/.wifi`,
which does not exist on this machine — `gen_secrets.py` exits 1 and `flash.py` aborts at step 1
before it builds. That is the tooling failing closed, and it is the right behaviour: flashing a
node with credentials for a network it cannot reach makes it unreachable and needs USB. The
nodes'  `/status` does not report the SSID they are on, so there is nothing here to reconstruct it
from.

⚠️**When.** These are night nodes. Detections run 20:00–08:00 local and peak at 22:00–00:00
(191 of 392 in those two hours); the last on either node was **08:46 local**. The safe window is
**09:00–19:00 local**, and it is 10:20 now. Flash `nyquist` first, confirm it comes back and is
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
