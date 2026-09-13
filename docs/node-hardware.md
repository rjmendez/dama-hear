# Tier-1 node

A microphone, a clock, and a radio. It detects, sketches and ships; it decides nothing.

## Bill of materials

You supply the MCU, LoRa, battery and solar. The MCU is not settled; the sensor set below
is independent of it.

| item | each | ×3 |
|---|---|---|
| GPS with PPS — Adafruit Ultimate GPS #746 (PPS on V3) | $29.95 | $89.85 |
| I2S mic — Adafruit ICS-43434 #6049 | $8.95 | $26.85 |
| BME280 — temperature, pressure, humidity | ~$10 | ~$30 |

⚠️**Check the GPS module before buying this too.** A drone GPS with a compass usually carries a
barometer on the same I²C bus, and it is already wired. Measured on an HGLRC HG-M10-02: `0x0E`
IST8310 magnetometer and `0x76` **BMP280** — temperature and pressure, no humidity. Humidity is a
~0.1% effect on `c` at these temperatures (`hear/node/telemetry.py`), so a BMP280 buys essentially
the whole acoustic-thermometer argument for nothing. Note where it sits: on the GPS, which has to
be in the open for sky view, so watch for solar heating of the enclosure biasing the air
temperature it reports.

⚠️**Check the GPS modules you already own first.** Most u-blox breakouts have PPS. Power one
outdoors and watch for an LED blinking *once per second* on fix — that blink IS the PPS signal,
and if it is not on a header you can tap it at the LED pad. Cheaper equivalents: ATGM336H ~$8,
NEO-6M ~$9, NEO-M8N ~$16. These nodes never move, so they need PPS and NMEA, not RTK.

## The BME280 is an acoustic sensor here

`c = 331.3 + 0.606 * T`. One degree is 0.606 m/s, which is **183 µs over 35 m** — larger than
the clock synchronisation this whole design exists to protect. A BME280 per node gives the speed
of sound *at each sensor* rather than one global figure.

The 2026-09-05 session recorded no temperature at all; `c` had to be reconstructed afterwards
from the operator's recollection of the afternoon. `telemetry.pack()` sends a sentinel rather
than a zero when the sensor is absent, so a missing BME280 produces "no temperature" instead of
a plausible-looking, wrong 331.3 m/s.

Pressure doubles as an altitude cross-check for the 3D geometry.

## What the ICS-43434 can and cannot do

- **AOP 120 dB SPL.** A rifle report is ~160 dB at 1 m and 125–135 dB at 50 m, so **it will
  clip** — matching the 15% clipping already measured in the field. Clipping is time-valid and
  amplitude-invalid: arrival times survive, so trajectory work is unaffected. What is lost is the
  amplitude cue, and `ref_db` alone was worth AUC 0.90.
- **50 Hz – 15 kHz**, low-passed above 24 kHz. No ultrasonic content, so N-wave *shape* is out of
  reach on this part at any sample rate.
- **23–51.6 kHz I2S**, which is nearly moot: the nRF52840 I2S cannot produce 48000 Hz at all
  and its usable ceiling is 50.000 kHz. The two parts meet just above 50 kHz and no higher.

⚠️**Use an I2S mic, not PDM.** The nRF52840 PDM peripheral is hard-capped at 16 kHz with fixed
÷64/÷80 decimation. A PDM mic silently locks the node out of the band it needs.

## What a node sends

| | size | when |
|---|---|---|
| event sketch | 172 B | per detection (see `docs/uplink.md`) |
| telemetry | 14 B | periodic — environment, soundscape level, GPS, battery |

Both fit a 237 B Meshtastic payload with room to spare. Telemetry decodes to a dama-shaped node
payload via `telemetry.to_dama()`, so a hear node is not a special case downstream — it is another
fleet node with a thinner sensor set and `node_type: "hear"`.

## Pin cost

The sensor set needs eight GPIO: three for I2S, one for PPS, two for I2C (shared with anything
else on the bus), one UART pair for GNSS NMEA. Carriers that break out fewer than that decide the
build — `docs/faketec-pin-budget.md` is one that did, worked through pad by pad.

## ⚠️mach's onboard PDM microphone has a raised, flat noise floor (mechanism NOT established)

Measured 2026-09-08 over the drained `scene.csv` from both nodes (5653 mach rows, 7077 nyquist,
19 boot sessions). Both files declare the same band axis on **every** row — `bands,slices,span_ms,
f_lo,f_hi = 20,4,1024,62.5,7812.5` — so this is not an axis confound, and the int8 window clamp
touches 0.0009% of mach cells, so no floor estimate is clamp-biased.

| | mach | nyquist |
|---|---|---|
| per-row minimum cell, median | 38.8 dB (SD 0.81) | 24.5 dB (SD 2.03) |
| per-boot floor, all sessions | 36.8–39.0 dB (15) | 23.8–26.2 dB (4) — no overlap |
| p10 spread across bands 3–18 | **0.50 dB** | 1.75 dB pooled; 1.50/2.50/15.00/16.50 per boot |
| row max (ref_db), median / p99 | 52.5 / 89.6 dB | 50.2 / 83.0 dB |

Read the frequencies off the **scene** bank, not the detection bank: `firmware/hear_node/
mel_scene.h` puts band 3 at 437.5 Hz, band 7 at 1062.5–1437.5 Hz, band 11 at 2062.5–2687.5 Hz,
band 18 at 5375–6875 Hz. (`hear/sketch.py`'s F_LO/F_HI of 300/20000 belong to the DETECTION bank
and band *k* is a different frequency in each — `hear_node.ino` says so where the two are
defined. An earlier write-up of this measurement labelled band 7 as "500 Hz" and bands 7–18 as
"3.5 octaves" by taking the wrong axis; corrected, the flat span is bands 3–18 = 437.5–6875 Hz =
**3.97 octaves flat to 0.50 dB**, which is wider than the claim it replaces.)

What this rules out, measured:

* **Not a quieter place.** mach reaches HIGHER peaks than nyquist, so the compressed range is a
  rising floor, not a falling ceiling. And over the 42.8 min both nodes were simultaneously
  UTC-anchored, mach's p10 minus nyquist's is +15.1 dB at band 19 (~6.9 kHz peak) and +12.8 dB at
  band 7 but **−3.0 dB** at band 11 and −3.9 dB at band 15. Elevation and indoor attenuation are
  monotone in frequency; this is not.
* **Not deafness.** mach band 0 (62.5 Hz) has SD 1.74 dB and lag-1 Pearson 0.79. The LF path is
  intact; the floor takes over above band ~6.
* **Not the node name, and not the card.** See `tests/test_node_identity.py` for the 276 rows in
  mach's own file that carry nyquist's `NODE_ID`.

What it costs: on the 42.8 min overlap, 10 of mach's 20 bands vary by **less than one 0.5 dB
encoding step** over the whole window, so above band ~10 mach contributes rounding noise rather
than scene. A per-dimension-standardised cosine over those rows measures quantisation, not the
room (mach 0.100 at lag 1 vs nyquist 0.825; the RAW row-to-row cosine is 1.000/0.960 for mach,
*higher* than nyquist's 0.994/0.892). Treat mach's bands 7–19 as unusable rather than quiet, and
do not "fix" it by rescaling — the information is not in the file.

**Mechanism NOT established.** What is established is that the floor is additive, spectrally
flat, temporally rigid, present in all 15 boots, and still present on the current build and card
(live `GET /status`: mach `audio.ambient` 20.2 / `env_peak` 951 against nyquist 15.7 / 6080).
Mic self-noise, a supply/decoupling problem and PDM clock jitter are all consistent with it and
none has been distinguished; no hardware was opened. Do not report a part as the cause.

**What would settle it.** Seal or electrically disconnect mach's mic and re-measure the per-row
minimum and the bands 3–18 flatness: a floor that stays at ~38.8 dB and ~0.5 dB flat with no
acoustic input is downstream of the transducer. Cheaper and needing no disassembly: **swap the
two boards between their positions and re-drain.** If the 38.8 dB / 0.5 dB signature follows the
BOARD it is the unit; if it stays at the POSITION it is the site. Until one of those runs, "the
mach device recorded it" is an inference from the signature, not a measurement of the hardware.

## ⚠️`valid_nmea: 0` is not a fault when `ubx_pvt` is climbing

nyquist's u-blox is **UBX-binary only**, and that is a property of the module, not of this
firmware: `gps_configure()` sets timepulse keys and message rates and never touches a
protocol-out key. Measured live, read-only, 2026-09-08:

* `/log` shows the boot-time baud sweep trying all 8 candidates — 9600/19200/38400/57600/115200/
  230400/460800/4800 — and reporting **0 NMEA at every one**; only 230400 saw UBX frames.
* `/gpsraw` returns 64,954 raw bytes with `valid_nmea_lines = 0` and no `$` talker sentence
  anywhere in the dump, while `/status` shows fix 3, 18 sats, `ubx_pvt` climbing, `tacc_ns` 26,
  PPS spread 4 µs, 0 glitches.
* mach, on the same firmware, gives 6,325 valid NMEA lines in 428,978 bytes of genuine ASCII
  `$GPGSV/$GAGSV/$GBGSV/$GQGSV`.

The `sentences` counter is misleading by construction: `nmea_line()` counts every LF-terminated
chunk over 6 bytes, and LF bytes occur inside binary UBX payloads, so `sentences` climbs while
`valid_nmea` correctly stays at 0. The node's own web page now says "UBX-binary only (no NMEA,
and none needed)" instead of printing a bare zero. This is a *different* problem from the one
`POST /gpspins` addresses, which is a module that never talks at all.
