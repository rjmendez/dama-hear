// BirdWeather PUC, running dama-hear firmware in place of the vendor's.
// Class: puc-ntp until 1PPS is wired, then puc-pps (hear/nodeclass.py).
//
// Everything here was measured off the hardware -- a flash dump, a boot log and a whole-bank pin
// scan -- because there is no schematic and no published firmware source. What is still unknown is
// marked as such rather than guessed, since a wrong pin here is a soldering error.
#pragma once

#define BOARD_NAME     "puc-ntp"

// ---- GPS ---------------------------------------------------------------------------------
// Quectel L86, MT3333 core, firmware AXN5.1.9. MediaTek PMTK, NOT u-blox: none of the UBX config
// applies -- no CFG-VALSET, no NAV-PVT, no TIM-TP. Confirmed by round trip: $PMTK605 was answered
// with $PMTK705,MT3333_AXN5.1.9_MODULE_STD_F1_P1,0002,Quectel-L86,1.0.
#define GPS_PROTO      GPS_PMTK
#define GPS_RX_PIN     44        // measured: 9600-baud traffic idling high, ~1 s gaps
#define GPS_TX_PIN     43        // measured: the module replies to commands sent here
#define GPS_BAUD_FIXED 9600
// Vendor GPS power modes, lifted verbatim from the dump's command strings -- useful because they
// are the states this module can be parked in: $PMTK225,0 continuous, $PMTK225,8 AlwaysLocate,
// $PMTK161,0 standby, $PMTK104 full cold start. The vendor also calls the part "L80" in one log
// string while the module answers Quectel-L86; the round-trip answer is the one to trust.

// ⚠️1PPS IS NOT ROUTED ON STOCK HARDWARE. The L86 exposes it on pin 11; it was forced on with
// $PMTK285,4,100 (always, regardless of fix) and NO GPIO saw a 1 Hz edge across repeated
// whole-bank scans. The vendor firmware has no PPS string, no PMTK285 and no interrupt configured
// on any pin, which is consistent: there was never a trace to write code for.
//
// GPIO18 is where the wire should land -- held low, not a strapping pin, clear of the flash
// (26-32), PSRAM (33-37) and USB (19-20) ranges. Set to -1 until the joint exists; a node with
// PPS_PIN -1 must be refused as a TDoA arrival source rather than quietly trusted.
#define PPS_PIN        18
#define PPS_WIRED      0         // flip to 1 only when /pps has actually reported edges

// ---- the tick that DOES exist ---------------------------------------------------------------
// ⚠️FOUND 2026-09-08: the DS3231's SQW/INT pin is on GPIO38 and it emits 1 Hz. The GPS route to a
// hardware tick is shut -- the L86 is not answering and its 1PPS was never routed -- but the RTC
// has its own and nobody had looked, because the part ships INTCN=1 (interrupt mode, no alarms) so
// that pin sits idle from the factory.
//
// HOW IT WAS ESTABLISHED, and why the first attempt said "nothing":
//   * SQW is OPEN-DRAIN. A floating /scan cannot see it -- the same trap the GPS-TX probe above
//     documents. Against an internal pullup it appears immediately: 8 edges per 4 s, 50% duty.
//   * Causally, not by correlation: SQW off -> gpio38 static and held high (twice); SQW on ->
//     1 Hz returns. That on/off control is what makes this an identification.
// ⚠️IT IGNORES THE RATE BITS. RS2/RS1 set for 1024, 4096 and 8192 Hz all still give 1 Hz, with the
// control register reading back exactly what was written each time. That is the documented
// behaviour of the DS3231M -- the MEMS variant, whose SQW is 1 Hz only -- or of a clone that
// hardwires it. NOT CONFIRMED; the part answers the DS3231's temperature and status registers.
//
// ⚠️A LOCAL TICK IS NOT A PPS, and this pin must never be treated as one. The edge is STABLE, not
// CORRECT: it says a second elapsed, never which second, and it is disciplined by a crystal rather
// than by GPS. What it is good for is subdividing a second that NTP named -- the same split the ESP
// audioboards use, where the board owns the rate and a host names the second.
#define RTC_SQW_PIN    38        // measured; open-drain, needs a pullup to be seen at all
#define RTC_SQW_HZ     1         // fixed at 1 regardless of RS2/RS1 on this part

// ---- microphone --------------------------------------------------------------------------
// Two MEMS mics. The vendor firmware records 48 kHz and SUMS them to mono ("mono_sum_left_right"),
// throwing away the inter-mic delay -- but it computes LeftSPL/RightSPL/LeftPSD/RightPSD
// separately, so the stereo path exists in hardware. Two mics on a fixed baseline is a bearing,
// which neither XIAO node can produce at all.
// ⚠️FOUND 2026-09-08 on the live board: CLK=6, DIN=7, and the two mics are stereo on that one
// DIN as the dump implied. Method and evidence, because "a pin toggles" is not "a microphone":
//   * /pdmscan drives ONE candidate clock and watches every other pin. clk=6 woke gpio 7 at ~5900
//     transitions per 12k samples, repeatably. clk=7 woke NOTHING -- and capacitive coupling
//     between adjacent pads is symmetric, so that asymmetry is what rules out crosstalk.
//   * /mic then ran real I2S PDM RX on the pair: left rms 46.1 / right rms 36.1 at 16 kHz, both
//     channels non-zero, nothing pinned, and the two channels differ. The negative control at
//     clk=9 din=10 returned a DC constant, mean -30935 rms 0.0 -- which is what no-mic looks like.
// ⚠️The claim above that the pin scan saw static I2S lines because "the peripherals are unpowered"
// is FALSE: six I2C devices answer on 47/48, so the sensor rail is live. The lines were static
// because nothing was clocking them.
// The vendor app links i2s_pdm_rx_set_gpio -- this is a PDM microphone path, the same kind as the
// XIAO, NOT the generic I2S I first wrote here. From the flash dump, not from the product page.
#define MIC_KIND       MIC_PDM
#define MIC_COUNT      2
#define MIC_CLK_PIN    6         // measured: clocking this makes DIN drive; the reverse does not
#define MIC_DIN_PIN    7         // measured: real PDM audio, L and R differ, control is a constant
#define FS_NOMINAL     48000     // vendor config: "sampleRateHz": 48000
#define MIC_BAND_LO_HZ 50
#define MIC_BAND_HI_HZ 20000     // unverified part; at 48 kHz Nyquist is 24 kHz so the mic binds

// ---- storage -----------------------------------------------------------------------------
// A microSD slot exists (the vendor writes /sdcard/YYYYMMDD/*.flac). The dump links sdmmc_host_*,
// diskio_sdmmc and logs "Using SDMMC peripheral" -- so the card is on the NATIVE SDMMC peripheral,
// not SPI. I had declared SCK/MISO/MOSI/CS here, which is the wrong bus entirely and would have
// sent whoever wires this to the wrong pads. The roles are CLK, CMD and D0..D3.
#define SD_BUS         SD_SDMMC
#define SD_CLK_PIN     -1
#define SD_CMD_PIN     -1
#define SD_D0_PIN      -1        // bus width not yet established (1-bit or 4-bit)

// ---- I2C ---------------------------------------------------------------------------------
// DS3231 RTC plus temperature, humidity, pressure, VOC, eCO2, IAQ, a 3-axis magnetometer, a 3-axis
// accelerometer and an 11-channel spectral light sensor -- read off the vendor's own CSV header.
// ⚠️Bus pins NOT yet known.
#define I2C_SDA_PIN    -1
#define I2C_SCL_PIN    -1
#define HAS_BARO       1
#define HAS_HUMIDITY   1
#define HAS_MAG        1
#define HAS_RTC        1         // DS3231. Holdover, not sync -- it does not make this a timing node.
#define HAS_LIGHT      1
#define HAS_AIR        1         // VOC / eCO2 / IAQ

// ---- reserved ------------------------------------------------------------------------------
// 19,20 USB D-/D+ -- reconfiguring these killed the console once already and cost a replug caught
// inside a four-second window. 26-32 SPI flash. 33-37 octal PSRAM. Never touch any of them.
#define FREE_PADS      {15, 16, 17, 18, 21, 38, 39}

// ---- I2C ------------------------------------------------------------------------------------
// Found 2026-09-08 by /i2c. /scan alone never could: an idle bus does not toggle. The narrowing is
// that an external pullup beats the ESP32's ~45k internal pulldown, which left eleven candidates;
// the other nine are the SD card's CMD and D0-D3, which carry pullups for the same reason.
#define I2C_SDA_PIN    47
#define I2C_SCL_PIN    48
// Confirmed by ID register; the rest ACK an address, which proves something answered, not what.
#define I2C_ADDR_LIS3DH   0x18   // CONFIRMED WHO_AM_I 0x33
#define I2C_ADDR_LIS3MDL  0x1C   // CONFIRMED WHO_AM_I 0x3D
#define I2C_ADDR_AS7341   0x39   // ID reg 0x92 = 0x24; NOT second-sourced, verify before relying
#define I2C_ADDR_EEPROM   0x50   // AT24C-series, reads 0xFF throughout (blank)
#define I2C_ADDR_DS3231   0x68   // CONFIRMED by temp + status decode; OSF=0, has never lost time
#define I2C_ADDR_BME680   0x76   // CONFIRMED chip id 0x61
