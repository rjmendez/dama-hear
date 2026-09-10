// BirdWeather PUC, running dama-hear firmware in place of the vendor's.
// Class: puc-ntp until 1PPS is wired, then puc-pps (hear/nodeclass.py).
//
// Everything here was measured off the hardware -- a flash dump, a boot log and a whole-bank pin
// scan -- because there is no schematic and no published firmware source. What is still unknown is
// marked as such rather than guessed, since a wrong pin here is a soldering error.
// ⚠️NOTHING INCLUDES THIS FILE. Verified by grep across firmware/: puc_node.ino carries its own
// pin defines and does not include this header, so every value below is DOCUMENTATION, not
// configuration -- editing it changes no built firmware. A fix landed here in a6bbdab and had no
// effect on any node for exactly this reason. Change puc_node.ino, or make it include this.
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
// ⚠️⚠️DO NOT SOLDER TO GPIO18. This said GPIO18 was where the wire should land, chosen because it
// reads low and is not a strapping pin. That was wrong and would have been found with an iron in
// hand: the VENDOR FIRMWARE CONFIGURES GPIO18 AS AN INPUT (gpio_config, pin_bit_mask 0x40000), and
// on the live board it reads LOW against an internal pullup -- so something external already
// drives that net. A PPS wire there is a second driver on someone else's signal.
//
// The only SAFE pins no firmware API touches at all are 0, 2, 3, 15, 16, 17 and 46; of those 0, 3,
// 45 and 46 are ESP32-S3 strapping pins and are out. ⚠️LAND A PPS WIRE ON 2, 15, 16 OR 17.
// PPS_PIN below stays -1-in-spirit until a joint exists; a node with no PPS must be refused as a
// TDoA arrival source rather than quietly trusted.
#define PPS_PIN        -1        // was 18 -- GPIO18 IS ALREADY IN USE, see above. Pick 2/15/16/17.
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

// ---- what the VENDOR FIRMWARE says is on this board ------------------------------------------
// From its self-test CSV header, in ~/puc-backup (2x 4 MiB at 0x0 and 0x400000, mrpink). This is
// the vendor's own inventory, so it settles the parts whose I2C address alone could not:
//
//   MAC_ADDR, FW_VER, BUTTON, WIFI, DS3231, BME688, LIS2DH12, LIS3MDL, AS7341,
//   RGB_LED, SD_DET, USB_DET, USB_VOLTS, BATT_VOLTS, GPS, MIC_LEFT, MIC_RIGHT, BUZZER
//
// ⚠️TWO CORRECTIONS to what the ID registers alone suggested. 0x76 is a BME688, not a BME680 --
// they share chip id 0x61. 0x18 is a LIS2DH12, not a LIS3DH -- they share WHO_AM_I 0x33. The
// AS7341 guess at 0x39 (id 0x24) is confirmed. An ID register narrows a part; it does not always
// name one.
//
// ---- pins the vendor firmware CONFIGURES, recovered from its gpio_config() call sites ----------
// Method: locate gpio_config() by its unique assert "GPIO_PIN mask error", find every caller, then
// decode the gpio_config_t each one builds -- pin_bit_mask at +0/+4 (64-bit), mode +8, pull_up +12,
// pull_down +16, intr_type +20. The ELECTRICAL CONFIG below is hard evidence read out of those
// stores. The NAMES are inference and are marked as such: the log strings in that function are
// function-level, so they say which routine configures a pin, never which pin is which.
//
//   GPIO  5   INPUT + PULLDOWN     a detect that reads HIGH when its thing is present
//   GPIO  8   INPUT + PULLUP       a detect/switch that reads LOW when active
//   GPIO 18   INPUT
//   GPIO 38   INPUT                <- the DS3231 SQW input. Independently found on hardware first.
//   GPIO 39   INPUT                configured beside 40, in the "Vesper Detected" routine
//   GPIO 40   OUTPUT, PULSED       set high, short delay, set low -- a strobe
//   GPIO 21   OUTPUT, driven high
//   GPIO 41   OUTPUT, driven high
//   GPIO 1,4,5,6  OUTPUT           one further site, mask 0x72
//
// ⚠️THE CROSS-CHECK THAT MAKES THIS WORTH TRUSTING. /scanpu on the live board found exactly three
// pins held LOW against an internal pullup -- 8, 18 and 39 -- meaning something external drives
// them. All three are INPUTS here. Two independent methods, same three pins.
//
// GPIO 40 pulsed with 39 read beside it, inside the routine that logs "Vesper Detected", is a
// strobe-and-sample mic-presence probe. INFERRED, not proven. Likewise GPIO 8 (INPUT+PULLUP
// reading LOW on the live board) is the shape of a card-detect with a card inserted, which is
// testable in one move: eject the card and re-read /scanpu.
//
// STILL UNMAPPED: which of these is BUTTON vs SD_DET vs USB_DET, the BUZZER (GPIO1 is the LEDC
// channel's gpio_num, from ledc_channel_config_t +0 at DRAM 0x3fcae0e8), and the two ADC inputs.
// ⚠️THE RGB LED PIN IS NOT IN THE FIRMWARE AT ALL: rmt_new_tx_channel's gpio_num comes from a
// FUNCTION RETURN (mov.n a2, a10 at 0x4201be4c), i.e. a runtime config lookup. The NVS partition
// was parsed and holds no pin config -- only restart_counter, last_lat/lon, puc_mode,
// station_mode and the wifi stack's own keys -- so that pin lives in PUC_Config.json on the card.
// GPIO45 remains the one pulled-up pin no API touches; likely a passive strap.
//
// ⚠️THE DUMP CONTAINS THE WIFI PSK IN PLAINTEXT (nvs.net80211/sta.pswd). Treat ~/puc-backup as a
// secret.

// ---- storage -----------------------------------------------------------------------------
// A microSD slot exists (the vendor writes /sdcard/YYYYMMDD/*.flac). The card is on the NATIVE
// SDMMC peripheral, not SPI. I had declared SCK/MISO/MOSI/CS here, which is the wrong bus entirely
// and would have sent whoever wires this to the wrong pads. The roles are CLK, CMD and D0..D3.
//
// ⚠️PINS RECOVERED 2026-09-08 from the vendor dump, statically -- no probing, nothing driven.
// The app memcpy's SDMMC_SLOT_CONFIG_DEFAULT() (the ORIGINAL-ESP32 defaults: clk 14, cmd 15, d0 2,
// d1 4, d2 12, d3 13 -- a red herring, and the reason a naive read of the rodata is wrong) into a
// stack struct at a1+16, then overrides every field for the S3 immediately after. Those overrides
// are what is below, read off the store offsets at 0x4200ce4f-0x4200ce79:
//     s32i a1,16 -> clk 12   s32i a1,20 -> cmd 13   s32i a1,24 -> d0 14
//     s32i a1,28 -> d1  9    s32i a1,32 -> d2  11   s32i a1,36 -> d3 10   s8i a1,64 -> width 4
// All six sit in the pulled-up set /scan vs /scanpd left over after the mic and I2C were assigned,
// which is the independent check on this.
#define SD_BUS         SD_SDMMC

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

#define SD_CLK_PIN     12        // from the vendor dump: override after SLOT_CONFIG_DEFAULT
#define SD_CMD_PIN     13
#define SD_D0_PIN      14
#define SD_D1_PIN      9
#define SD_D2_PIN      11
#define SD_D3_PIN      10
#define SD_BUS_WIDTH   4

// ---- GPS: the command set the VENDOR uses, lifted from the dump -------------------------------
// Useful when the L86 is diagnosed: these are the exact strings the stock firmware sends, so a
// module that answers these and not ours is telling us something about our port, not the module.
//   $PMTK605     query firmware version   (answered PMTK705 ... Quectel-L86)
//   $PMTK104     full cold start          $PMTK161,0  standby
//   $PMTK220,1000  fix interval 1 Hz      $PMTK225,0  continuous   $PMTK225,8  AlwaysLocate
//   $PMTK286,1   active interference cancellation on
//   $PMTK306,15  /  $PMTK311,10           $PMTK353,1,1,1,0,0  constellation search mode
// ⚠️There is NO PMTK285 anywhere in the vendor image -- it never enabled 1PPS, consistent with the
// pin never having been routed.

// Electrical config only -- names still inferred, see the block above.
#define SQW_IN_PIN     38        // DS3231 SQW, INPUT (firmware) + measured on hardware
#define VESPER_STROBE  40        // OUTPUT, pulsed, in the "Vesper Detected" routine
#define VESPER_SENSE   39        // INPUT, configured beside it
#define DETECT_PU_PIN  8         // INPUT+PULLUP, reads LOW live -- card-detect shaped
#define DETECT_PD_PIN  5         // INPUT+PULLDOWN
#define BUZZER_PWM_PIN 1         // ledc_channel_config_t.gpio_num @0x3fcae0e8
