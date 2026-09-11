# Board profiles

One header per hardware build. A profile declares **what is wired where and what the part can
do** — nothing else. No behaviour, no policy, no thresholds: those are the platform's, and they
must be identical across boards or the nodes are not comparable.

## Why this exists

`hear_node.ino` reached 2702 lines with its pin map spread through it as bare `#define`s, and
`puc_node.ino` began as a second copy of the same scaffolding because there was nowhere for the
differences to live. Two boards is where that stops being tolerable: a third would mean a third
copy of the OTA failback, the UTC anchor and `csv_open`, and those are the parts that took the
longest to get right and would be the worst to have three slightly-different versions of.

Roughly 2000 of those 2702 lines are platform. About 700 are board. This is the seam.

## What a profile must state, and how it must state it

Every field is **measured or datasheet, never assumed**, and carries how it is known. A profile is
read by whoever is wiring the next node, so a wrong number here becomes a soldering error.

| field | meaning |
|---|---|
| `BOARD_NAME` | matches `hear/nodeclass.py`, so the firmware and the solver agree on what this is |
| `PPS_PIN` | where the GPS timepulse lands. `-1` = not wired, and the node then cannot contribute a TDoA arrival |
| `GPS_RX_PIN` / `GPS_TX_PIN` | node's RX (module TX) and node's TX (module RX) |
| `GPS_PROTO` | `GPS_UBX` or `GPS_PMTK` — these share no configuration commands at all |
| `MIC_KIND` | `MIC_PDM` or `MIC_I2S`, and its pins |
| `FS_NOMINAL` | sample rate. Sets Nyquist, which no filter or integration time recovers past |
| `MIC_BAND_HZ` | what the microphone passes, from its datasheet. The tighter of this and Nyquist binds |
| `SD_*` | card pins, or `-1` if none |
| `I2C_*` | bus pins, or `-1` |
| `HAS_*` | which sensors are fitted, so absent ones report null rather than a plausible zero |

## Wiring is standardised, and one node currently is not

`nyquist` follows `xiao_s3_sense.h` exactly. **`mach` has its GPS TX/RX pair reversed** relative to
it. The firmware detects this at boot and follows the hardware, so it works — but the two nodes
are not the same board, and a third built from the standard would differ from `mach`. Rewire
`mach` to match when it is next on the bench; the auto-detect exists to stop a swap being fatal,
not to make it acceptable.
