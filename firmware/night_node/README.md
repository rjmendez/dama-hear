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

The SD card is the actual record — `night.csv`, appended every 30 s. WiFi is a convenience and an
overnight run must not depend on it.

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

## What a good night looks like

`fix` 3+ with 6+ sats, `pps` climbing by 1 per second with `glitches` at 0, spread of tens of µs,
and `fs` settling on a stable figure. **That figure is the deliverable** — if it is not 16000.000,
every timestamp this platform has ever produced was scaled wrong, and now you know by how much.

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
