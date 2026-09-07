# Night node

A XIAO ESP32-S3 Sense left outside overnight, reporting over WiFi. It exists for **one
measurement**: the true I²S sample rate, disciplined against a real GPS PPS.

`firmware/path_test` could never make it — its pulse and `esp_timer` came off the same crystal, so
the interval was that oscillator against itself. A GPS PPS is an independent reference, so counting
samples between edges gives the rate in Hz to GPS accuracy. Over a night the per-block granularity
averages out to well under a ppm.

## Before you flash

    cp firmware/night_node/secrets.h.example firmware/night_node/secrets.h   # then edit
    arduino-cli compile -u -p /dev/ttyACM0 \
      --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi firmware/night_node

`secrets.h` is gitignored. Without it the node starts its own AP (`dama-hear-node` / `damahear`,
http://192.168.4.1/) — fine for a bench check, useless in the garden.

## Wiring

Leaves the onboard PDM mic in place, since the good mic has not arrived.

| node | GPS module |
|---|---|
| D7 (GPIO44) | module TX |
| D6 (GPIO43) | module RX |
| **D11 (GPIO42)** | **PPS** |
| D4 / D5 (GPIO5/6) | SDA / SCL — IST8310 + BMP280 |
| 3V3, GND | VCC, GND |

⚠️**D11 is the PDM microphone's CLK, an output.** This build disables the mic so nothing drives
that pin against the module. Do not re-enable I2S PDM while PPS is wired here.

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
it. Three files, all fetchable over the same link with `/sd?file=/dets.csv&tail=20000`:

| file | written | holds |
|---|---|---|
| `dets.csv` | as detections fire, batched once a second | one row per detection: `utc_us`, `sample`, `pps_n`, signed `us_since_pps`, `trigger`, `flags`, the rate it was timed at, and the log-mel sketch as hex |
| `health.csv` | every 30 s | GPS and PPS quality, the acquisition audit, gate state, detection counters, card space |
| `scene.csv` | every 1.024 s, ungated | the scene descriptor — 20 bands × 4 quarter-second slices of log-mel, hex, plus the microseconds its FFTs cost |

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

The detection sketch is 44 ms of log-mel and only exists when the gate fires. That is enough for a
classifier built beforehand and useless for anything else: the 11.33 h run produced 45 in-run
detections and no way to listen to a single one. (The counter ends on 47; 45 leaves out the two
that fired in the first 12 s of that boot, before the mic had settled. Both are in the capture.)

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

## The scene feature: 1.024 s, ungated

The sketch is an **impulse** descriptor: 8 frames at hop 64 = 704 samples = 44 ms, and it only
exists when the gate fires. Nothing in 11.33 h described the **background**, which is what separates
a chorus from a road. All 45 in-run sketches peaked in the bottom mel band, 312–500 Hz — measured
in `modules/bioacoustic/README.md`, not here.

`scene.csv` is the scene-scale counterpart, the node's analogue of the 0.96 s patch hugbot's YAMNet
consumes. 20 bands × 4 quarter-second slices, 1.024 s of span, written whether or not anything
triggers. It re-uses the shipped filterbank and window **unchanged**: `MEL16_WIN` and `MEL16_FB_*`
are functions of nfft and fs only, not of frame count, so a longer span needs no new tables. The
measured +10.6 ppm rate error is far inside one 62.5 Hz bin of a 256-point FFT, so tables built for
16000.0 stay correct.

`BLOCK` is 256 and `MEL16_NFFT` is 256, so **one I2S block is one FFT frame** — enforced by a
`static_assert`. That is the whole design: the descriptor is built 16 ms at a time at a steady
62.5 FFT/s, instead of a 64-FFT burst once a second that would have to fit inside the DMA's 90 ms
of headroom or drop audio.

Storage, because it writes continuously to a 40 MB partition with 19 MB free: 80 B of mel
hex-encoded to 160 chars plus ~66 chars of columns is **~227 B a row**, and 12 h is 42188 rows =
**9.6 MB**. That fits alongside `health.csv` (~0.3 MB per 12 h) and `dets.csv` and leaves the card
about half empty. Eight slices instead of four would be 16 MB and would not.

**The cost is measured, not estimated.** `scene.fft_us_per_row` in `/status` (and the `fft_us`
column of every row) is the microseconds of FFT and filterbank summed over the 64 frames of one
1.024 s row, taken with `esp_timer_get_time()` around the work itself; `scene.fft_us_max` is the
worst row since boot.

Measured on the node, 115 rows in: **16 842 µs per 1.024 s row — 1.64% of one core**, worst row
17 670 µs. So the scene descriptor is essentially free, and the 44 ms impulse sketch beside it is
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
      --output-dir .otabuild/out firmware/night_node
    curl -F firmware=@.otabuild/out/night_node.ino.bin http://<ip>/update

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

    python3 firmware/night_node/gen_secrets.py     # reads ~/.wifi, writes gitignored secrets.h

Takes every `WIFI_<n>_SSID`/`WIFI_<n>_PSK` pair and tries each in turn, because an outdoor node may
only reach one of them and which one is not knowable from indoors. It prints a count and masked
names, never the credentials. `secrets.h` is written 0600 and is gitignored.

    nohup python3 firmware/night_node/watch.py http://<ip> 30 >> ~/dama-hear-night.log 2>&1 &

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
