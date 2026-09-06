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
