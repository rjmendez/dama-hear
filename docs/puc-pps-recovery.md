# PUC PPS recovery

## Root cause and current state

The PUC has two different one-hertz candidates, and they must not be conflated:

- The **DS3231 SQW/INT output is the source actually identified on the stock board**. It is
  open-drain, appears on **GPIO38**, and was causally confirmed by turning SQW off and on while
  watching that pin. It is a local RTC tick, not GNSS PPS: it names no UTC second and cannot
  make the node a `puc-pps` arrival source.
- The Quectel L86 exposes GNSS 1PPS on **module pin 11**, but repeated whole-bank scans found no
  stock route to the ESP32-S3. The GNSS UART is separately measured at GPIO44 (module TX to MCU)
  and GPIO43 (MCU TX to module RX), 9600 baud, PMTK.
- **GPIO17** is the measured safe pad for a future L86 PPS wire. **GPIO18 is held low by another
  circuit** and is unsafe for the L86's push-pull PPS output. GPIO38 is already the RTC SQW net.

The firmware therefore remains `puc-ntp`. It observes GPIO17 without enabling a pull resistor,
reports waveform measurements, and never enables PPS time discipline merely because a signal is
one hertz. `waveform_valid` means only that the electrical timing passed the diagnostic gate;
`source_confirmed` and `discipline_enabled` remain false in this build.

## Diagnostic gate

The `/status` JSON and `/pps` endpoint expose:

- rising and falling edge counts;
- measured frequency from the rising-edge interval;
- minimum/maximum rising interval;
- interval jitter (`max - min`);
- minimum/maximum high pulse width;
- `waveform_valid`.

The strict waveform gate requires at least nine rising edges, every observed interval between
950,000 and 1,050,000 microseconds, pulse widths between 2,000 and 998,000 microseconds, and
at most 5,000 microseconds interval jitter. This is evidence of a stable one-hertz waveform,
not proof of its source. The node class and timing discipline must remain disabled until the
source is independently traced to the L86 and the audio capture path is measured.

## Safe physical procedure

No physical change is required for the current diagnostic-only firmware. If GNSS PPS is to be
added:

1. Power down the PUC and identify the L86 module's pin 11 (`1PPS`) from the module datasheet
   and board silkscreen. Do not infer it from the UART pins.
2. Verify with continuity that the chosen ESP32-S3 pad is GPIO17. Do not use GPIO18, GPIO38,
   flash/PSRAM pins, USB GPIO19/20, or an unmeasured pad.
3. Connect L86 pin 11 to GPIO17 and connect the signal ground to the PUC ground. Confirm the
   logic level is compatible with the ESP32-S3 (the L86 output is 3.3 V logic); do not add a
   pull-up/down or connect the signal to GPIO38.
4. Before installing a timing image, use a scope or logic analyzer at GPIO17 to verify a
   push-pull pulse near 1 Hz, approximately the configured PMTK285 width (100 ms is the normal
   target), with no contention or boot-time disturbance.
5. Boot the diagnostic image and collect `/status` and `/pps` for at least 10 seconds. A valid
   diagnostic result must show the interval, pulse width, and jitter limits above. Also record
   the GNSS fix and the PMTK configuration response; frequency alone cannot distinguish a
   miswired RTC SQW or test generator.

Only after that evidence exists should a separate change consider setting
`PPS_SOURCE_CONFIRMED`, measuring the PUC audio capture path against a reference node, and
changing the node class. This change intentionally does not do those things and does not deploy
or alter any live cluster.
