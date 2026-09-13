// Minimal ESP32-S3 breakout: one external I2S microphone, PMTK GPS and 1PPS.
// USB is the only host connection; no SD card, I2C sensor, camera or LoRa radio is required.
#pragma once

#define BOARD_NAME     "esp32s3-i2s-gps"

// GPS UART: node RX <- module TX, node TX -> module RX.
#define GPS_PROTO      GPS_PMTK
#define GPS_RX_PIN     15
#define GPS_TX_PIN     16
#define PPS_PIN        4

// On-board addressable RGB LED (standard ESP32-S3 breakout wiring). Keep this reserved for
// status indication; it is not a microphone, GPS, or PPS signal.
#define RGB_LED_PIN    48
#define HAS_RGB_LED    1

// ICS-43434-style mono I2S microphone.
// Validated hardware pinout: BCLK=41, WS=1, DIN=42
#define MIC_KIND       MIC_I2S
#define MIC_COUNT      1
#define MIC_BCLK_PIN   41
#define MIC_WS_PIN     1
#define MIC_DIN_PIN    42
#define MIC_CLK_PIN    -1
#define I2C_SDA_PIN    -1
#define I2C_SCL_PIN    -1
#define SD_SCK_PIN     -1
#define SD_MISO_PIN    -1
#define SD_MOSI_PIN    -1
#define FS_NOMINAL     16000     // downstream scene/gate/timebase rate; hear_node keeps the
                                 // I2S acquisition/sketch path at 48 kHz via FS_ACQ=FS_NOMINAL*DECIM.
#define MIC_BAND_LO_HZ 50
#define MIC_BAND_HI_HZ 15000

// Capability gates. The firmware leaves these buses untouched when they are absent.
#define HAS_SD         0
#define HAS_I2C        0
#define HAS_BARO       0
#define HAS_HUMIDITY   0
#define HAS_MAG        0
#define HAS_RTC        0
#define HAS_LIGHT      0
#define HAS_AIR        0

#define FREE_PADS      {2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 17, 18, 21, 38, 40, 47}
