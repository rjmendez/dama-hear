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

// ---- microphone --------------------------------------------------------------------------
// Two MEMS mics. The vendor firmware records 48 kHz and SUMS them to mono ("mono_sum_left_right"),
// throwing away the inter-mic delay -- but it computes LeftSPL/RightSPL/LeftPSD/RightPSD
// separately, so the stereo path exists in hardware. Two mics on a fixed baseline is a bearing,
// which neither XIAO node can produce at all.
// ⚠️PINS NOT YET KNOWN. The pin scan runs with the peripherals unpowered, so the I2S lines were
// static. /scan and /scanpd on the beachhead firmware are how they get found.
#define MIC_KIND       MIC_I2S
#define MIC_COUNT      2
#define MIC_BCLK_PIN   -1        // unknown
#define MIC_WS_PIN     -1        // unknown
#define MIC_DIN_PIN    -1        // unknown
#define FS_NOMINAL     48000     // vendor config: "sampleRateHz": 48000
#define MIC_BAND_LO_HZ 50
#define MIC_BAND_HI_HZ 20000     // unverified part; at 48 kHz Nyquist is 24 kHz so the mic binds

// ---- storage -----------------------------------------------------------------------------
// A microSD slot exists (the vendor writes /sdcard/YYYYMMDD/*.flac). Pins unknown. There is also
// 25.94 MB of onboard FAT in the custom partition table, which needs no pins at all.
#define SD_SCK_PIN     -1
#define SD_MISO_PIN    -1
#define SD_MOSI_PIN    -1
#define SD_CS_PROBE_A  -1
#define SD_CS_PROBE_B  -1

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
