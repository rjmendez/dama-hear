# fakeTec pin budget

> **Closed route.** The nodes are not being built on fakeTec. Kept because the pin budget is why:
> the sensor set needs exactly the four GPIO the board has spare, three of them mid-board pads on
> the ProMicro module. That is a carrier limit, and it is what sent the build elsewhere.

The node has to fit a microphone, a PPS input and a BME280 onto a board designed to be a
Meshtastic tracker and nothing else. It fits, with **zero pins spare**.

## What fakeTec actually routes

Traced from `fakeTecv5.kicad_pcb` in [gargomoma/fakeTec_pcb](https://github.com/gargomoma/fakeTec_pcb),
pad-by-pad, against the pin table in Meshtastic's `nrf52_promicro_diy_tcxo/variant.h`. The two
13-pad rows J1/J2 are the ProMicro/SuperMini footprint, and the pad order runs opposite to the
variant's table — confirmed independently on three pads (J2.2=SDA=P1.04, J2.3=SCL=P0.11,
J1.8=Batt%=P0.31).

| ProMicro pin | fakeTec net | reachable at | free for us? |
|---|---|---|---|
| P0.02 / P1.15 / P1.11 / P1.13 / P0.29 / P0.10 / P0.09 | LoRa MISO/MOSI/SCK/CS/BUSY/DIO1/RST | J4 | no — radio |
| P0.17 | RXEN | J2.8, J4.6 | no — radio |
| P0.31 | Batt% | J1.8 | no — battery sense |
| P0.11 / P1.04 | SCL / SDA | **J5** (GND/3V3/SCL/SDA) | shared — see below |
| P1.00 | BTN | J2.4, J14.1 | no — button |
| P0.13 | 3V3_EN | J1.9 | no — peripheral rail |
| P0.20 / P0.22 | *(variant: GPS TX/RX)* | **J14.4 / J14.3** | already the GPS UART |
| P0.24 | T1G | J2.5, J14.2 | MOSFET gate (variant calls it `PIN_GPS_EN`) |
| P0.08 | T2G | J2.11 | MOSFET gate |
| P0.06 | T3G | J2.12 | MOSFET gate |
| P1.06 | *(nothing)* | ProMicro pad only | **free** |
| P1.01, P1.02, P1.07 | *(nothing)* | ProMicro **mid-board** pads | **free** |

⚠️**P0.06 and P0.08 are MOSFET gates on this board, not a spare UART.** The Meshtastic variant
calls them `Serial2 RX/TX` and the fakeTec schematic wires them to SI2312 gates. Firmware that
grabs them as general IO is switching a power rail.

## The budget

| need | pins | where it goes |
|---|---|---|
| I2S — SCK, LRCK, SDIN | 3 | P1.01, P1.02, P1.07 |
| GPS PPS — one GPIOTE capture input | 1 | P1.06 |
| BME280 — I2C | 0 | shares SCL/SDA on **J5**, the OLED port |
| GPS NMEA — UART | 0 | already on **J14.3 / J14.4** |
| | **4 of 4 free pins** | |

The sensor set costs exactly the four pins the board has left, and nothing is spare. An OLED and
the BME280 can share J5 — different I2C addresses, one bus.

⚠️**Three of those four pins are mid-board pads on the ProMicro module, not fakeTec headers.**
P1.01/P1.02/P1.07 are listed as "Mid board / Free pin" in the Meshtastic variant table and are
not routed anywhere by fakeTec. The mic wires solder to the module, under or beside it, on a
board designed to sit flat in a Heltec-v3 case. *Confirm this against the physical SuperMini
before ordering the mic — the pads are documented but their presence varies by clone.*

If a build can give up the MOSFETs, T2G/T3G (P0.06/P0.08) return two easier pins at J2.11/J2.12
and the mid-board count drops from three to one. That is the trade: power switching for solder
access.

## Sample rate: 48 kHz is not available

⚠️**The nRF52840 I2S cannot produce 48000 Hz.** LRCK = MCK / RATIO, and from Nordic's own HAL
(`nrfx/hal/nrf_i2s.h`) MCK is 32 MHz over a fixed divider list {2,3,4,5,6,8,10,11,15,16,21,23,
30,31,32,42,63,125} and RATIO is one of {32,48,64,96,128,192,256,384,512}. No pair gives 48 kHz.

| setting | LRCK | note |
|---|---|---|
| MCKFREQ 32MDIV10 (3.2 MHz), RATIO 64X | **50000.000 Hz** | exact |
| MCKFREQ 32MDIV5 (6.4 MHz), RATIO 128X | 50000.000 Hz | exact, same rate |
| MCKFREQ 32MDIV21 (1.5238 MHz), RATIO 32X | 47619.05 Hz | the "close to 48k" trap |

**Use 50.000 kHz.** It is exact, it is above 48 kHz rather than below, and it sits inside the
ICS-43434's 23–51.6 kHz range. SCK is then 3.2 MHz, near that part's ceiling but within it, with
`SWIDTH = 24BitIn32` and `RATIO = 64X` matching how the mic frames its 24-bit word.

A node that configures 47619.05 Hz and calls it 48 kHz is **0.79% wrong about time**: 2.4 ms over
a 300 ms capture window, against a design whose whole argument is that PPS holds the clock to
~30 ns. This is the 2026-09-05 failure mode again — a rate that was knowable at capture time and
simply not written down.

Nothing in `hear/` needs to change for this: `fs` is a parameter everywhere and 48000 appears
only as a test constant. What must change is that the **node reports its true rate** and the
central side uses it, rather than either end assuming.

## What can be proven today, with only the board

No mic, no GPS, no BME280 — these still work:

1. **Flash the `nrf52_promicro_diy_tcxo` variant** and confirm the radio comes up. There is no
   `_xtal` variant to get wrong any more -- it was removed, because `_tcxo` now defines
   `TCXO_OPTIONAL` and tries DIO3 at 1.8 V and then XTAL at 0.0 V on every boot. Older guides
   (including the fakeTec README) still link to `_xtal`; that link is dead.
2. **Measure the airtime claim.** ⚠️The claim this item was written against — "172 B is ~250 ms
   at SF7 and exceeds the 400 ms FCC dwell at SF9" — has since been **withdrawn**: it was
   `payload_bits / bitrate` at BW 125, which omits preamble, header and symbol quantisation and
   under-reports by 10–17 %, and no US Meshtastic preset uses that bandwidth at those SFs
   anyway. See `docs/uplink.md`'s Airtime section for the preset-mapped replacement. The
   measurement is still worth doing: two boards and a 173 B payload on a private portnum settles
   it against the radio instead of against a formula.
3. **Confirm the four free pads exist** on the actual SuperMini, with a meter. This is the one
   finding above that is documentation rather than measurement, and it gates the mic purchase.
4. **Golden vectors.** Freeze `Gate` → `sketch` → `pack` outputs for a fixed input as byte-exact
   test data, so the firmware port is checkable against the Python rather than against opinion.
   Pure host-side work, no hardware at all.

Nothing here needs the sensors, and item 3 should happen before they are ordered.

## If the radio never starts

Meshtastic will still enumerate over USB and answer `--info` with no radio at all; the tell is
every outbound packet dying `Routing.Error=4 NO_INTERFACE` with `air_util_tx` at `0.000000`.
The boot log's `SX126x init result` is the diagnosis: **-2** is `CHIP_NOT_FOUND`, nothing
answering on SPI. **-707** is `SPI_CMD_FAILED`, and is only reachable after RadioLib has read the
string `SX1262` back over the bus — so it proves SPI, RESET and the module's rail are good, and
the fault is the reference clock. Seeing -707 at both `Vref 1.8V` and `Vref 0.0V` rules out the
TCXO-versus-crystal question too. This applies to any SX1262 build, not just this one.
