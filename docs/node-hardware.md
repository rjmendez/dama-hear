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
