// Seeed XIAO ESP32-S3 Sense + u-blox GPS + BMP280 + microSD.
// Built as: nyquist, mach.  Class: xiao-s3-pps (hear/nodeclass.py).
#pragma once

#define BOARD_NAME     "xiao-s3-pps"

// ---- GPS ---------------------------------------------------------------------------------
// u-blox, UBX protocol. Baud is AUTO-DETECTED at boot and differs between the two built nodes --
// nyquist's module runs at 230400 and mach's at 115200 -- so it is not stated here. The solution
// rate is NOT a board fact either: the firmware pins it to 1 Hz with CFG-RATE, because the two
// modules shipped at 5 Hz and 10 Hz and that asymmetry cost mach two rejected UTC labellings a
// second until it was found.
#define GPS_PROTO      GPS_UBX
#define GPS_RX_PIN     44        // D7 <- module TX
#define GPS_TX_PIN     43        // D6 -> module RX
// ⚠️mach is wired with this pair REVERSED. gps_pick_pins() measures which pin carries a
// transmitter before opening the UART and follows the hardware, so both boards work -- but the
// standard is the order above and mach should be brought to it.

#define PPS_PIN        1         // D0. NOT D11: that is the PDM mic's clock, an output, and two
                                 // push-pull drivers on one net is not a configuration.

// ---- microphone --------------------------------------------------------------------------
#define MIC_KIND       MIC_PDM
#define MIC_CLK_PIN    42        // D11
#define MIC_DIN_PIN    41        // D12
#define FS_NOMINAL     16000     // measured PPS-disciplined: 16000.17 Hz on nyquist, 16000.26 on
                                 // mach -- 0.4 ppm apart, and both within 20 ppm of nominal.
#define MIC_BAND_LO_HZ 50        // the DC block sits at 1.6 Hz; this is the part's own low corner
#define MIC_BAND_HI_HZ 10000     // onboard PDM MEMS. Nyquist (8 kHz) binds first at this rate --
                                 // usable_band_hz() in nodeclass.py says which ceiling applies.

// ---- storage -----------------------------------------------------------------------------
#define SD_SCK_PIN     7         // D8
#define SD_MISO_PIN    8         // D9
#define SD_MOSI_PIN    9         // D10
#define SD_CS_PROBE_A  21        // the expansion board wires CS to GPIO21, NOT the D2 pad the
#define SD_CS_PROBE_B  3         // silkscreen implies. Probed in that order at boot.

// ---- I2C ---------------------------------------------------------------------------------
#define I2C_SDA_PIN    5         // D4
#define I2C_SCL_PIN    6         // D5
#define HAS_BARO       1         // 0x76/0x77. nyquist carries BMP280 silicon (chip id 0x58) on a
                                 // board LABELLED BME280; mach carries a real BME280 (0x60) and so
                                 // also reports humidity. Identified by chip id, never by label.
#define HAS_HUMIDITY   0         // per-node in practice -- see above. Reported from the chip id.
#define HAS_MAG        1         // nyquist IST8310 @0x0E, mach QMC5883L @0x0D. Detected, unread.
#define HAS_RTC        0
#define HAS_LIGHT      0
#define HAS_AIR        0

// ---- free pads ----------------------------------------------------------------------------
// D1/GPIO2, D2/GPIO3, D3/GPIO4 are the only genuinely free pads. Everything else on the header is
// driven by this build; /pins derives that table from these defines so it cannot go stale.
#define FREE_PADS      {2, 3, 4}
