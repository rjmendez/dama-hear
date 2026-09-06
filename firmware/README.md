# Node firmware — proof of concept

Runs the node's DSP on a XIAO ESP32-S3 and measures what it costs. **No microphone, no GPS, no
radio.** It is the compute half only, fed from recorded data, so the chain is provable before a
wire is cut.

    python3 firmware/gen_golden.py                      # vectors from hear/
    arduino-cli compile -u -p /dev/ttyACM0 \
      --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi firmware/hear_poc

`gen_golden.py` emits `hear_poc/golden.h` from `hear/` — the same `_shot` signal
`tests/test_node.py` uses — so the firmware is checked against the Python that produced the field
results, not against opinion. Regenerate it whenever `hear/` changes.

## Measured, XIAO ESP32-S3 at 240 MHz

| | |
|---|---|
| sketch vs Python | **172/172 bytes exact**, ref_db delta 0.0000 dB |
| gate, streaming | **3.41 M sample/s** — 1.41% of one core at 48 kHz, 0.47% at 16 kHz |
| sketch + pack | **2.32 ms** per detection |

The gate number is the one that matters: it runs continuously, and 1.41% of one core is the real
answer to `docs/validation-full-captures.md`'s "nothing about node CPU cost". That doc's arithmetic
estimate — ~3 ops/sample, 0.23% duty at 64 MHz — is optimistic by more than an order of magnitude.
Measured here it is ~70 cycles/sample, which at 64 MHz would be ~5% at 48 kHz. The conclusion
survives; the number does not.

The sketch cost is 2.32 ms against `docs/uplink.md`'s "~0.5 ms on a 64 MHz Cortex-M4F with
CMSIS-DSP". That prediction remains unvalidated — this is scalar C, not CMSIS-DSP, and tabulating
the twiddles only bought 13%, so the cost is in the butterflies themselves. ESP-DSP's
`dsps_fft2r_fc32` is the lever if it ever matters. It does not yet: the field rate was ~427 events
in 123.7 minutes, so 2.32 ms per detection is ~0.01% duty.

## What it does not establish

- **Nothing about capture.** No I2S, no PPS, no clock discipline. The hard part of a node is the
  timebase, and none of it is here.
- **The gate is causal; `hear/` is not.** `hear/node/detect.py` uses `np.convolve(mode='same')`,
  which reads samples that have not arrived. So the firing indices differ by construction, and the
  harness prints the difference rather than hiding it: **8006 here against 8029 in the reference**,
  for an impulse at 8000. The streaming gate is nearer the true onset than the reference is —
  which is the peak-versus-onset problem, visible as a number.
- **Float32, not float64.** It happens to land byte-exact on this vector. That is one vector.

## Sense bring-up

`firmware/sense_bringup` proves the four subsystems a mains-powered recorder node needs, and says
which actually work rather than assuming. Measured on a XIAO ESP32-S3 Sense:

| | result |
|---|---|
| PSRAM | 8.0 MB — **only with `PSRAM=opi`**; the default board option disables it |
| camera | **OV3660** (PID 0x3660), 1600×1200 JPEG ~42 kB, 10/10 frames at 13.9 fps |
| PDM mic | 16 kHz, 99% non-zero, room noise at rms 2573 / peak 7923 |
| WiFi | radio up, scan only — no credentials in the repo |
| microSD | not mounted on CS 21 or 3 |

The SD line is a missing card, not a defect: the test walks both CS candidates because Seeed's own
docs disagree (the wiki says GPIO3, the SD examples use 21), so it asks the hardware instead of
picking one. Insert a card and it will report which CS the expansion actually uses.

The camera is the 3 MP OV3660, not the OV2640 the older kits shipped.

## Path test — live mic, bench PPS

`firmware/path_test` runs the real chain on live audio from the Sense's onboard PDM mic, and
exercises the timing path against a self-generated pulse. No GPS, no radio.

**Wire a jumper from D0 (GPIO1) to D1 (GPIO2)** before running: D0 emits the bench pulse, D1
captures it. The trick is taken from acoustic-triangulation's `twonode_selftest.cpp`, which is
what makes a timing claim testable on a desk instead of in a field.

Measured so far: 320000 samples in 20 s off the PDM mic (16000 Hz at block granularity), the gate
firing on claps, and a real 172 B frame emitted per detection — the same `pack()` layout the
Python produces.

⚠️**A self-generated pulse cannot test clock discipline.** The pulse and `esp_timer` come off the
same crystal, so the interval is that oscillator measured against itself. What it does test: that
the capture path fires at all, its jitter, and the true I2S rate against the CPU clock — the
48000-vs-47619 class of trap. Discipline needs the GPS.

### On generating a 1 Hz pulse, which took five tries

LEDC is the obvious peripheral and cannot do it. `freq = clk / (div * 2^res)`, the divider field
is ~10 bits, and **the ESP32-S3 LEDC timer is 14 bits wide** — the 20-bit range belongs to the
original ESP32. So low frequency needs high resolution and this part runs out of resolution first:
the floor on the 80 MHz APB is 80e6/(1024·2^14) = **4.8 Hz**. Worse, a reachable 10 Hz/14-bit
config returned attach-ok while driving nothing. A hardware timer ISR toggling the pad is less
elegant and works; its jitter lands in the measured spread, so the spread is generator + capture
rather than capture alone.

Two smaller traps on the way: `digitalRead` on an LEDC-driven pad returns nothing useful because
the input buffer is off, which made a working generator look dead — `GPIO_MODE_INPUT_OUTPUT` fixes
it. And a generator self-check is worth the ten lines: without one, "no pulse" and "no jumper" are
indistinguishable, and the first version blamed the wiring for a code fault.
