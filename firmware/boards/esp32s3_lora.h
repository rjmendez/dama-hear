// Full-size ESP32-S3 breakout + RFM95W (SX1276) LoRa for Meshtastic. NOT BUILT YET.
// Class: esp32s3-lora-pps -- deliberately NOT registered in hear/nodeclass.py, see the bottom
// of this file. Build document: docs/esp32s3-lora-node.md.
//
// ⚠️NOTHING HERE IS MEASURED ON A BOARD. Every pin below is DERIVED -- from the installed
// ESP-IDF headers, from a datasheet, or from another board's convention -- and derived is the
// state in which a pin map has twice been wrong in this repo (the L86 1PPS documented on module
// pin 11 when Hardware Design Table 3 says 6; the PUC's PPS pad named GPIO18 when 18 is
// externally driven and GPIO17 is the pad that floats). Both reached a soldering iron.
// Step 1 of docs/esp32s3-lora-node.md is /scanpu + /scanpd on the BARE breakout. Do not solder
// until every pin below has a measured row, and then mark it MEASURED here.
#pragma once

#define BOARD_NAME     "esp32s3-lora-pps"

// ---- what the silicon forbids, and where that is written -------------------------------------
// Paths relative to ~/.arduino15/packages/esp32/tools/esp32s3-libs/3.3.11/include/soc/esp32s3/
//   include/soc/soc_caps.h:187   SOC_GPIO_VALID_GPIO_MASK clears BIT22..BIT25.
//                                GPIO 22, 23, 24, 25 DO NOT EXIST on this part.
//   include/soc/soc_caps.h:189   SOC_GPIO_VALID_OUTPUT_GPIO_MASK == SOC_GPIO_VALID_GPIO_MASK.
//                                ⚠️the S3 has NO input-only GPIOs. Any table that says otherwise
//                                is an ESP32-classic table being read for the wrong part.
//   include/soc/spi_pins.h:11-22 MSPI: 26 CS1, 27 HD, 28 WP, 29 CS0, 30 CLK, 31 MISO, 32 MOSI --
//                                the flash bus, NEVER usable. 33 D4, 34 D5, 35 D6, 36 D7,
//                                37 DQS -- used ONLY when the part is octal. See PSRAM below.
//   include/soc/usb_pins.h:26-27 USBPHY_DP_NUM 20, USBPHY_DM_NUM 19 -- the native USB pads.
//   include/soc/uart_pins.h:23-24 U0RXD 44, U0TXD 43 -- the ROM/console UART's IO_MUX pads.
//
// ⚠️usb_pins.h ALSO defines USBPHY_VP_NUM 42 and USBPHY_VM_NUM 41, AND THOSE ARE NOT PADS TO
// AVOID. Its own header comment says the external-FSLS-PHY macros are deprecated and
// "meaningless" because those signals route to any GPIO through the matrix. 41 and 42 carry the
// microphone on every XIAO node in the fleet today. 19 and 20 are the real constraint.

// ---- PSRAM: this is the question that moves pins, and it is OPEN ------------------------------
// Octal PSRAM consumes GPIO 33..37. Quad PSRAM and no-PSRAM leave them free. The Arduino core
// treats this as a per-board fact, not a per-chip one:
//   boards.txt:1162,1204   esp32s3.name=ESP32S3 Dev Module          build.psram_type=qspi
//   boards.txt:2372,2414   esp32s3-octal.name=... Octal (WROOM2)    build.psram_type=opi
//   boards.txt:38220,38266 XIAO_ESP32S3                             build.psram_type=qspi
// -- and the fleet's XIAO nodes are nonetheless built OCTAL, because firmware/night_node/
// flash.py:34 overrides that default with `esp32:esp32:XIAO_ESP32S3:PSRAM=opi`
// (firmware/README.md:53: "8.0 MB -- only with PSRAM=opi; the default board option disables it").
// So a board entry's default does NOT tell you what a given module is; the FQBN it boots on does.
//
// ⚠️THE MAP BELOW USES NONE OF 33..37, SO THE QUESTION CANNOT BITE. That is deliberate and it
// costs nothing: after every assignment here, ten pads are still free. Resolve the question
// empirically (docs/esp32s3-lora-node.md step 2) before treating 33..37 as spare.

// ---- strapping pins -- DATASHEET, AND I COULD NOT VERIFY IT FROM THE TOOLCHAIN ----------------
// ⚠️READ THIS BEFORE USING 0, 3, 45 OR 46. The installed ESP-IDF headers do NOT enumerate the
// strapping pins. What they do contain is fragmentary and, in one place, self-contradictory:
//   esp_rom/esp32s3/include/esp32s3/rom/efuse.h:210-211  ROM UART print gated on the level of
//                                                        GPIO46 at digital reset.
//   soc/esp32s3/register/soc/efuse_struct.h:442-444      the SAME control, uart_print_control,
//                                                        described as "GPIO8 is low at reset".
//   soc/esp32s3/register/soc/efuse_reg.h:502-504         JTAG source selected by "strapping
//                                                        gpio10", gated on an eFuse.
// The rom/ header is the target-specific one and efuse_struct.h's GPIO8 line reads like text
// carried over from another target, but I did not establish that and I am not asserting it.
// GPIO0, GPIO3 and GPIO45 appear NOWHERE in the toolchain as straps. The set {0, 3, 45, 46} is a
// DATASHEET claim -- ESP32-S3 datasheet, chapter 2 "Pin Definitions", the section titled
// "Strapping Pins" -- that the operator must confirm against that document before relying on it.
// Inside this repo it is corroborated only by firmware/puc_node/puc_node.ino:222 ("GPIO45 and 46
// are strapping pins"), which is the same class of claim and not an independent check.
// UNVERIFIED. None of the four is used below, so nothing here depends on the answer.

// ---- GPS ---------------------------------------------------------------------------------
// CONVENTION, not measurement: both existing boards put the GPS on 44/43, and both are right to
// -- those are U0RXD/U0TXD's IO_MUX pads (uart_pins.h:23-24), so the UART reaches them without
// the GPIO matrix. The cost is the UART0 console, which this build does not use: the console is
// USB-CDC on 19/20.
// ⚠️AND THAT COST IS A TRAP ON A BREAKOUT. A full-size dev board carrying a CH340/CP2102
// USB-serial bridge has that bridge wired to 43/44 already. If the scan shows either pin driven,
// this pair is NOT free on your board and the GPS moves. That is a per-board fact, and it is one
// of the reasons the scan comes first.
#define GPS_PROTO      GPS_UBX   // assumed u-blox, as on the XIAO nodes. A PMTK module is
                                 // GPS_PMTK and shares NO configuration commands -- see puc.h.
#define GPS_RX_PIN     44        // UNVERIFIED. node RX <- module TX. U0RXD IO_MUX pad.
#define GPS_TX_PIN     43        // UNVERIFIED. node TX -> module RX. U0TXD IO_MUX pad.

#define PPS_PIN        4         // UNVERIFIED. A plain digital pad: not 22-25 (absent), not
                                 // 26-32 (flash), not 33-37 (octal PSRAM), not 19/20 (USB), not
                                 // 43/44 (this board's GPS UART), not {0,3,45,46} (strapping).
                                 // ⚠️"not forbidden" is exactly the argument that put the PUC's
                                 // PPS on GPIO18 for two months. It is necessary and it is not
                                 // sufficient. /scanpu must show this pin FLOATING -- HIGH
                                 // against the internal pullup, low against the pulldown --
                                 // before a push-pull 1PPS output is soldered to it.
#define PPS_WIRED      0         // flip to 1 only when /pps has actually reported edges

// ---- LoRa: RFM95W, an SX1276 -----------------------------------------------------------------
// WHICH LINES THE DRIVER ACTUALLY NEEDS, from Meshtastic's own source (github.com/meshtastic/
// firmware, read 2026-09-10):
//   src/mesh/RadioInterface.cpp:466  new RF95Interface(loraHal, LORA_CS, RF95_IRQ, RF95_RESET,
//                                                     RF95_DIO1)
//   src/RF95Configuration.h          RF95_IRQ defaults to LORA_DIO0; RF95_DIO1 to LORA_DIO1,
//                                    commented "not really used for RF95"
//   src/mesh/RF95Interface.h:51      setRadioIsr() => lora->setDio0Action(callback, RISING)
// setDio0Action is the ONLY interrupt that path arms. DIO1 is passed into RadioLib's Module and
// never used. So SPI + DIO0 + RESET are REQUIRED; DIO1 is optional and left unwired here.
// DIO2..DIO5 are not referenced at all. RXEN/TXEN exist only for modules with an external RF
// switch (RF95_RXEN/RF95_TXEN are #ifdef'd); a bare RFM95W has none.
//
// ⚠️MODULE PIN NUMBERS, AND THE DATASHEET DISAGREES WITH ITSELF ABOUT RESET.
// RFM95W/96W/98W datasheet Version 2.0, section 1.4, Table 2 "Pin Description", page 11/123:
//     1 GND   2 MISO  3 MOSI  4 SCK   5 NSS   6 RESET  7 DIO5  8 GND
//     9 ANT  10 GND  11 DIO3 12 DIO4 13 3.3V 14 DIO0  15 DIO1 16 DIO2
// DIO0 is pin 14, not 7 or 8, and 11/12 are DIO3/DIO4 -- out of numerical order. This is the
// same shape as the L86 1PPS error: a plausible pin number that is the wrong one.
// AND: section 7.2.2 "Manual Reset" of that same document says "Pin 7 should be pulled low for a
// hundred microseconds", contradicting its own Table 2. The PDF's metadata names it
// SX1272DS_V0'8.book, so 7.2.2 reads as inherited chip-level text while Table 2 is the
// module-level table -- but that is an INFERENCE, not something I established. Table 2 is the one
// to wire to, and RESET must be confirmed by continuity on the actual module before power.
// RESET is active LOW (7.2.2: pulled low, then released) and the chip needs 5 ms afterwards.
#define LORA_PART      "RFM95W"  // SX1276 core, 868/915 MHz variant (datasheet Table 1).
#define LORA_SPI_HOST  2         // SPI2. The four pins below are SPI2's IO_MUX fast path, so the
                                 // bus does not go through the GPIO matrix:
                                 // spi_pins.h:29-34 CS 10, MOSI 11, CLK 12, MISO 13.
                                 // Independently corroborated: Meshtastic's tbeam-s3-core
                                 // variant.h -- a shipping ESP32-S3 + SX127x board -- uses
                                 // LORA_CS 10, LORA_MOSI 11, LORA_SCK 12, LORA_MISO 13.
#define LORA_CS_PIN    10        // UNVERIFIED. -> RFM95 pin 5  NSS
#define LORA_MOSI_PIN  11        // UNVERIFIED. -> RFM95 pin 3  MOSI
#define LORA_SCK_PIN   12        // UNVERIFIED. -> RFM95 pin 4  SCK
#define LORA_MISO_PIN  13        // UNVERIFIED. <- RFM95 pin 2  MISO
#define LORA_DIO0_PIN  14        // UNVERIFIED. <- RFM95 pin 14 DIO0. REQUIRED: the only ISR the
                                 // RF95 path arms. 14 is SPI2's IO_MUX WP pad, which binds only
                                 // when SPI2 is configured quad/octal; a 4-wire master leaves it
                                 // an ordinary GPIO. spi_pins.h:24-27 states the IO_MUX sets are
                                 // a routing choice, not a reservation.
#define LORA_RST_PIN   21        // UNVERIFIED. -> RFM95 pin 6  RESET, active low.
#define LORA_DIO1_PIN  -1        // NOT WIRED, and that is a decision rather than an omission: the
                                 // RF95 path never arms it (RF95Interface.h:51). Wire it only if
                                 // a later driver moves to pure-SX127x FSK timeouts.
#define LORA_RXEN_PIN  -1        // no external RF switch on a bare RFM95W
#define LORA_TXEN_PIN  -1

// ---- microphone --------------------------------------------------------------------------
// CONVENTION from xiao_s3_sense.h, which is the only mic pair in this repo that has produced a
// corpus. ⚠️NO PART IS CHOSEN YET, so FS_NOMINAL and the band are the XIAO's numbers carried
// across and are NOT this board's measurements. docs/node-hardware.md argues for an I2S part
// over PDM; if that is what gets fitted this becomes MIC_I2S and needs a third pin (WS).
#define MIC_KIND       MIC_PDM
#define MIC_COUNT      1
#define MIC_CLK_PIN    42        // UNVERIFIED. See the usb_pins.h note above: 42 is not a USB pad.
#define MIC_DIN_PIN    41        // UNVERIFIED.
#define FS_NOMINAL     16000     // INHERITED from the XIAO, not measured here. 48 kHz is the
                                 // fleet norm elsewhere; this number sets Nyquist and no filter
                                 // recovers past it, so settle it when the part is chosen.
#define MIC_BAND_LO_HZ 50
#define MIC_BAND_HI_HZ 10000     // Nyquist binds at 16 kHz; usable_band_hz() says which ceiling.

// ---- storage: a SECOND SPI bus, deliberately ------------------------------------------------
// ⚠️THE SD CARD MUST NOT SHARE THE LoRa BUS. It is electrically legal -- two chip selects on one
// bus -- and it is a bad trade here: a DIO0 edge arrives during an SD block write and the
// radio's service and the card's transaction contend for one peripheral at interrupt time. SPI3
// has no IO_MUX pads on this part (spi_pins.h:46 "SPI3 have no iomux pins"), so these go through
// the GPIO matrix, which costs a little skew and nothing that matters at SD clock rates.
// The pin NUMBERS are xiao_s3_sense.h's, kept so the two boards' wiring diagrams read alike.
#define SD_BUS         SD_SPI
#define SD_SPI_HOST    3         // SPI3
#define SD_SCK_PIN     7         // UNVERIFIED.
#define SD_MISO_PIN    8         // UNVERIFIED.
#define SD_MOSI_PIN    9         // UNVERIFIED. 9 is SPI2's IO_MUX HD pad and is an ordinary GPIO
                                 // here for the same reason 14 is: SPI2 is 4-wire.
#define SD_CS_PIN      5         // UNVERIFIED. A dedicated CS, not the XIAO's probe-two-pads
                                 // arrangement -- that exists because the XIAO expansion board
                                 // wires CS somewhere its silkscreen does not admit.

// ---- I2C ---------------------------------------------------------------------------------
// CONVENTION from puc.h, where 47/48 is MEASURED (six devices answered /i2c). Full-size S3
// designs land here often enough that it is the right default to try first.
// ⚠️/scan CANNOT FIND AN I2C BUS. An idle bus does not toggle. What the scan gives you is the
// set of pins that stay HIGH against the internal pulldown -- external pullups -- and /i2c then
// sweeps that set for an address that answers. puc.h documents the whole narrowing.
#define I2C_SDA_PIN    47        // UNVERIFIED.
#define I2C_SCL_PIN    48        // UNVERIFIED.
#define HAS_BARO       1         // intended: BMP280/BME280. `c = 331.3 + 0.606*T` -- one degree
#define HAS_HUMIDITY   0         // is 183 us over 35 m, the whole one-way arrival budget.
#define HAS_MAG        0         // Report from the chip id, never from the board label: nyquist
#define HAS_RTC        0         // carries BMP280 silicon on a board printed BME280.
#define HAS_LIGHT      0
#define HAS_AIR        0

// ---- free pads ----------------------------------------------------------------------------
// What is left after the above, on a module of ANY PSRAM type. Not "measured free" -- "not
// claimed by this build". The scan decides whether they are actually free on your breakout.
#define FREE_PADS      {1, 2, 6, 15, 16, 17, 18, 38, 39, 40}
// ⚠️DO NOT ADD 33..37 TO THAT LIST WITHOUT RESOLVING THE PSRAM QUESTION. On an octal module they
// are the PSRAM data bus and touching them takes the board down.
// ⚠️DO NOT COPY puc.h's FREE_PADS EITHER. It lists 18 and 39, and tests/test_puc_pps_pin.py
// records the measurement that both are externally DRIVEN on that board. Free pads are a
// per-board result, never a per-chip one.

// ---- the class is deliberately NOT registered ------------------------------------------------
// hear/nodeclass.py has no "esp32s3-lora-pps" and must not get one until this board exists and
// its capture path has been measured against an external reference. A NodeClass with
// path_bias_s=None is refused as a TDoA arrival source, which is the correct state for hardware
// nobody has timed -- and inventing a number by copying the XIAO's 1/16000 across a change of
// microphone, sample rate and SPI load is the transplant that makes a constant wrong while it
// still looks right. xiao-s3-i2s in that file is the same situation, stated the same way.
