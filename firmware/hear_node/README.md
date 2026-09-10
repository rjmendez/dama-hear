# node

A XIAO ESP32-S3 Sense left outside overnight, reporting over WiFi. It exists for **one
measurement**: the true I²S sample rate, disciplined against a real GPS PPS.

`firmware/path_test` could never make it — its pulse and `esp_timer` came off the same crystal, so
the interval was that oscillator against itself. A GPS PPS is an independent reference, so counting
samples between edges gives the rate in Hz to GPS accuracy. Over a night the per-block granularity
averages out to well under a ppm.

## Before you flash

    cp firmware/hear_node/secrets.h.example firmware/hear_node/secrets.h   # then edit
    arduino-cli compile -u -p /dev/ttyACM0 \
      --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi firmware/hear_node

`secrets.h` is gitignored. Without it the node starts its own AP (`dama-hear-node` / `damahear`,
http://192.168.4.1/) — fine for a bench check, useless in the garden.

## Wiring

Leaves the onboard PDM mic in place, since the good mic has not arrived.

| node | GPS module |
|---|---|
| D7 (GPIO44) | module TX |
| D6 (GPIO43) | module RX |
| **D0 (GPIO1)** | **PPS** |
| D4 / D5 (GPIO5/6) | SDA / SCL — IST8310 + BMP280 |
| 3V3, GND | VCC, GND |

⚠️**This table said D11 and was wrong**, and it is the thing you check your wiring against at
2 a.m. The code has said `#define PPS_PIN 1 // D0` and `#define PDM_CLK 42 // D11` for some time,
the `.ino` header says `GPS PPS -> D0 (GPIO1)`, and the mic is *enabled* —
`i2s.begin(I2S_MODE_PDM_RX, ...)`. **D11 is the PDM microphone's CLK, an output**, and two
push-pull drivers on one pin is
not a configuration — which is exactly why PPS moved to D0 and the mic stayed. Do not wire PPS to
D11.

`/pins` dumps the compiled-in map so it can be checked against the wiring rather than trusted.
⚠️**microSD CS is GPIO21, not GPIO3.** The Seeed wiki says GPIO3; this board mounts on 21, which is
not a castellated pad at all. That leaves D2/GPIO3 genuinely free — five spare pads, not four.

Power the module at **3V3**, not 5V — the XIAO is 3.3 V logic.

⚠️PPS is not on a drone GPS harness. Tap it at the module's PPS LED pad, and meter which end of the
LED swings: through an LED and series resistor only one side is a usable edge.

## Reading it

`http://damahear.local/` or the printed IP. The page refreshes every 2 s; `/status` is JSON,
`/detections` lists what the gate fired on.

The SD card is the actual record. WiFi is a convenience and an overnight run must not depend on
it. Everything below is fetchable over the same link with `/sd?file=/dets.csv&tail=20000`:

| file | written | holds |
|---|---|---|
| `dets.csv` | as detections fire, batched once a second | one row per detection: `utc_us`, `sample`, `pps_n`, signed `us_since_pps`, `trigger`, `flags`, the rate it was timed at, the log-mel sketch as hex, and `clip` / `clip_why` |
| `health.csv` | every 30 s | GPS and PPS quality, the acquisition audit, gate state and `gate_floor`, detection counters, the clip counters, card space |
| `scene.csv` | every 1.024 s, ungated | the scene descriptor — 20 bands × 4 quarter-second slices of log-mel, hex, the microseconds its FFTs cost, and the band edges that produced it |
| `clips/*.wav` | one per detection, budgeted | 4 s of raw PCM around the trigger |
| `gate.cfg` | when `POST /gate?...&persist=1` is used | one line: the gate floor to restore at boot |

⚠️**Three headers changed in this build**, so the first boot after flashing rolls `dets.csv`,
`health.csv` and `scene.csv` aside to their `-prev.csv` names. `csv_open` does `SD.remove(prev)`
*first*, so an existing `-prev.csv` is destroyed rather than chained. **Pull the card's files before
you flash this.**

`night.csv` is the older, narrower health schema and is no longer written. If the health schema
changes again the node rolls the old file to `health-prev.csv` rather than appending wider rows
under a narrower header, which would make every row in it ambiguous.

### The header row was missing for 11.33 h, and it was not the CSV code

An 11.33 h run produced `health.csv` and `dets.csv` with **no header row at all**, and
`health-prev.csv` had none either. The writer looked correct: open `FILE_APPEND`, `if (f.size() ==
0)` write the header.

`File::size()` is the bug. In esp32 core 3.0.5 `VFSFileImpl::size()` returns `_stat.st_size` and
only re-`stat`s when the file has been written (`vfs_api.cpp:406`). `_stat` is a plain member that
the constructor never initialises — it is filled by one `stat()` run **before** the open
(`vfs_api.cpp:274`), and on a file the open is about to *create* that `stat()` fails and leaves
`_stat` untouched. So on a freshly created file `size()` returns whatever was on the heap:
deterministic per build, because the same allocation sequence repeats every 30 s.

Which is why it appeared exactly at the schema roll. Before it, `/health.csv` always existed at
open time and `size()` was real. The roll's `rename` makes the very next open a *create* — the only
path that reaches the uninitialised `_stat`. And it was self-perpetuating: the headerless file
failed the schema check on the next boot, rolled itself aside, and destroyed the last file that
still carried the old header.

Both writers now ask `SD.exists()` before opening, which opens `"r"` and tests the handle and is
therefore also correct for a file that exists and is empty. The `det_hdr_done` latch went with it —
it was set even when the header had *not* been written, so a card swapped mid-run could never get
one — and the `SD.rename` that rolls the old schema aside is now checked and logged.

## The last four minutes of audio, in PSRAM

The detection sketch is 33.3 ms of log-mel and only exists when the gate fires. That is enough for a
classifier built beforehand and useless for anything else: the 11.33 h run produced **48 in-run
detections** and, until this build, no way to listen to a single one.

⚠️**48 is the in-run count.** Recipe, on `dets.csv` from the 2026-09-07 capture: 62 data rows, 14
of them at `uptime_s == 12` — the first 12 s of a boot, before the mic has settled — and 48 with
`uptime_s > 12`, all of which also carry a valid `utc_us`. This README and `hear_node.ino` used
to say 45 and "the counter ends on 47"; **neither reproduces and both are gone.**

The file and the counter reconcile exactly once you remember that **`dets.csv` persists across
reboots and `det_n` does not.** The last `health.csv` row reads `det_n = 50` for the 11.33 h boot;
48 in-run + that boot's own 2 startup triggers = 50. The other 12 rows at `uptime_s == 12` are two
apiece from the six OTA reboots before the run. Likewise `sample` is `g_samples`, which also resets
each boot, so the two repeated values (5 and 211) are different detections from different boots
landing on the same index — both pairs carry different `trigger` values, and there are **zero**
byte-identical rows in the file. An earlier draft of this section recorded the mismatch as an
unexplained discrepancy; it is neither unexplained nor a discrepancy.

`praw` is a rolling raw-PCM ring in PSRAM. 240 s × 16000 Hz × 2 B = **7.68 MB** against the 8.34 MB
the board reports free. `ps_malloc` needs one *contiguous* block and total-free is not
largest-free, so boot asks for 240 s and steps down through 180/120/60/30 rather than failing, and
keeps 256 kB of PSRAM back for WiFi. **A failed allocation is not an error** — `/audio` goes away
and capture, gating and logging are untouched. The boot log says which span was obtained, because a
silent failure here would look exactly like a quiet night.

It holds the **DC-blocked** samples, not the raw ones: the pedestal drifted 1093.9 → 1439.6 over
the night, so raw audio carries a moving offset a consumer would only have to remove again, and the
ring would not match what the gate and the sketch saw. `gate.dc` / `sig_dc` recovers the pedestal if
it is ever wanted.

The ring is contiguous in **write order, not in time**. A lost block leaves no hole in it, and the
night lost 18 seconds of 40791 (42749 samples), so reading it back at a flat rate would be wrong by
up to that much. So it is anchored the way everything else here is: one `(UTC, sample)` pair per GPS
second, 300 of them, recorded for the *previous* edge, because `local_to_utc` will only name an
edge whose NAV-PVT has arrived — and if it has not, the pair is skipped rather than guessed. There
was a figure here for how late that report arrives. It was never measured, so it is gone; nothing
depended on it, because the naming either succeeds or refuses.

A mark's sample index is interpolated from the last completed I2S block to the edge and clamped to
one block. Without that it is `g_samples`, which advances once per 256-sample block and so names
the last completed block rather than the edge — 0 to 15.9 ms early, every time, which is up to
5.4 m of acoustic path. The clamp means a reader stall can at worst put it back where it started.
None of this has been checked against an external reference, because the node has no second clock
to check it with: these are bounds on the arithmetic, not a measured accuracy.

    GET /audio                              what is retrievable: span, fill, from/to utc_us
    GET /audio?from=<utc_us>&dur=<seconds>  that window as a playable mono 16-bit WAV

`dur` is capped at 30 s. Serving the whole ring would be 7.68 MB, which at the 335 kB/s measured on
this node is ~23 s inside one handler, and the I2S DMA holds 6 × 240 frames = **90 ms** — a handler
that does not drain it would throw away more audio than the whole night lost. So the loop's own
audio path is pumped between 4 kB chunks, the same reason `/tp` pumps `Serial1` rather than
`delay()`ing.

The writer does not stop while the response is sent, so the oldest ~16 s of the ring is not served
(capped at a third of the ring, so a 30 s fallback ring still gives 20 s). If the requested window
is not entirely held, **what is there is served and the response says so** rather than silently
returning a short file:

| header | |
|---|---|
| `X-Audio-Clipped` | `none` / `head` / `tail` / `both`; `416` with `all` if none of it is held |
| `X-Audio-From-Utc-Us`, `X-Audio-Samples` | what actually came back |
| `X-Audio-Fs-Hz` | the PPS-disciplined rate, e.g. `16000.1690` |
| `X-Audio-Ring-From-Utc-Us`, `X-Audio-Ring-To-Utc-Us` | what else could have been asked for |

⚠️A WAV header's rate field is an **integer** and cannot carry 16000.169 Hz. It says 16000. Anything
doing timing work must use `X-Audio-Fs-Hz`, not what the WAV claims. `Content-Length` is the 44-byte
header plus exactly the samples being sent — the `/sd` tail bug (`streamFile()` advertising
`f.size()` regardless of the seek) is not repeated here.

## The scene feature: 1.024 s, ungated, and now down to 62 Hz

The sketch is an **impulse** descriptor: 8 frames at hop 192 = 1600 acquisition samples =
33.3 ms, and it only exists when the gate fires. (The hop scales with the rate and `NFFT` does
not, so moving the bank from 16 kHz to 48 kHz made the window cover *less time*, not more — 44 ms
→ 33.3 ms. That is what the phones do and what the 20-band model was trained on.) Nothing in 11.33 h described the **background**, which is what separates
a chorus from a road.

`scene.csv` is the scene-scale counterpart, the node's analogue of the 0.96 s patch hugbot's YAMNet
consumes. 20 bands × 4 quarter-second slices, 1.024 s of span, written whether or not anything
triggers.

### It no longer shares the detection sketch's filterbank

It used to, and this section used to say so. Two measurements moved it.

**Every in-run detection peaked in the bottom band, and none of them peaked anywhere else.**
Recipe: take the 48 `dets.csv` rows with `uptime_s > 12` and `utc_us != 0`, decode `frame_hex`,
un-quantise the int8 payload to dB (`q / 2 + ref_db`), and take each band's peak across the 8
frames. `argmax` is band 0 in **48 of 48**, and band 0 exceeds band 1 in **48 of 48**, median gap
**5.5 dB**. A feature whose most extreme band always wins is telling you the spectrum is still
climbing where the filterbank stops — not that it has found the peak.

**A 10 s pull off the node's own ring says where the energy actually is**, relative to the total:

| band | dB rel. total |
|---|---|
| 2–62 Hz | −6.2 |
| **62–312 Hz** | **−1.9** |
| 312–500 Hz | −11.9 |
| 500 Hz–1 kHz | −16.2 |
| 1–2 kHz | −22.3 |
| 2–4 kHz | −27.8 |
| 4–8 kHz | −26.3 |

62–312 Hz carries **+8.2 dB more than the whole 312–8000 Hz span the sketch could then see**. And
the detection bank's band 0 started at FFT bin 5 = 312.5 Hz: all of that low energy is captured,
survives the 1.6 Hz DC block, reaches the PSRAM ring, and is thrown away before the filterbank.

Both numbers were measured while the detection bank ran at 16 kHz. It now runs at 48 kHz, where
its band 0 starts at **375.0 Hz** — so it discards *more* of that low energy, not less, and the
argument for a separate scene bank is stronger than when it was made.

So the scene descriptor gets its own bank, `mel_scene.h`, generated by `firmware/gen_mel_scene.py`:

|  | detection (`MELIMP_*`, `mel_impulse.h`) | scene (`MELS_*`, `mel_scene.h`) |
|---|---|---|
| rate | **48 000 Hz** (`acblk`, acquisition) | **16 000 Hz** (`dcblk`, decimated) |
| bin width | 187.5 Hz | 62.5 Hz |
| band 0 | bins 2–3 = **375.0–562.5 Hz** | bins 1–4 = **62.5–250.0 Hz** |
| band 1 | bins 3–4 = 562.5–750.0 Hz | bins 3–6 = 187.5–375.0 Hz |
| band 2 | bins 4–5 = 750.0–937.5 Hz | bins 5–8 = 312.5–500.0 Hz |
| band 19 | bins 79–106 = 14812.5–19875.0 Hz | bins 98–125 = 6125.0–7812.5 Hz |
| empty bands | 0 | **0** |
| nonzero weights | 194 | **233** |

⚠️**The two banks are at different rates, so a bin index does not compare between them.**
`(FB_LO, FB_N) = (9, 5)` occurs in *both* — 1687.5–2437.5 Hz on a sketch frame, 562.5–812.5 Hz on
a scene row. No Hz span coincides; a bin-space comparison would report that pair as a shared band
and it is not one. `tests/test_firmware_mel_scene.py` compares them in Hz, using each bank's own
declared rate, for exactly that reason.

`NFFT` stays 256 and the band **count** stays 20, so the row is the same size and the storage
arithmetic below still describes the file being written. The scene bank's top edge is 7812.5 Hz
rather than 8000 because `hear/sketch.py`'s `mel_filterbank` clamps its upper edge to `fs/2 ×
0.98`; that clamp is `hear/`'s and was not touched. The detection bank is on the **fixed** axis
(`[F_LO, F_HI] = 300–20000 Hz`) and is not clamped, which is why its top band reaches 19875 Hz.

⚠️**The detection bank is frozen and this change does not touch it.** `firmware/hear_poc` checks it
byte-exact against golden vectors (172/172) and `hear/wire.py` profile 0 *is* the 20×8 `f_lo=300`
shape, so re-spanning it would silently reinterpret every frame already on the wire. The scene
descriptor has no wire profile, no trained model and — as of this writing — no captured rows at
all, which is the whole reason it is the half that gets to move.

The **window** is still shared. `np.hanning(nfft)` depends on `nfft` alone — *not* on `fs` — so
`MELIMP_WIN` is bit-for-bit what a scene-only generator would emit even now that the two banks are
at different rates; duplicating it would cost 1024 B of flash for a second copy of the same
numbers. `static_assert(MELS_NFFT == MELIMP_NFFT, ...)` is what makes that reuse checkable rather
than assumed.

`scene.csv` gains two trailing columns, **`f_lo_hz` and `f_hi_hz`**, so a card's rows say which
bank produced them without needing the firmware version. They are appended *after* `mel_hex`, not
inserted next to `bands`/`slices`, because `tools/hear_bridge.py` checks the header as a prefix
(`head[:len(columns)]`) and documents trailing columns as the supported way to grow one. `/status`
reports the same edges under `scene.f_lo_hz` / `scene.f_hi_hz`. The header change means the first
boot rolls the old `scene.csv` aside — that machinery exists for exactly this.

### The rest is unchanged

`BLOCK` is 256 and `MELIMP_NFFT` is 256, so **one I2S block is one FFT frame** — enforced by a
`static_assert`. That is the whole design: the descriptor is built 16 ms at a time at a steady
62.5 FFT/s, instead of a 64-FFT burst once a second that would have to fit inside the DMA's 90 ms
of headroom or drop audio. The measured +10.6 ppm rate error is far inside one 62.5 Hz bin of a
256-point FFT, so tables built for the nominal rates stay correct for both banks.

Storage, because it writes continuously to a 40 MB partition with 19 MB free: 80 B of mel
hex-encoded to 160 chars plus ~66 chars of columns is **~227 B a row**, and 12 h is 42188 rows =
**9.6 MB**. That fits alongside `health.csv` (~0.3 MB per 12 h) and `dets.csv` and leaves the card
about half empty. Eight slices instead of four would be 16 MB and would not.

**The cost is measured, not estimated.** `scene.fft_us_per_row` in `/status` (and the `fft_us`
column of every row) is the microseconds of FFT and filterbank summed over the 64 frames of one
1.024 s row, taken with `esp_timer_get_time()` around the work itself; `scene.fft_us_max` is the
worst row since boot.

Measured on the node, 115 rows in: **16 842 µs per 1.024 s row — 1.64% of one core**, worst row
17 670 µs. So the scene descriptor is essentially free, and the 33.3 ms impulse sketch beside it is
smaller again. `rows` and `written` were both 115 with `write_fail` 0 and `short_blocks` 0.

One thing the same session measured that is worth knowing before you lean on `/audio`: fetching a
3 s clip and three CSVs took `drop_s` from 2 to 4. Plain `/status` polling costs nothing
detectable, but a **download does** — it holds `loop()` long enough to lose a block or two even
with the pump draining by elapsed time. Pull audio when you want audio, not on a timer.

Two more counters worth a glance: `scene.short_blocks` is I2S reads that came up short of a whole
frame and were skipped rather than padded with silence, and `scene.write_fail` is the same
short-write accounting `dets.csv` gets. `scene.csv` is held open and flushed on the 30 s health
interval rather than per row, so a power cut can lose up to 30 rows (~7 kB).

`us_since_pps` is **signed**. A sample back-dated across an edge belongs to the second before it,
and reporting that unsigned would put the event 999 ms — 343 m — from where it happened.

## A WAV per detection

The PSRAM ring holds 240 s and `/audio` can serve any window of it. What it cannot do is outlive
those 240 s: an event heard at 03:00 is gone by 03:04 unless somebody was awake and fetching. Each
detection now also gets a fixed-length WAV on the card, so the audio survives the night the way
`dets.csv` does.

    /clips/<8 hex boot id>-<10 digit sample>.wav      e.g. /clips/1a2b3c4d-0004192768.wav

The boot id is `esp_random()` at startup — **not** derived from `utc_us` (three of the capture's 62
rows have `utc_us == 0`, and zeros collide) and not from `sample` or `det_n` alone, both of which
restart at 0 every boot and would have a second night overwrite the first. The sample index is the
join key back to `dets.csv`. Clips live in a subdirectory because FAT root directory entries are
finite and long filenames burn several each.

The writer addresses the ring **by sample, not by UTC**. `dets[].sample` is a direct `praw` index —
both are counted in `g_samples` — so unlike `/audio`, which refuses outright with *"the ring cannot
be addressed by time"*, a clip still works for a detection stamped `utc_us == 0`.

### Length, and why the post-roll is the long half

**1 s before the trigger, 3 s after: 64 000 samples, 128 044 B with the header.** The events are
longer than the descriptor — across the 8 frames of each in-run sketch the median energy varies
only ~4 dB and the peak frame is spread over all 8 positions, so whatever fired the gate has not
finished inside the sketch window. Post-roll is where the content is. That was measured on the
44 ms window; the window is now 33.3 ms, so it holds a fortiori.

Every written clip is **exactly** 128 044 B. A window that has fallen off either end of the ring is
refused rather than shortened, which is what makes the budget arithmetic exact rather than an
estimate — and what stops a caller believing it has audio it does not have, the same reason
`/audio` sends `X-Audio-Clipped`.

**One clip per event, not per trigger.** The 48 in-run detections collapse to **33 distinct events**
at 1 s clustering: 16 of the 47 gaps are under 1 s, about 1.5 triggers an event, the gate
retriggering on a decay tail. Clipping per trigger would spend 1.5× the card for the same audio,
and the second clip would be a 4 s window overlapping the first by 3 s. A detection suppressed this
way still gets its row, with `clip_why = dedupe`.

### The budget, and the arithmetic behind it

Free space measured on this card is **19 MiB** (`sd_free_mb` is a floor — `(total − used)/1048576`
— and it read 19 for most of the run). Over a 12 h night the CSVs take, from row sizes measured on
the capture's own files:

| file | rows in 12 h | B/row | total |
|---|---|---|---|
| `scene.csv` | 42 188 (12 h / 1.024 s) | 227 | 9 576 676 B |
| `health.csv` | 1 440 | 151.2 (219 244 B / 1450 rows) | 217 728 B |
| `dets.csv` | 51 | 437 (403.1 measured + 34 of clip columns) | 22 287 B |
| | | | **9 816 691 B = 9.36 MiB** |

That leaves **9.64 MiB**. At the measured event rate — 33 events in 11.33 h = 2.91/h = 35 over
12 h — clips cost 35 × 128 044 = 4 481 540 B = **4.27 MiB**, a 2.26× margin.

`CLIP_BUDGET_B` is set **above** that rather than at it, at **6 MiB = 49 clips**, because the events
are not spread evenly: **34 of the 48 triggers fall in the two hours 09:00–10:59**, which is
4.15 MiB of 4 s clips inside two hours. A per-night average protects nothing against that; a byte
budget does. 6 MiB is 1.4× the measured 12 h event count and still leaves 3.64 MiB of the remainder
for the CSVs to overrun into.

⚠️**There is no 24 h budget, because there is no spare day.** `scene.csv` alone consumes the whole
19 MiB in 24.96 h and the three CSVs together in 24.36 h. This is a night-length node.

Under the budget sits a live floor: clips stop when `sd_free_mb` drops below **2 MiB**, which is
~2.6 h of scene rows (227 B per 1.024 s = 221.7 B/s), so the record keeps running for hours after
the audio stops. Free space is sampled on the existing 30 s health tick, **not per clip** —
`SD.usedBytes()` is a free-cluster walk on FATFS and its cost on this card has not been measured.

**Nothing is pruned.** Oldest-first deletion would risk removing a clip a `dets.csv` row already
names, and a row naming a file that is not there is worse than a row that says it never got one.

### Exhaustion is visible, and a full card does not read as a quiet night

`clip_written` advances **only** after the full 128 044 B has landed. Every refusal has its own
counter and its own token in the `clip_why` column of `dets.csv`:

| `clip_why` | counter | means |
|---|---|---|
| `ok` | `clip_written` | the file named in `clip` is on the card |
| `budget` | `clip_skip_budget` | `CLIP_BUDGET_B` spent. Working as designed |
| `cardfull` | `clip_skip_cardfull` | the **card** is nearly out. `health.csv` and `dets.csv` are next |
| `dedupe` | `clip_skip_dedupe` | within 1 s of a clip that was written |
| `ring` | `clip_skip_ring` | window not in the ring, or no PSRAM ring at all |
| `stalled` | `clip_skip_ring` | the post-roll never arrived within 10 s — capture is stuck |
| `nocard` | — | no card mounted |
| `fail` | `clip_fail` | short write or failed open; the partial file is deleted |

`budget` and `cardfull` are deliberately **not** the same token: one says the firmware is rationing
itself, the other says go and swap the card. All of these are columns in `health.csv` and fields
under `clips` in `/status`, alongside `budget_left_b` and `budget_left_clips`.

### What it costs the audio, and the one regression

The clip is written **one 4096 B chunk per `loop()` pass**, not in a single 128 kB write that would
stall the I2S reader far past the DMA's 6 × 240 frames = 90 ms. `loop()` already calls
`audio_pump()` every pass, so the DMA is drained between chunks by the code that already does it,
with no nested pump inside a card write. A whole clip is 32 chunks, so at the ~16 ms an I2S block
takes it lands in about half a second.

⚠️**A clip costs about two dropped seconds, measured on hardware.** Forcing one detection with
`POST /gate?floor=100` on a quiet evening took `drop_s` from 5 to 7 while the single 128 044 B clip
was written; a second run over the same window took it 7 → 12 alongside the `/sd` fetches of the
clip itself. So the cost is real and it is not hidden — it lands in `drop_s`, which is the
instrument for exactly this.

Put that against the baseline: the 2026-09-07 capture lost 18 one-second windows in 11.33 h with no
clips at all. At the measured event rate (33 distinct events in 11.33 h) clips would add roughly
70 short windows a day, so **they roughly triple the loss** — from about 2.7 s of audio lost in a
day to something nearer 10 s. Still four thousandths of a percent, and worth it for being able to
hear an event rather than guess at it, but it is a trade and not a free feature. If it matters,
the chunk size and the pump cadence in `clip_pump()` are where to spend the effort.

⚠️**A detection's row now waits for its clip.** `det_flush` will not write a row while `clip_st` is
`PENDING`, because a row written earlier would either name a file that may never appear or record
"no clip" for one that was about to land. The wait is bounded at **10 s** (`CLIP_WAIT_MAX_S`),
against the ~1 s it used to be. The cost is real: a power cut inside that window loses the row,
where before it lost at most a second's worth. `det_n` versus `det_written` in `health.csv` is
where that would show. The record was judged to matter more than the audio, so the deadline
resolves the clip as `stalled` and releases the row rather than ever blocking it indefinitely.

`SD.begin` now asks for **8 open files** instead of the library's default 5 (`SD.h:29`). The
long-lived set is `dets.csv` + `scene.csv` + the clip in flight = 3, and `/ls` holds a directory
plus an entry while the 30 s health block has `health.csv` open — 6, one past the old limit, and an
`SD.open` past the limit just returns a falsy `File`.

A power cut mid-clip leaves a truncated WAV with no `dets.csv` row naming it. Those orphans are
identifiable exactly that way: **a file in `/clips` that no row names was interrupted.**

## The gate floor, at runtime

The floor was a compile-time `800`, and over the capture it — not the adaptive `8 × ambient` limb —
was what the gate actually ran on: `gate_thr` was exactly 800.0 in **1418 of 1450** `health.csv`
rows (**97.8%**), maximum 1715. Median ambient over those rows is **28.9**, so `8 × ambient` is
~231 and the floor sat ~3.5× above it all night. 800 was chosen for gunshots; retuning it for
anything quieter meant a reflash and a walk outside, which is why it never got retuned.

    GET  /gate                                  the active floor, its source, and the live evidence
    POST /gate?floor=300                        this boot only
    POST /gate?floor=300&persist=1              and across reboots, via /gate.cfg
    POST /gate?reset=1                          back to the compiled-in 800, deletes /gate.cfg

POST for the same reason `/reboot` is POST: a link prefetcher must not be able to deafen the node,
and `watch.py` polls this node's endpoints unattended every 30 s. `/status` reports `gate.floor`,
`gate.floor_source` (`default` / `file` / `http`) and `gate.floor_saved`, and `health.csv`
gained a `gate_floor` column — because `gate_thr` alone only **bounds** the floor from above, and
only in the 32 rows of 1450 where the adaptive limb was binding.

⚠️`gate()` used to recompute the threshold with its own copy of the `FLOOR` literal while
`gate_thr()` computed another. A runtime floor changed at one site only would have left `/status`
and `health.csv` reporting a threshold the gate was not using — a record that looks correct and is
not. Both sites read `g_floor` now.

### The guard, and what it does and does not protect

**`FLOOR_MIN = 100.`** Below roughly this value the knob stops doing anything. Recipe: count the
30 s `health.csv` rows whose `gate_e_max` exceeded `max(floor, 8 × ambient)` — windows that would
have contained at least one crossing:

| floor | windows | % of 1450 |
|---|---|---|
| 800 (shipped) | 38 | 2.6 |
| 600 | 100 | 6.9 |
| 400 | 443 | 30.6 |
| 200 | 982 | 67.7 |
| **100** | **1043** | **71.9** |
| 0 | 1045 | 72.1 |

100 and 0 differ by **0.2 pp**, because by then the adaptive limb has taken over as the binding
one. A setting that reads as a change and is not one is worse than a refusal.

**`FLOOR_MAX = 32768.`** The envelope is a 16-sample mean of `|int16|`, so it cannot exceed 32768
by construction; a floor at or above that is a gate that can never fire, which is
indistinguishable from a dead microphone — the exact failure `env_e_max_win` exists to rule out.

⚠️**Neither bound protects the card, and it would be a lie to imply one does.** Floor 100 gives 27×
the crossing rate of floor 800 on the measured night. What bounds the card is downstream:
`det_flush` writes at most 16 rows per second (16 × 437 B = 6992 B/s, so 19 MiB in **47 min** at
the absolute cap), and the clip writer — which at 128 044 B a clip would fill the card in about
three minutes at that rate — is held by `CLIP_BUDGET_B`. **The clip byte budget is what makes the
floor route safe, not the other way round.** Lower this floor on an unattended node only with that
budget in place.

Persistence re-acquires one hazard worth naming: a bad value in `/gate.cfg` re-applies itself every
boot, and there is no watchdog here to undo it. The clamp is therefore applied on **load** as well
as on set — a file written by hand with `0` in it comes back as 100, not 0 — and `POST
/gate?reset=1` removes the file.

`floor_saved` is the **number** `/gate.cfg` holds, or `null` if there is no file — not a yes/no.
A bare `persisted: true` next to an active floor of 300 while the file still said 800 would be
true and useless; what you need to know at 2 a.m. is what the node comes back as after the plug
timer cuts it.

### Three things that made the record worthless before they were found

Each was invisible from outside, and each is now a field you can read.

**The gate could go deaf and look quiet.** Ambient was only learned while armed, so a noise floor
that rose above the threshold could never be learned, so it could never fall back below
`thr * REARM`, so the gate never re-armed. Measured outdoors: 156 s solid disarmed, ambient frozen
at 73.2, two detections all night, both from before it locked. The floor is now tracked whether
armed or not — fast below threshold, ~12.5 s above it — with a 30 s forced re-arm under that.
`gate.armed` and `gate.forced_rearms` say so.

**The mic sits on a DC pedestal.** `mean(s)` measured 1285.8 against `mean(|s|)` 1285.3 — the same
number, which can only happen if the waveform never crosses zero. The envelope detector was
therefore measuring the offset rather than any sound, and pinned the threshold at 8 × 1285 =
10280: an event had to swing 27% of full scale to register. Samples are DC-blocked at ~1.6 Hz
before anything sees them; the true acoustic floor turns out to be 15–70. `gate.dc` reports the
pedestal being subtracted.

**A full card fails by returning a short write, not by raising anything.** The counter used to
advance regardless, so a card that filled at 03:00 looked exactly like a night that went quiet at
03:00. `write_fail` and `sd_free_mb` make the difference visible.

## What the GPS actually was

The HGLRC HG-M10-02 came off a flight controller and was configured accordingly: **230400 baud,
UBX binary, NMEA disabled**. Assuming 9600/NMEA found nothing, and a lenient line counter reported
22 "sentences" from pure framing noise — a wrong number that looked like a working link.

The scan now walks eight rates and counts **UBX sync words as well as NMEA lines**, because a
module that has ever met Betaflight or INAV will not be speaking NMEA. Once found, the module ACKs
the VALSET and reports 3D fix with 12 satellites and `tAcc` **23 ns** — indoors.

That 23 ns is the number `docs/architecture.md` has been asserting without evidence.

Its I²C carries an **IST8310** magnetometer and a **BMP280** at `0x76`, both already wired.

## Two traps this hit on the bench, both now guarded

**A floating PPS input self-oscillates.** Bare `INPUT` on an unconnected pin produced ~3.4 kHz of
phantom edges, and the rate maths turned them into a confident **+626 ppm**. The pin is now
`INPUT_PULLDOWN`, and edges closer together than 500 ms are counted as glitches rather than
averaged in — a 1 Hz pulse cannot produce them, so seeing `glitches` climb means noise on the wire,
not a fast clock.

**`gate()` is stateful and must be called exactly once per sample.** An earlier version called it a
second time on the not-detected path, feeding the envelope samples that never existed.

**Counters must count the thing they are named after.** `sentences` incremented on any line, so
noise and data were indistinguishable; `valid_nmea` requires a `$` and a talker id, and told the
truth immediately. Every pin now gets probed rather than assumed — a pulldown says whether anything
is driving a line, which separates "not wired" from "wired but silent" without a meter.

## Measuring the clock without a microphone

With the mic off there are no samples to count, but PPS still disciplines the ESP32's own crystal,
and every I2S rate on this part derives from it. `esp_clock.ppm_vs_gps` compares `esp_timer` against
GPS seconds: **measured +10.23 ppm over 74 s**, with PPS spread 2 µs and zero glitches.

That is the same class of error the 48000-vs-47619 trap belongs to, measured against an independent
reference instead of a datasheet. Over a 300 ms capture window 10 ppm is 3 µs — small, but it is
now a number rather than an assumption, and it is the floor the I2S rate will be measured against
once the external mic lands.

## Is the output tagged with the exact time?

Now yes, and it was not before. Rows used to carry `hh:mm:ss` lifted from NAV-PVT -- one-second
resolution, stale by up to a second, tied to no PPS edge at all.

A PPS edge IS a top-of-second, so the anchor is: latch the local clock at the edge, learn which
second it was from the NAV-PVT that follows, then

    utc_us = edge_unix_us + (local_us - edge_local_us)

`night.csv` now leads with `utc_us` and a `time_valid` column. Measured against an NTP-synced host:
**within 20-30 ms**, which confirms the SECOND is right. It says nothing about microsecond accuracy
-- the host clock is not a reference -- but the second is the part that costs 343 m.

⚠️**The hazard is picking the wrong second, and it looks entirely normal when you do.** NAV-PVT's
epoch sits ~200 ms past the second on this module, so a late report can arrive just after the NEXT
edge and land inside a naive time window, labelling that edge with the previous second. A window
alone cannot catch it. The label must also advance exactly one second per edge; when it does not,
the labelling is rejected and counted in `time.label_rejects` rather than quietly believed.

Two bugs on the way, both worth remembering. Blanking the anchor on every edge left the node unable
to timestamp for the ~200 ms each second before the report arrived -- the last good anchor now
persists, and local and UTC are committed together as one matched pair. And it is the PENDING edge
that must be named, not "the most recent edge", or a late report renames the wrong one.

## OTA, and what happens when a bad image lands

    arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi \
      --output-dir .otabuild/out firmware/hear_node
    curl -F firmware=@.otabuild/out/hear_node.ino.bin http://<ip>/update

`/ota` shows the running partition, the boot counter, and whether this image has been accepted.

**Verified, not asserted.** A good image pushed over WiFi took **5.9 s**, moved app0 -> app1, and came
back healthy. The failback was then exercised by resetting the board four times with 16 s gaps, so
no boot ever reached the 30 s healthy mark: after the third the node **flipped itself back to app0**
and came up clean.

⚠️**The failback does not use the bootloader's rollback feature.** This core ships a prebuilt
bootloader and `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` could not be confirmed, so relying on it
would be relying on something unverified. Instead the app counts its own boots in RTC memory, which
survives a reset; three boots without reaching healthy and it sets the other partition itself.

Healthy means **WiFi joined** as well as running, because an image that boots happily but cannot be
reached is unrecoverable over the air. That case also cannot reboot itself, so an image that has not
become reachable within 90 s restarts deliberately, which advances the counter.

⚠️**What it cannot save you from:** a build that faults before `setup()` runs -- a bad global
constructor, say -- since nothing then increments the counter. That still needs USB. The counter is
the first statement of `setup()` to make that window as small as possible.

## What a good night looks like

`fix` 3+ with 6+ sats, `pps` climbing by 1 per second with `glitches` at 0, spread of tens of µs,
and `acq.fs_clean_hz` settling. **That figure is the deliverable** — if it is not 16000.000, every
timestamp this platform has ever produced was scaled wrong, and now you know by how much. Measured
here: **16001**, about +60 ppm.

Read `acq.fs_clean_hz`, never `i2s.measured_hz`. The latter divides cumulative samples by
cumulative seconds, so a single stall poisons it for the rest of the run — it read 13730 Hz while
the node was really clocking 16001. `fs_clean_hz` averages only over unbroken runs of GPS seconds
and discards any second that lost a block; those discarded seconds are `acq.drop_s`. Expect it
flat at the one or two seconds the I2S peripheral takes to start.

**`drop_s` cannot see a single lost block.** Blocks are 256 samples, so a clean second delivers
either 62 or 63 of them (15872 or 16128) — one missing block lands inside that legitimate spread
and is indistinguishable from it at one-second granularity. The detector is deliberately set to
0.97 to avoid crying wolf at the quantisation, which means it catches losses of two blocks (32 ms)
and up. The number that bounds the small stuff is `fs_clean_hz` over a long window: held at
16000.00 across 466 s, total loss is under 0.1% however it is distributed.

Polling the node does **not** measurably cost audio, though `loop()` does read I2S, serve HTTP and
parse GPS in one thread. Measured over 10.5 minutes at 15-30 s intervals with two concurrent
pollers: three dropped seconds, two of them the I2S start, one at ~8 minutes. That rate is roughly
70 over a night, each losing at least 32 ms -- call it 2 s of audio in 12 hours, 0.005%.
`fs_clean_hz` reconverged to 16000.00 afterwards. An earlier build appeared
to lose 14% under polling, but that reading came from `i2s.measured_hz`, which is cumulative and
was still carrying the boot-second loss — the poller was not the cause.

Also worth a glance: `gate.armed` should be 1, and `gate.headroom` (`e_max_win / thr`) says how
close the night came to triggering. Sustained headroom far below 1 means the threshold is above
everything that happens out there — a distinguishable outcome from silence, which is the point.

## Credentials and watching it

    python3 firmware/hear_node/gen_secrets.py     # reads ~/.wifi, writes gitignored secrets.h

Takes every `WIFI_<n>_SSID`/`WIFI_<n>_PSK` pair and tries each in turn, because an outdoor node may
only reach one of them and which one is not knowable from indoors. It prints a count and masked
names, never the credentials. `secrets.h` is written 0600 and is gitignored.

    nohup python3 firmware/hear_node/watch.py http://<ip> 30 >> ~/dama-hear-night.log 2>&1 &

`watch.py` polls `/status`, appends every sample to `~/dama-hear-night.jsonl`, and prints a line
only when something *changes* — fix gained or lost, the first PPS edge, glitches climbing, a reboot,
the node going away or coming back. A log that prints every poll is a log nobody reads in the
morning. It runs detached and does not depend on any terminal staying open.

## Reaching it without the cable

| | |
|---|---|
| `GET /log` | the boot log, from a 6 kB RAM ring |
| `POST /reboot` | restart. POST only, so a link prefetcher cannot reboot a node by looking at it |
| `POST /update` | firmware, `curl -F firmware=@<bin>` |
| `GET /sd?file=/scene.csv&tail=N` | any file off the card |
| `GET /audio` | what the PSRAM ring currently holds, as JSON |
| `GET /audio?from=<utc_us>&dur=<s>` | that window of raw PCM as a WAV |
| `GET /gate` | the active gate floor, its source, and live ambient / e_max |
| `POST /gate?floor=<n>[&persist=1]` | set it; `?reset=1` restores the compiled-in default |

Every diagnosis worth having so far — the 230400-baud UBX scan, the driven-vs-floating pin probes,
the I²C scan — came out of the boot log, which used to exist only on USB. It survives the cable now.

⚠️**`/log` does not print the network name.** It says `network 1/3`. These endpoints are
unauthenticated and the node is meant to sit outdoors; handing the SSID to anyone who can reach
port 80 is not a trade worth making for a line of log.

⚠️**The failback will not revert a proven image.** Carrying the node out of WiFi range means no
join, never healthy, reboot at 90 s — three of those and the old logic would have rolled back
working firmware because the node had *moved*. `proven_ok` in RTC memory records that a build once
reached healthy; after that, unreachable is treated as what it usually is. The failback still
guards the first boots after an OTA, which is the only window where unreachable really does mean
bad image.
