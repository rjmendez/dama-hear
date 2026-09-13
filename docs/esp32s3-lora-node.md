# Building the ESP32-S3 + RFM95 node

A full-size ESP32-S3 breakout carrying an RFM95W for Meshtastic, plus the node sensor set: GPS
with 1PPS, microphone, microSD, I²C environment.

**Status: nothing is built and nothing is measured.** Everything below that names a pin is
DERIVED — from the installed ESP-IDF headers, from a datasheet, or from another board's
convention. Derived is the state in which this repo has twice put a wrong pin under a soldering
iron:

* the Quectel L86's 1PPS was documented on module **pin 11** when its Hardware Design Table 3
  says **pin 6**;
* the PUC's PPS landing pad was named **GPIO18** on the argument "reads low and is not a
  strapping pin" — both true, both insufficient — when GPIO18 is externally driven and
  **GPIO17** is the pad that floats. `tests/test_puc_pps_pin.py` carries that measurement.

So **step 1 is not wiring. Step 1 is the scan.** The instrument already exists in this repo and
it is what caught the GPIO18 error.

The pin map is `firmware/boards/esp32s3_lora.h`; `tests/test_esp32s3_lora_board.py` checks it
against what the silicon forbids. Read both. This page is the procedure.

---

## 0. Bill of materials

| item | note |
|---|---|
| ESP32-S3 breakout, full-size | ⚠️**PSRAM type decides five pins** — see §2. Any module works; you must find out which one you have. |
| RFM95W module, 915 MHz | SX1276 core. Datasheet Table 1 gives the 868/915 variant −111 to −136 dBm. |
| ¼-wave 915 MHz antenna + pigtail | **Never power the radio without an antenna.** |
| GPS with 1PPS | u-blox assumed (`GPS_PROTO GPS_UBX`). `docs/node-hardware.md` lists parts and the "check what you already own" argument. |
| I²S or PDM MEMS microphone | Part not chosen. `docs/node-hardware.md` argues for I²S; the header currently declares PDM because that is what the fleet's corpus came from. |
| BMP280/BME280 | An acoustic sensor here: `c = 331.3 + 0.606·T`, so one unmeasured degree is 183 µs over 35 m — the whole one-way arrival budget. |
| microSD breakout | On its **own** SPI bus, not the radio's. §3. |

---

## 1. FIRST: scan the bare breakout

Do this **before any component is soldered**, on a board with nothing attached but USB.

### What the instrument is

`firmware/puc_node/puc_node.ino` serves three whole-bank pin scans:

| endpoint | what it does |
|---|---|
| `/scan` | samples every safe pin for 4 s with pins left as found |
| `/scanpd` | same, with the internal **pulldown** engaged on every pin |
| `/scanpu` | same, with the internal **pullup** engaged on every pin |

`pin_scan()` (`puc_node.ino:634`) samples `GPIO.in` and `GPIO.in1` on a paced loop —
`SCAN_N` 120000 samples across the window, ~30 kHz per pin — and reports per pin: edge count,
high duty, shortest run, and a verdict line for the three signals it can name (a ~1 Hz ~10 %-duty
pulse is a 1PPS, a ~100 µs minimum bit is 9600 baud, a >20 kHz toggle is a clock).

### Why the pullup run is the decisive one

`puc_node.ino:202-206` states it, and it is the whole reason GPIO18 was caught:

> An UNCONNECTED input follows whatever pull is applied, so it reads HIGH. A pin wired to a
> push-pull driver that is currently low does NOT follow it, and reads LOW. Reset-state and
> pulldown cannot tell those apart — both read low either way.

So a pad is free only if it reads **HIGH under `/scanpu` and low under `/scanpd`**. One polarity
proves nothing. This is the measurement that produced the PUC table:

```
gpio   pullup   pulldown   reading
  15    HIGH      low      floats -- free
  16    HIGH      low      floats -- free
  17    HIGH      low      free, and where the joint is
  18    low       low      HELD LOW -- something external owns this net
  39    low       low      HELD LOW -- and puc.h's FREE_PADS lists it, wrongly
  38    8 edges, 50% duty -- the DS3231 1 Hz, which proves the scan can see 1 Hz at all
```

⚠️**That table is the PUC's and it does not transfer.** A different board drives different
nets. Run it on *your* breakout and write down *your* table.

### Running it on a bare breakout

```
arduino-cli compile --fqbn esp32:esp32:esp32s3:PSRAM=opi --libraries firmware/lib \
  firmware/puc_node
arduino-cli upload -p /dev/ttyACM0 --fqbn esp32:esp32:esp32s3:PSRAM=opi firmware/puc_node
# then, once it has joined the AP:
curl http://<node>/scanpu
curl http://<node>/scanpd
curl http://<node>/scan
```

Four things will bite, in this order:

1. ⚠️**`/scanpu` needs PSRAM, or it returns `PSRAM alloc failed`.** `pin_scan()` allocates two
   `SCAN_N × 4 B` buffers with `ps_malloc` — 960 kB, far past heap. If the FQBN's PSRAM option
   does not match the module, the scan does not run at all. That failure is also the answer to
   §2, so it is not wasted.
2. ⚠️**The build needs credentials or it will not compile.** `hear_wifi_guard.h` turns a missing
   `secrets.h` into a compile error on purpose — see `docs/`'s note on the secretless build,
   which once came up as its own AP and got called bricked.
3. ⚠️**`SAFE[]` does not include 43 and 44.** `puc_node.ino:197-198` omits them because the PUC's
   GPS lives there. On a breakout carrying a **CH340/CP2102 USB-serial bridge**, 43/44 are wired
   to that bridge and are *not free* — and the scan as shipped will not tell you. Add them to
   `SAFE[]` for this run, or meter them. This is the one pin pair in the map most likely to be
   wrong on a given breakout.
4. `SAFE[]` also omits 19/20 (USB), 22–25 (absent), and 26–37 (flash and octal PSRAM). Those
   omissions are correct; leave them.

### The gate

Every pin in `firmware/boards/esp32s3_lora.h` must appear in *your* table as **HIGH/low =
floats** before anything is soldered to it. Then change that pin's comment in the header from
`UNVERIFIED` to `MEASURED` with the date. A pin that reads held, or that toggles, is not free —
pick another from `FREE_PADS` and re-run the test.

---

## 2. SECOND: settle the PSRAM question

**Octal PSRAM consumes GPIO 33–37. Quad and no-PSRAM leave them free.**
`spi_pins.h:18-22` names them: 33 D4, 34 D5, 35 D6, 36 D7, 37 DQS, "used ONLY when the part has
OCTAL flash or PSRAM".

You cannot read this off the part number reliably, and the Arduino core shows why — its board
defaults disagree with each other and with how this repo actually builds:

| `boards.txt` entry | default `build.psram_type` |
|---|---|
| `esp32s3` — *ESP32S3 Dev Module* (:1162, :1204) | `qspi` |
| `esp32s3-octal` — *ESP32S3 Dev Module Octal (WROOM2)* (:2372, :2414) | `opi` |
| `XIAO_ESP32S3` (:38220, :38266) | `qspi` |

…and the fleet's XIAO nodes are built **octal** anyway, because `firmware/night_node/flash.py:34`
overrides the default with `esp32:esp32:XIAO_ESP32S3:PSRAM=opi`
(`firmware/README.md:53`: "8.0 MB — **only with `PSRAM=opi`**; the default board option disables
it"). The board entry's default tells you what the core guesses. It does not tell you what is on
your module.

### The empirical test, which is decisive

Build the scan firmware twice and read `psram` off `/` or `/status`:

| FQBN option | `psram` reports | conclusion |
|---|---|---|
| `:PSRAM=opi` | non-zero | **OCTAL** — 33–37 are the PSRAM bus, hands off |
| `:PSRAM=opi` | 0, or `/scanpu` says `PSRAM alloc failed` | not octal; try the next row |
| `:PSRAM=enabled` (QSPI) | non-zero | **QUAD** — 33–37 are free pads |
| both | 0 | **no PSRAM** — 33–37 free, and `/scanpu` cannot run at all (§1) |

### What changes

**Nothing in the shipped map.** `firmware/boards/esp32s3_lora.h` uses none of 33–37 on purpose,
and `test_uses_no_pin_that_octal_psram_would_take` keeps it that way. Ten pads are still free
after every assignment, so avoiding the question costs nothing.

| | pads available beyond the map |
|---|---|
| **Octal** (e.g. a WROOM-2-based module) | `{1, 2, 6, 15, 16, 17, 18, 38, 39, 40}` |
| **Quad or none** | the same ten, **plus 33, 34, 35, 36, 37** |

⚠️No PSRAM at all also costs the node its 240 s raw-audio ring
(`firmware/night_node/README.md:118` — 7.68 MB of the 8.34 MB). That is a capability decision,
not just a pin one.

---

## 3. The pin map

Authoritative copy: `firmware/boards/esp32s3_lora.h`. This table is the same numbers with the
provenance column spelled out.

| GPIO | role | wired to | how it is known |
|---:|---|---|---|
| 44 | GPS RX ← module TX | GPS TX | `uart_pins.h:23` U0RXD IO_MUX pad; convention of both existing boards. ⚠️**UNVERIFIED**, and see §1 note 3. |
| 43 | GPS TX → module RX | GPS RX | `uart_pins.h:24` U0TXD. ⚠️UNVERIFIED. |
| 4 | 1PPS in | GPS PPS | Candidate only. ⚠️UNVERIFIED — must read floats on `/scanpu`. |
| 10 | LoRa NSS | RFM95 **pin 5** | `spi_pins.h:30` SPI2 IO_MUX CS. Corroborated by Meshtastic `variants/esp32s3/tbeam-s3-core/variant.h`. |
| 11 | LoRa MOSI | RFM95 **pin 3** | `spi_pins.h:31` SPI2 IO_MUX MOSI; same corroboration. |
| 12 | LoRa SCK | RFM95 **pin 4** | `spi_pins.h:32` SPI2 IO_MUX CLK; same. |
| 13 | LoRa MISO | RFM95 **pin 2** | `spi_pins.h:33` SPI2 IO_MUX MISO; same. |
| 14 | LoRa DIO0 (IRQ) | RFM95 **pin 14** | **Required** — see §4. ⚠️UNVERIFIED pad. |
| 21 | LoRa RESET, active low | RFM95 **pin 6** | Table 2; but see the §4 contradiction. ⚠️UNVERIFIED pad. |
| — | LoRa DIO1 | *not wired* | Deliberate; §4. |
| 42 | mic CLK | mic | Convention from `xiao_s3_sense.h`. ⚠️UNVERIFIED. |
| 41 | mic DIN | mic | Same. ⚠️UNVERIFIED. |
| 7 | SD SCK | microSD | Convention from `xiao_s3_sense.h`; **SPI3**, §3.1. ⚠️UNVERIFIED. |
| 8 | SD MISO | microSD | Same. ⚠️UNVERIFIED. |
| 9 | SD MOSI | microSD | Same. ⚠️UNVERIFIED. |
| 5 | SD CS | microSD | Dedicated CS. ⚠️UNVERIFIED. |
| 47 | I²C SDA | BMP280 | Convention from `puc.h`, where 47/48 is **measured** (six devices answered `/i2c`). ⚠️UNVERIFIED here. |
| 48 | I²C SCL | BMP280 | Same. |

**Forbidden, and why:**

| pins | reason | source |
|---|---|---|
| 22, 23, 24, 25 | **do not exist on the die** | `soc_caps.h:187` — `SOC_GPIO_VALID_GPIO_MASK` clears BIT22..BIT25 |
| 26–32 | MSPI flash bus, never usable | `spi_pins.h:11-17` |
| 33–37 | MSPI octal PSRAM data — **only if octal** | `spi_pins.h:18-22`, and §2 |
| 19, 20 | native USB D−/D+ | `usb_pins.h:26-27` |
| 0, 3, 45, 46 | strapping — ⚠️**DATASHEET, UNVERIFIED**, §5 | not in the toolchain |

⚠️**There are no input-only pins on ESP32-S3.** `soc_caps.h:189` sets
`SOC_GPIO_VALID_OUTPUT_GPIO_MASK` equal to the input mask. Any table that marks an S3 pin
input-only is an ESP32-classic table being read for the wrong part.

⚠️**41 and 42 are not USB pads.** `usb_pins.h` also defines `USBPHY_VP_NUM 42` and
`USBPHY_VM_NUM 41`, and its own header comment says those external-FSLS-PHY macros are
deprecated and "meaningless" because the signals route to any GPIO through the matrix. 41/42
carry the microphone on every XIAO node in the fleet today. Only 19/20 are real.

⚠️**9 and 14 are listed in `spi_pins.h` as SPI2's HD and WP pads and are still ordinary GPIO
here.** `spi_pins.h:24-27` says the IO_MUX sets are a *routing choice* for quad/octal SPI, not a
reservation. A 4-wire SPI2 master leaves both free.

### 3.1 Two SPI buses, not one

The LoRa module is on **SPI2** (the IO_MUX fast path, so the bus skips the GPIO matrix). The SD
card is on **SPI3**, which has no IO_MUX pads at all (`spi_pins.h:46`: "SPI3 have no iomux pins")
and so goes through the matrix — a little skew, nothing that matters at SD clock rates.

⚠️**They must not share.** Two chip selects on one bus is electrically legal and is the wrong
trade: a DIO0 edge arrives during an SD block write, and the radio's service and the card's
transaction contend for one peripheral at interrupt time. It would also *look fine on the bench*,
where nothing is detecting while the card is written.
`test_the_lora_bus_does_not_collide_with_the_sd_bus` checks both the pins and the host index, so
a later edit cannot merge them quietly.

---

## 4. The RFM95W: which pins the driver actually needs

**Signal set — from Meshtastic's own source** (github.com/meshtastic/firmware, read 2026-09-10):

```
src/mesh/RadioInterface.cpp:466   new RF95Interface(loraHal, LORA_CS, RF95_IRQ,
                                                    RF95_RESET, RF95_DIO1)
src/RF95Configuration.h           #define RF95_IRQ  LORA_DIO0
                                  #define RF95_DIO1 LORA_DIO1  // "not really used for RF95"
src/mesh/RF95Interface.h:51       setRadioIsr() { lora->setDio0Action(callback, RISING); }
```

`setDio0Action` is the **only** interrupt that path arms. So:

| line | needed? |
|---|---|
| SCK, MOSI, MISO, NSS | **yes** |
| DIO0 | **yes** — the RX/TX-done interrupt |
| RESET | **yes** |
| DIO1 | **no.** Passed into RadioLib's `Module` and never used. Wire it only if a later driver moves to pure-SX127x FSK timeouts. |
| DIO2–DIO5 | not referenced |
| RXEN / TXEN | only on modules with an external RF switch (`RF95_RXEN`/`RF95_TXEN` are `#ifdef`'d). A bare RFM95W has none. |

**Module pin numbers — RFM95W/96W/98W datasheet Version 2.0, §1.4 Table 2 "Pin Description",
page 11/123:**

| pin | name | | pin | name |
|---:|---|---|---:|---|
| 1 | GND | | 9 | ANT |
| 2 | MISO | | 10 | GND |
| 3 | MOSI | | 11 | **DIO3** |
| 4 | SCK | | 12 | **DIO4** |
| 5 | NSS | | 13 | 3.3 V |
| 6 | **RESET** | | 14 | **DIO0** |
| 7 | DIO5 | | 15 | DIO1 |
| 8 | GND | | 16 | DIO2 |

⚠️**DIO0 is pin 14.** Not 7, not 8. And 11/12 are DIO3/DIO4 — *out of numerical order*, with
DIO1 and DIO2 at 15 and 16. This is the same shape as the L86 error: a plausible pin number that
is the wrong one.

⚠️**THE DATASHEET CONTRADICTS ITSELF ABOUT RESET.** §7.2.2 "Manual Reset" of that same document
says:

> Pin 7 should be pulled low for a hundred microseconds, and then released. The user should then
> wait for 5 ms before using the chip.

Table 2 says pin 7 is **DIO5** and pin 6 is RESET. The PDF's own metadata names it
`SX1272DS_V0'8.book`, so §7.2.2 reads as chip-level text carried over from Semtech while Table 2
is the module-level table — **but that is an inference, not something established here.**
Table 2 is the one to wire to, and **RESET must be confirmed by continuity on the actual module
before power is applied**. Behaviour either way: active low, ~100 µs, then 5 ms before use.

---

## 5. Strapping pins — what I could not verify

The installed ESP-IDF headers **do not enumerate the ESP32-S3 strapping pins.** What they contain
is fragmentary, and in one place self-contradictory:

| source | says |
|---|---|
| `esp_rom/esp32s3/include/esp32s3/rom/efuse.h:210-211` | ROM UART print gated on **GPIO46** at digital reset |
| `soc/esp32s3/register/soc/efuse_struct.h:442-444` | the *same* control, `uart_print_control`, described as **"GPIO8** is low at reset" |
| `soc/esp32s3/register/soc/efuse_reg.h:502-504` | JTAG source selected by **"strapping gpio10"**, gated on an eFuse |

GPIO0, GPIO3 and GPIO45 appear **nowhere** in the toolchain as straps.

The `rom/` header is the target-specific one, and `efuse_struct.h`'s GPIO8 line reads like text
carried over from another target — but that was not established and is not asserted here.

**So: `{0, 3, 45, 46}` is a DATASHEET claim** — ESP32-S3 datasheet, chapter 2 "Pin Definitions",
the section titled "Strapping Pins" — **that the operator must confirm against that document.**
Inside this repo it is corroborated only by `firmware/puc_node/puc_node.ino:222` ("GPIO45 and 46
are strapping pins"), which is the same class of claim, not an independent check.

**The build does not depend on the answer.** None of the four is used, and
`test_uses_no_pin_believed_to_be_a_strapping_pin` keeps it that way. Strapping pins are sampled
at reset, not at runtime, which is also why `/scanpu` can safely include them.

---

## 6. Wiring order

Only after §1 and §2 are done and every `UNVERIFIED` in the header has become `MEASURED`.

1. **Antenna on the RFM95 first.** Transmitting into an open port can damage the PA.
2. **Ground and 3.3 V** to the RFM95 (pins 1, 8, 10, 16 GND; pin 13 3.3 V). Power it from the
   board's 3.3 V rail, not 5 V.
3. **Continuity-check RESET.** Meter from the module's RESET land to pin 6 *and* to pin 7 and
   confirm which one it is before §4's contradiction becomes a dead chip.
4. **SPI**: 12→pin 4 SCK, 11→pin 3 MOSI, 13←pin 2 MISO, 10→pin 5 NSS.
5. **DIO0**: 14←pin 14. Leave DIO1 unwired.
6. **RESET**: 21→pin 6.
7. Power up. Flash Meshtastic. The boot log is the diagnosis, and
   `docs/faketec-pin-budget.md` already documents the SX126x equivalents:
   `CHIP_NOT_FOUND` means nothing is answering on SPI — check NSS, MISO and the rail;
   an SPI-level error *after* the chip has been identified proves SPI, RESET and the rail are
   good and moves the fault to the reference clock.
8. Only once the radio enumerates: GPS, then microphone, then microSD, then I²C — one at a time,
   re-running `/scanpu` after each so a new joint that lands on the wrong net is caught by the
   pin that stops floating rather than by a symptom three subsystems later.
9. Set `PPS_WIRED 1` **only** when `/pps` has actually reported edges. It means edges arrived,
   not that a pin was chosen.

---

## 7. Airtime, duty cycle and link budget at the operator site

Site: 3.5 acres = 14 164 m². A square parcel of that area has a 119.0 m side and a **168.3 m
diagonal**, so **~170 m** is the right maximum baseline — for a square. A long thin parcel is
longer and the number moves; check the plat.

### 7.1 Link budget — not the constraint

| quantity | value | how |
|---|---|---|
| λ at 915 MHz | 0.3276 m | c/f |
| FSPL at 170 m | **76.29 dB** | `20·log₁₀(170) + 20·log₁₀(915e6) − 147.55` — reproduces the briefed 76.3 dB |
| first Fresnel radius at midpoint | **3.73 m** | `0.5·√(λ·d)` = `0.5·√(0.3276 × 170)` |
| RFM95W sensitivity, SF7 BW500, 915 MHz | **−116 dBm** | datasheet Table 9, `RFS_L500_HF`, SF = 7 |
| RFM95W max TX | +20 dBm on PA_BOOST | datasheet, supply-current table (120 mA at that level) |

At +20 dBm with two 2 dBi antennas (⚠️**antenna gain is ASSUMED**, nothing here measured it):
received ≈ −52 dBm, which is **~64 dB above sensitivity**. The RF link at 170 m is not close to
marginal. What *is* tight is geometry: full first-Fresnel clearance wants 3.73 m of clear radius
around the line of sight at midpoint, and the common 60 % rule of thumb (⚠️rule of thumb, not
measured here) still wants 2.24 m. Trees and a roofline eat that, not distance.

### 7.2 Airtime — and the one number in the brief that is wrong

Meshtastic **ShortTurbo** is SF7 / BW 500 kHz / CR 4/5 — `src/mesh/MeshRadio.h:220-224`.

Airtime by the standard LoRa formula (`Tsym = 2^SF/BW`; preamble `(n+4.25)·Tsym`; payload
symbols `8 + ceil((8·PL − 4·SF + 28 + 16·CRC)/(4·(SF−2·DE)))·(CR+4)`):

| preamble symbols | 188 B on-air | 6 nodes × 118 det/h |
|---|---|---|
| 8 | 75.58 ms | 1.49 % |
| **16 — what Meshtastic actually uses** | **77.63 ms** | **1.53 %** |

⚠️**The briefed 75.6 ms / 1.49 % assumes an 8-symbol preamble. Meshtastic ships 16.**
`src/mesh/RadioInterface.h:106-107`:

```cpp
static constexpr uint16_t preambleLengthDefault =
    16; // 8 is default, but we use longer to increase the amount of sleep time when receiving
```

The corrected figures are **77.6 ms** and **1.53 %**. The brief's arithmetic is otherwise exact —
FSPL, Fresnel radius and the 170 m baseline all reproduce to three figures.

⚠️**188 B is also not where the frame lands.** `hear/wire.py`'s v2 frame is 173 B
(`HDR_V2` 13 + 20×8 sketch). Meshtastic's on-air header is 16 B
(`RadioInterface.h:21 MESHTASTIC_HEADER_LENGTH 16`) and the protobuf `Data` wrapper costs a few
more, so ~192–193 B is the realistic figure. At SF7/BW500 the payload-symbol ceiling steps in
2.56 ms increments, so this moves the answer very little:

| on-air bytes | airtime (preamble 16) | 6 × 118/h duty |
|---|---|---|
| 173 | 71.2 ms | 1.40 % |
| 188 | 77.6 ms | 1.53 % |
| 193 | 78.9 ms | 1.55 % |
| 205 (with PKC's +12 B) | 84.0 ms | 1.65 % |

⚠️**AND THIS COUNTS ORIGINATIONS ONLY.** A Meshtastic mesh rebroadcasts. Six nodes within
earshot of each other will each relay what they hear unless hop limit and roles are set to stop
it, and the channel utilisation that matters is the sum. **The 1.5 % figure is a floor, not a
prediction.** Measure `air_util_tx` on a live node before believing any number on this page.

### 7.3 Every preset, at 193 B on-air

Computed, preamble 16, low-data-rate optimise engaged where `Tsym > 16 ms`:

| preset | SF | BW kHz | CR | airtime | vs. the 400 ms dwell |
|---|---:|---:|---|---:|---|
| **ShortTurbo** | 7 | 500 | 4/5 | **78.9 ms** | fits |
| ShortFast | 7 | 250 | 4/5 | 157.8 ms | fits |
| MediumTurbo | 9 | 500 | 4/5 | 254.2 ms | fits |
| ShortSlow | 8 | 250 | 4/5 | 279.8 ms | fits |
| MediumFast | 9 | 250 | 4/5 | 508.4 ms | over |
| MediumSlow | 10 | 250 | 4/5 | 914.4 ms | over |
| LongTurbo | 11 | 500 | 4/8 | 1.30 s | over |
| LongFast *(default)* | 11 | 250 | 4/5 | 1.71 s | over |
| LongModerate | 11 | 125 | 4/8 | 6.10 s | over |
| LongSlow | 12 | 125 | 4/8 | 11.15 s | over |

Preset parameters from `src/mesh/MeshRadio.h:216-300`. ⚠️The "dwell" column applies the 400 ms
figure `docs/uplink.md` already used; whether a 500 kHz LoRa carrier is regulated as a hopping
system or as digital modulation under 15.247 is **not settled on this page** and should not be
decided from it.

**ShortTurbo is the right preset here and it is not a close call**: one sketch per event fits
comfortably, and the site's longest baseline is 170 m against a 64 dB margin.

---

## 8. What a Meshtastic module would have to provide

`docs/acoustic-stack.md` §1.1 is titled **"There is no radio."** It is still true:

> A node **ships nothing**. It is a single-client `WebServer` that gets polled. […]
> `hear/wire.py`'s v2 frame and `hear/node/telemetry.py`'s 14 B frame have **no firmware
> producer**.

The consumer is already built and tested: `hear/backend/pipeline.py` exposes

```python
BackendPipeline.ingest(frame: bytes, iface: str, rx_utc_s: float) -> Dict
```

and it is transport-agnostic **by construction** — it takes bytes plus an interface name and
imports nothing that knows what LoRa, Meshtastic or MQTT is. So the module does not need to
change any of it. It needs to become the **producer**, and that means seven things:

1. **Emit the 173 B v2 frame, byte-exact.** `hear/wire.py` `pack_v2`. Routing is by length —
   `pipeline.py:105`: 14 B is telemetry, anything else goes to `wire.decode`, and "the three
   frame sizes this repo emits — 14, 172, 173 — do not collide." A module that invents a fourth
   length breaks that.
2. **Carry a node id.** `ingest()` refuses v1 frames outright
   (`v1_frame_has_no_node_id`) and refuses a v2 frame whose `node_id` is not in the survey
   (`unknown_node`). The id is 2 B in the v2 header and the module must get it from the same
   place `survey.json` does.
3. **Carry microseconds-of-day, not microseconds-within-the-second.** The v2 header is 5 B of
   `us_of_day`; `wire.py`'s docstring is blunt that `us_of_day` is *not* v1's `node_us` and that
   confusing them is a one-second, 343 m error that nothing downstream can see. `rx_utc_s` picks
   the day and nothing else, so radio latency, retries and mesh hops cannot move a solution.
4. **Declare a profile id.** `profile_for()` raises rather than inventing one, and
   `PROFILE_GEOMETRY` is append-only: an id names bands, frames, NFFT, hop, rate, layout and band
   edges together. A module shipping a geometry with no id has nothing legal to put in the field.
5. **Use a private portnum**, so this traffic is distinguishable from mesh chat and so other
   Meshtastic clients ignore it.
6. **Not hold the CPU.** `docs/acoustic-stack.md` §1.3 measures the real constraint: the transport
   budget is denominated in *seconds of node deafness per hour*, not bytes. `/sd` and `/perf`
   never call `audio_pump()` and lose ~100 % of audio for the duration of a transfer. A radio
   task that blocks the audio loop pays the same way, and the node's own `drop_samples` counter
   **under-reports it by 25–92×**. Whatever the module does, it must yield to `audio_pump()`.
7. **Respect the duty budget in §7.2** — and measure `air_util_tx` rather than trusting it.

⚠️**What this does NOT unlock.** `hear/backend/` is the *geometry* path: decode → attribute →
associate → solve. `docs/acoustic-stack.md` says plainly that it "is not the path to central
classification and must not be wired as one." A working radio gives the array arrival times and
sketches. It does not give it audio, and §1.2's constraint — every byte the central stack can
retrospectively pull today is 16 kHz — is untouched by any of this.

---

## 9. What is still unverified on this page

| claim | state |
|---|---|
| every GPIO assignment | ⚠️**UNVERIFIED** — derived, never measured on a board. §1 is the fix. |
| strapping pins `{0, 3, 45, 46}` | ⚠️DATASHEET, unconfirmed; contradicted in part by the toolchain. §5. |
| RFM95 RESET on module pin 6 | Table 2 says 6, §7.2.2 of the same document says 7. Meter it. |
| this breakout's PSRAM type | ⚠️unknown; §2 is the test, and the map is built so it does not matter. |
| antenna gain in §7.1 | assumed 2 dBi, both ends. Nothing measured it. |
| the 400 ms dwell limit's applicability at BW 500 | not settled here. |
| microphone part, `FS_NOMINAL`, `MIC_BAND_*` | inherited from the XIAO, not this board's numbers. |
| `esp32s3-lora-pps` as a `NodeClass` | deliberately **not registered** until the capture path is measured against an external reference. `test_if_the_class_exists_its_capture_path_has_been_measured` enforces that a registration carries a real `path_bias_s`. |
