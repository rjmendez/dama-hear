// Overnight node: PDM mic + real GPS PPS + WiFi, on a XIAO ESP32-S3 Sense.
//
// The point of leaving this outside is ONE measurement the project has never been able to make.
// firmware/path_test could only ever check the capture path, because its pulse and esp_timer came
// off the same crystal. A GPS PPS is an INDEPENDENT reference, so counting I2S samples between
// edges gives the true sample rate in Hz to GPS accuracy -- the 48000-vs-47619 class of trap that
// no datasheet answers. Over a night the block-granularity averages out to well under a ppm.
//
// Wiring (leaves the onboard PDM mic in place, since the good mic has not arrived):
//     GPS TX  -> D7 (GPIO44)      GPS RX  <- D6 (GPIO43)
//     GPS PPS -> D0 (GPIO1)       GND common, module powered at 3V3
// PPS is usually not on a drone GPS harness; tap it at the module's PPS LED pad. Meter which side
// of the LED swings first -- through an LED and series resistor, only one end is a usable edge.
//
//     cp secrets.h.example secrets.h   # then edit
//     arduino-cli compile -u -p /dev/ttyACM0 \
//       --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi firmware/night_node
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <ESP_I2S.h>
#include <SD.h>
#include "ff.h"        // f_mkfs, for /format
#include <SPI.h>
#include "driver/gpio.h"
#include <Wire.h>
#include <Update.h>
#include "esp_ota_ops.h"
#include "mel16.h"
#include "mel_scene.h"
#include "esp_heap_caps.h"

#include <esp_mac.h>

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_N                        // no secrets.h -- fall back to the node's own AP
#define WIFI_N 0
static const char *WIFI_SSIDS[] = {""};
static const char *WIFI_PASSES[] = {""};
#endif

// ---------------------------------------------------------------- identity
// A record that does not name its node cannot be paired with anything, which makes it useless for
// TDoA -- the entire point of more than one of these. Two nodes also cannot share one mDNS name or
// one AP SSID.
//
// There is deliberately NO fixed default. A default would be identical on every node, which is the
// bug this exists to prevent; an unconfigured node derives its id from its own MAC instead, so it
// is at least unique even when nobody set one. NODE_ID and NODE_CLASS live in secrets.h because
// that is the per-device file, not because they are secret.
#ifndef NODE_CLASS
#define NODE_CLASS "xiao-s3-pps"        // 1 PDM mic @16k, GPS PPS, BMP280, microSD. See docs/node-classes.md
#endif
// Set by gen_secrets.py from `git describe --always --dirty --tags` at flash time. The fallback
// matters: a sketch built by hand, without flash.py, is NOT a released build and must not be able
// to claim a commit it was not built from.
#ifndef FW_BUILD
#define FW_BUILD "unset"
#endif
static char node_id[24];
static void node_identity() {
#ifdef NODE_ID
  snprintf(node_id, sizeof node_id, "%s", NODE_ID);
#else
  uint8_t m[6]; esp_efuse_mac_get_default(m);   // the factory MAC, available before WiFi starts
  snprintf(node_id, sizeof node_id, "hear-%02x%02x%02x", m[3], m[4], m[5]);
#endif
}
#define AP_SSID   "dama-hear-node"
#define AP_PASS   "damahear"          // >=8 chars or the AP silently refuses to start

// Pins and part facts now live in one place per hardware build, so a third node is wired from a
// document rather than from whichever #define someone finds first. See firmware/boards/README.md.
#define GPS_UBX  1
#define GPS_PMTK 2
#define MIC_PDM  1
#define MIC_I2S  2
#include "../boards/xiao_s3_sense.h"

// Local aliases, kept so this file's 2700 lines do not all churn in one commit. The profile is
// the source of truth; these are the names the existing code already uses.
#define PDM_CLK   MIC_CLK_PIN
#define PDM_DIN   MIC_DIN_PIN
#define GPS_RX    GPS_RX_PIN
#define GPS_TX    GPS_TX_PIN
#define I2C_SDA   I2C_SDA_PIN
#define I2C_SCL   I2C_SCL_PIN
#define SD_SCK    SD_SCK_PIN
#define SD_MISO   SD_MISO_PIN
#define SD_MOSI   SD_MOSI_PIN

#define BLOCK      256                // finer block -> finer sample-count granularity per PPS
#define MAXDET     128               // ring, not a cap: the 65th detection used to vanish

// ---------------------------------------------------------------- PPS capture
static volatile uint32_t pps_count = 0;
static volatile uint64_t pps_us_last = 0, pps_us_first = 0, pps_us_prev = 0;
static volatile uint32_t pps_samp_last = 0, pps_samp_first = 0;
// pps_samp_last is g_samples, which advances once per BLOCK -- so it names the last COMPLETED
// block, not the edge, and is 0-255 samples (0-15.9 ms) early. That is fine for the acquisition
// audit, which only ever differences it, and NOT fine for the raw ring, whose whole job is to map
// a sample index to a UTC instant: 15.9 ms is 5.4 m of acoustic path. So the ring gets its own
// interpolated index, and the audit keeps the block-quantised one it was validated against.
static volatile uint32_t blk_end_samp = 0;     // g_samples at the last completed block
static volatile uint64_t blk_end_us   = 0;     // esp_timer at that same instant
static volatile uint32_t pps_samp_exact = 0;   // interpolated sample index AT the edge
static volatile uint32_t pps_samp_prev_exact = 0;  // and the one before it, for the mark
static volatile uint32_t pps_int_min = 0xFFFFFFFF, pps_int_max = 0;
static volatile uint32_t pps_glitch = 0;
// Set whenever the ISR has been detached and put back. /pps, /ppsv and /pinsweep take the
// interrupt away for a couple of seconds to sample the pin directly, and edges passing in that
// window are not counted -- so the NEXT interval looks like 2 or 3 seconds and sits in pps_int_max
// for the rest of the boot. Measured: one /pps call on mach turned a 4 us spread into 3000037 us.
// A diagnostic that damages the record it reports on is worse than none, and it is the same shape
// as the cumulative i2s rate one boot stall poisons for good. So the first interval after a detach
// is discarded, and the clean-rate window restarts too: samples kept accruing across the gap while
// edges did not, so any ratio spanning it is meaningless.
static volatile bool pps_resync = false;
static volatile uint32_t pps_resyncs = 0;
// A 1 Hz pulse cannot have edges closer than this. Anything faster is noise on the wire, and it
// must be counted rather than averaged in: a floating input self-oscillated at ~3.4 kHz on the
// bench and produced a confident +626 ppm sample-rate figure out of nothing.
#define PPS_MIN_GAP_US 500000
static volatile uint32_t g_samples = 0;      // updated by the audio loop, read in the ISR

// ---- acquisition audit: true sample rate, and what was lost -----------------
// measured_fs() divides CUMULATIVE samples by cumulative seconds, so one stall poisons it for
// the rest of the run: it read 13730 Hz while the node was really acquiring ~16100. A figure
// like that cannot be used to convert a sample offset into a time. This estimator averages only
// over unbroken runs of seconds and throws away any second that lost a block, so it converges on
// the rate the hardware actually clocks -- and the seconds it discards become the drop counter,
// which is the number that says whether the audio record has holes in it.
// Touched only from loop() (the web handlers run there too), so no volatile and no races.
static const char HEALTH_HDR[] =
  "node,utc_us,time_valid,uptime_s,fix,sats,tacc_ns,pps,pps_bad,spread_us,esp_ppm,samples,fs_cum_hz,"
  "fs_clean_hz,fs_win_s,drop_s,drop_samples,samp_last_s,det_n,det_written,det_lost,ambient,"
  "env_peak_win,heap,gate_armed,gate_thr,gate_e_max,gate_forced,sig_dc,sd_free_mb,write_fail,"
  "temp_c,press_hpa,c_mps,"
  // gate_floor, because gate_thr only pins the floor down where the floor is the binding limb.
  // The clip counters, because a card that filled and a night that went quiet must not look the
  // same in the record -- clip_written advances only on a clip that landed, clip_skip_budget only
  // on one that was wanted and refused.
  "gate_floor,clip_written,clip_skip_budget,clip_skip_cardfull,clip_skip_dedupe,"
  "clip_skip_ring,clip_fail,clip_budget_left,"
  // Position. lat/lon are the LAST epoch; mean_lat/mean_lon are the running average over pos_n
  // 3D fixes, which is the number to use as a TDoA focus. hacc_m is the receiver's own estimate
  // for the last epoch -- it does NOT shrink as the mean improves, so do not read it as the
  // accuracy of the mean; pos_n is what says how good the mean is.
  "lat,lon,hell_m,hmsl_m,hacc_m,vacc_m,pos_n,mean_lat,mean_lon,mean_hell_m,mean_hmsl_m";
static double   fs_clean      = (double)FS_NOMINAL;
static uint32_t fs_clean_secs = 0;    // length of the current unbroken window, in GPS seconds
static uint32_t drop_seconds  = 0;    // seconds that came up short by a block or more
static uint32_t drop_samples  = 0;    // estimated samples lost, cumulative
static uint32_t samp_sec_last = 0;    // samples acquired during the most recent GPS second
static float    env_peak_win  = 0.0f; // peak since the last health row, not since boot

// ---- the anchor: local microseconds <-> UTC ---------------------------------
// A PPS edge IS a top-of-second. Latch the local clock at the edge, then learn which second it was
// from the NAV-PVT that follows it. Any local timestamp then converts exactly:
//     utc_us = edge_unix_us + (local_us - edge_local_us)
// The whole hazard is picking the WRONG second: 1 s is 343 m, and nothing downstream can see it.
// So the association is bounded in time and the result carries its own validity rather than being
// assumed good.
static volatile uint64_t pend_local_us = 0;    // esp_timer at the most recent edge, not yet named
static volatile uint32_t pend_edge_n   = 0;
static volatile uint64_t edge_local_us = 0;    // last edge that NAV-PVT successfully named
static volatile int64_t  edge_unix_us  = 0;    // UTC of that edge, in us since the Unix epoch
static volatile bool     time_valid    = false;
static volatile int32_t  last_nano     = 0;    // NAV-PVT fractional part; large = epoch not at TOS
static volatile uint32_t time_glitch   = 0;    // labellings rejected as inconsistent
static int64_t  prev_unix_s = 0;               // last accepted label, for the +1s/edge check
static uint32_t prev_edge_n = 0;

static void IRAM_ATTR pps_isr() {
  uint64_t now = (uint64_t)esp_timer_get_time();
  uint32_t sm = g_samples;
  if (pps_count && (uint32_t)(now - pps_us_last) < PPS_MIN_GAP_US) { pps_glitch++; return; }
  if (pps_count && !pps_resync) {
    uint32_t d = (uint32_t)(now - pps_us_last);
    if (d < pps_int_min) pps_int_min = d;
    if (d > pps_int_max) pps_int_max = d;
  } else if (pps_resync) {
    pps_resync = false;          // this interval spans the probe; every later one is real
  } else {
    pps_us_first = now; pps_samp_first = sm;
  }
  pps_us_prev = pps_us_last;
  // Interpolate forward from the last completed block at the nominal rate: 16000/1e6 = 2/125,
  // integer, no FPU in the ISR. CLAMPED to one block because the reader normally keeps up, so the
  // true offset is inside [0, BLOCK); if a stall makes the elapsed time longer than that, the
  // clamp leaves the mark no worse than the block-quantised value it replaces.
  uint32_t since = (uint32_t)(((now - blk_end_us) * 2ULL) / 125ULL);
  if (since > (uint32_t)BLOCK) since = (uint32_t)BLOCK;
  pps_samp_prev_exact = pps_samp_exact;
  pps_samp_exact = blk_end_samp + since;
  pps_us_last = now; pps_samp_last = sm;
  pps_count++;
  pend_local_us = now; pend_edge_n = pps_count;   // pending: named by the NAV-PVT that follows.
  // The previous anchor stays VALID meanwhile -- blanking it here left the node unable to
  // timestamp for the ~200 ms each second before the report arrived.
}

// ---------------------------------------------------------------- GPS (minimal NMEA)
static volatile uint32_t gps_tacc_ns = 0, ubx_pvt = 0, ubx_timtp = 0, ubx_nak = 0, ubx_ack = 0;
static volatile int32_t  gps_qerr_ps = 0;
static volatile uint8_t  timtp_flags = 0xFF;
// Read-back of what the module says its timepulse config actually is. An ACK to VALSET means the
// keys were accepted, not that the pin is driving anything -- so ask.
static char tp_readback[256] = "(not read)";   // 12 fields now, not 7
static char nmea[100]; static int nmea_i = 0;
static uint8_t rawbuf[512]; static volatile uint16_t raw_i = 0; static volatile uint32_t raw_tot = 0;
static volatile uint32_t nmea_valid = 0;      // lines that actually start '$' and carry a talker id
static uint32_t gps_baud = 0;
static volatile int gps_fix = 0, gps_sats = 0;
// Node POSITION. NAV-PVT has carried lat/lon all along and this firmware parsed the same message
// for time and threw the position away -- which left the array unable to do the one thing it is
// for: a TDoA is a hyperbola, and without the focus coordinates it is a number with no geometry.
//
// A single epoch is good to a few metres, which is the same order as the path differences we are
// trying to resolve (28 ms of dog is 9.8 m). These nodes do not move, so the fix averages down:
// the mean of N independent epochs improves as sqrt(N), and an overnight run is ~30k epochs. Both
// are kept -- last for liveness, mean for geometry -- and the count is reported so nobody uses a
// 12-sample mean as if it were an 8-hour one.
static volatile int32_t  pos_lat_e7 = 0, pos_lon_e7 = 0;   // 1e-7 deg, as the wire carries them
// TWO heights, deliberately. hMSL is the one a person reads; `height` is above the WGS84
// ELLIPSOID and is the one that belongs in a geodetic transform. lat/lon/h -> ECEF is defined on
// the ellipsoid, so feeding it hMSL silently injects the geoid undulation (about -33 m here) as
// a height error. It is largely common-mode between two nodes 18 m apart and would therefore
// mostly cancel in a baseline -- which is exactly why it would never have been noticed.
static volatile int32_t  pos_hell_mm = 0;   // above WGS84 ellipsoid: USE THIS FOR GEOMETRY
static volatile int32_t  pos_hmsl_mm = 0;   // above mean sea level: for reading, not for maths
static volatile uint32_t pos_hacc_mm = 0, pos_vacc_mm = 0;
// vAcc is reported separately from hAcc because the vertical is the weak axis of any GNSS fix --
// typically 1.5-2x worse -- and a 3D solution that quotes one accuracy for all three components
// is claiming a precision it does not have in z.
static double  pos_sum_lat = 0, pos_sum_lon = 0, pos_sum_hell = 0, pos_sum_hmsl = 0;
static uint32_t pos_n = 0;
#define POS_HACC_MAX_MM 25000u   // 25 m: reject the garbage epochs, keep everything plausible
static char gps_utc[16] = "--:--:--";
static uint32_t gps_sentences = 0;

static void nmea_line(const char *s) {
  gps_sentences++;
  if (s[0] == '$' && s[1] >= 'A' && s[1] <= 'Z' && s[2] >= 'A' && s[2] <= 'Z') nmea_valid++;
  // $xxGGA,hhmmss.ss,lat,N,lon,E,fix,sats,...
  if (ubx_pvt) return;                          // UBX is authoritative once it arrives
  if (!(s[0] == '$' && s[3] == 'G' && s[4] == 'G' && s[5] == 'A')) return;
  int f = 0; const char *p = s; char tm[12] = {0};
  while (*p && f < 8) {
    if (*p == ',') {
      f++; const char *v = p + 1;
      if (f == 1) { int i = 0; while (v[i] && v[i] != ',' && i < 6) { tm[i] = v[i]; i++; } tm[i] = 0; }
      if (f == 6) gps_fix = atoi(v);
      if (f == 7) gps_sats = atoi(v);
    }
    p++;
  }
  if (strlen(tm) >= 6)
    snprintf(gps_utc, sizeof gps_utc, "%c%c:%c%c:%c%c", tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]);
}



// ---------------------------------------------------------------- I2C
// Whatever is on the bus, named. A bare address list makes you go and look it up at 2 am; the
// ambiguous ones are resolved by reading the part's own ID register instead of guessing.
static char i2c_found[256];

static bool i2c_reg(uint8_t addr, uint8_t reg, uint8_t *out) {
  Wire.beginTransmission(addr); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)addr, 1) != 1) return false;
  *out = Wire.read(); return true;
}

static const char *i2c_name(uint8_t a) {
  uint8_t id;
  switch (a) {
    case 0x0C: return "IST8308 magnetometer";
    case 0x0D: return "QMC5883L magnetometer";
    case 0x0E:                                   // IST8310 WHO_AM_I (reg 0x00) reads 0x10
      return (i2c_reg(a, 0x00, &id) && id == 0x10) ? "IST8310 magnetometer"
                                                   : "magnetometer? (0x0E, WHO_AM_I mismatch)";
    case 0x1E: return "HMC5883L / LIS3MDL magnetometer";
    case 0x30: return "MMC5883 magnetometer";
    case 0x3C: case 0x3D: return "SSD1306 OLED";
    case 0x68: case 0x69: return "MPU6050/ICM IMU or DS3231 RTC";
    case 0x76: case 0x77:                        // chip id 0xD0: BME280 0x60, BMP280 0x58
      if (i2c_reg(a, 0xD0, &id)) {
        if (id == 0x60) return "BME280 (temp+pressure+HUMIDITY)";
        if (id == 0x58) return "BMP280 (temp+pressure, NO humidity)";
        if (id == 0x61) return "BME680";
        return "BMx280-family, unrecognised chip id";
      }
      return "0x76/0x77 present, chip id unreadable";
    default: return "unknown";
  }
}

// ---------------------------------------------------------------- BMP280 (temperature, pressure)
// Sound speed is the one environmental term that does NOT cancel in TDoA: it biases every node in
// the same direction, so a shared error in c moves every range together and the residual never
// sees it. c = 331.3 + 0.606*T, so 0.6 m/s per degree -- a 10 C overnight swing is 1.8% on every
// range. Without this the node assumes a temperature, and the assumption is invisible downstream.
//
// The part is identified by CHIP ID, not by the address it answers on. The board fitted here is
// LABELLED BME280 and reports 0x58, which is BMP280 silicon -- temperature and pressure, no
// humidity. Common with these modules. Humidity would have been worth about 0.5 m/s between dry
// and saturated air at 20 C (0.15%); temperature is the term that matters and it is present.
#define BMP_ADDR_A 0x76
#define BMP_ADDR_B 0x77
static uint8_t bmp_addr = 0;                 // 0 = not present
static uint16_t bmp_T1; static int16_t bmp_T2, bmp_T3;
static uint16_t bmp_P1; static int16_t bmp_P2, bmp_P3, bmp_P4, bmp_P5, bmp_P6, bmp_P7, bmp_P8, bmp_P9;
static float bmp_temp_c = NAN, bmp_press_hpa = NAN;
static uint32_t bmp_reads = 0, bmp_fail = 0;

static bool bmp_block(uint8_t reg, uint8_t *buf, uint8_t n) {
  Wire.beginTransmission(bmp_addr); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)bmp_addr, (int)n) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}
static bool bmp_w8(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(bmp_addr); Wire.write(reg); Wire.write(v);
  return Wire.endTransmission() == 0;
}

static bool bmp_begin() {
  for (uint8_t a = BMP_ADDR_A; a <= BMP_ADDR_B; a++) {
    uint8_t id;
    bmp_addr = a;
    if (!i2c_reg(a, 0xD0, &id)) continue;
    if (id != 0x58 && id != 0x60 && id != 0x61) continue;   // BMP280 / BME280 / BME680
    uint8_t c[24];
    if (!bmp_block(0x88, c, 24)) continue;
    bmp_T1 = (uint16_t)(c[1] << 8 | c[0]);  bmp_T2 = (int16_t)(c[3] << 8 | c[2]);
    bmp_T3 = (int16_t)(c[5] << 8 | c[4]);   bmp_P1 = (uint16_t)(c[7] << 8 | c[6]);
    bmp_P2 = (int16_t)(c[9] << 8 | c[8]);   bmp_P3 = (int16_t)(c[11] << 8 | c[10]);
    bmp_P4 = (int16_t)(c[13] << 8 | c[12]); bmp_P5 = (int16_t)(c[15] << 8 | c[14]);
    bmp_P6 = (int16_t)(c[17] << 8 | c[16]); bmp_P7 = (int16_t)(c[19] << 8 | c[18]);
    bmp_P8 = (int16_t)(c[21] << 8 | c[20]); bmp_P9 = (int16_t)(c[23] << 8 | c[22]);
    // A dig_T1 of 0 or 0xFFFF means the calibration block did not really read: the compensation
    // would then return a confident wrong temperature rather than failing, which is worse.
    if (bmp_T1 == 0 || bmp_T1 == 0xFFFF) continue;
    bmp_w8(0xF5, 0xA0);                  // t_sb 1000 ms, filter off -- this is a slow variable
    bmp_w8(0xF4, (2 << 5) | (2 << 2) | 3);   // osrs_t x2, osrs_p x2, NORMAL mode (free-running)
    logf("bmp   0x%02X chip 0x%02X, dig_T1=%u -- temperature live\n", a, id, bmp_T1);
    return true;
  }
  bmp_addr = 0;
  return false;
}

// Datasheet compensation, integer path for temperature (t_fine) and 64-bit for pressure.
static void bmp_read() {
  if (!bmp_addr) return;
  uint8_t d[6];
  if (!bmp_block(0xF7, d, 6)) { bmp_fail++; return; }
  int32_t adc_P = ((int32_t)d[0] << 12) | ((int32_t)d[1] << 4) | (d[2] >> 4);
  int32_t adc_T = ((int32_t)d[3] << 12) | ((int32_t)d[4] << 4) | (d[5] >> 4);
  if (adc_T == 0x80000 || adc_P == 0x80000) { bmp_fail++; return; }   // reset value = no sample yet
  int32_t v1 = ((((adc_T >> 3) - ((int32_t)bmp_T1 << 1))) * ((int32_t)bmp_T2)) >> 11;
  int32_t v2 = (((((adc_T >> 4) - ((int32_t)bmp_T1)) * ((adc_T >> 4) - ((int32_t)bmp_T1))) >> 12) *
                ((int32_t)bmp_T3)) >> 14;
  int32_t t_fine = v1 + v2;
  bmp_temp_c = ((t_fine * 5 + 128) >> 8) / 100.0f;
  int64_t p1 = ((int64_t)t_fine) - 128000;
  int64_t p2 = p1 * p1 * (int64_t)bmp_P6;
  p2 = p2 + ((p1 * (int64_t)bmp_P5) << 17);
  p2 = p2 + (((int64_t)bmp_P4) << 35);
  p1 = ((p1 * p1 * (int64_t)bmp_P3) >> 8) + ((p1 * (int64_t)bmp_P2) << 12);
  p1 = (((((int64_t)1) << 47) + p1)) * ((int64_t)bmp_P1) >> 33;
  if (p1 == 0) { bmp_press_hpa = NAN; bmp_reads++; return; }          // divide-by-zero guard
  int64_t p = 1048576 - adc_P;
  p = (((p << 31) - p2) * 3125) / p1;
  p1 = (((int64_t)bmp_P9) * (p >> 13) * (p >> 13)) >> 25;
  p2 = (((int64_t)bmp_P8) * p) >> 19;
  p = ((p + p1 + p2) >> 8) + (((int64_t)bmp_P7) << 4);
  bmp_press_hpa = (float)p / 25600.0f;                                // Q24.8 Pa -> hPa
  bmp_reads++;
}

// The whole point of measuring temperature. NAN in, NAN out -- a node that does not know its
// temperature must say so rather than quietly returning the 20 C answer.
static float sound_speed_mps() {
  return (bmp_temp_c == bmp_temp_c) ? 331.3f + 0.606f * bmp_temp_c : NAN;
}

// ---------------------------------------------------------------- measuring the line, not guessing
// Trying candidate baud rates answers "does THIS rate decode", which is silence when the answer is
// none of them -- exactly what mach reports: a driven RX line, 512 bytes of undecodable data, and
// 0 NMEA / 0 UBX at all eight rates. Measuring the line answers "what rate IS it", which is a
// different and much more useful question, and it works whether the module is speaking NMEA, UBX
// or something else entirely.
//
// The shortest run of one level on an asynchronous line is one bit time, because a UART frame
// always contains at least one isolated bit somewhere in ordinary traffic. digitalRead() costs
// under a microsecond here, so the floor of what this can resolve is a few hundred kbaud -- ample
// for anything a GPS module ships with.
static uint32_t gps_bit_time_us(int pin, uint32_t window_ms, uint32_t *hist_out, uint32_t *min_out);
static uint32_t gps_min_pulse_us(int pin, uint32_t window_ms) {
  return gps_bit_time_us(pin, window_ms, NULL, NULL);
}

// The MINIMUM run length is the wrong statistic and measuring it taught me so: on mach, with a
// link provably healthy at 115200 (3440 sentences, fix 3, a UBX ACK), the minimum run was 3 us,
// implying 333 kbaud, and the endpoint duly announced that no standard rate fitted and that two
// drivers must be fighting. One sub-bit glitch -- from digitalRead's own sampling jitter or from
// the line itself -- is enough to do that, because a minimum has no defence against a single
// outlier. It was a diagnostic contradicting a working link, which is worse than no diagnostic.
//
// So: histogram the run lengths and take the shortest one that happens OFTEN. A real bit time
// recurs thousands of times in 400 ms of traffic; a glitch does not. `min_out` still reports the
// raw minimum, because the gap between the two is itself the evidence that glitches are present.
#define GPS_RUN_MAX 400          // us; anything longer is idle, not a bit
#define GPS_RUN_QUORUM 20        // a run length must recur this often to count as the bit time
static uint32_t gps_bit_time_us(int pin, uint32_t window_ms,
                                uint32_t *hist_out, uint32_t *min_out) {
  pinMode(pin, INPUT);
  static uint32_t h[GPS_RUN_MAX + 1];
  memset(h, 0, sizeof h);
  uint32_t rawmin = 0xFFFFFFFFu;
  uint64_t t0 = (uint64_t)esp_timer_get_time(), tlast = t0;
  int last = digitalRead(pin);
  while ((uint64_t)esp_timer_get_time() - t0 < (uint64_t)window_ms * 1000ULL) {
    int v = digitalRead(pin);
    if (v != last) {
      uint64_t now = (uint64_t)esp_timer_get_time();
      uint32_t run = (uint32_t)(now - tlast);
      if (run) {
        if (run < rawmin) rawmin = run;
        if (run <= GPS_RUN_MAX) h[run]++;
      }
      tlast = now; last = v;
    }
  }
  if (min_out) *min_out = (rawmin == 0xFFFFFFFFu) ? 0 : rawmin;
  if (hist_out) memcpy(hist_out, h, sizeof h);
  for (uint32_t r = 1; r <= GPS_RUN_MAX; r++) if (h[r] >= GPS_RUN_QUORUM) return r;
  return 0;                      // nothing recurred often enough to be a bit time
}

// Snap a measured bit time to the nearest standard rate, and report how far off it was. A big
// residual means the line is not a UART at this rate -- inverted logic, a different protocol, or
// contention from two drivers -- and saying so beats returning a confident wrong number.
static const uint32_t GPS_BAUDS[] = {1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600,
                                     76800, 115200, 128000, 230400, 256000, 460800, 921600};
static uint32_t gps_snap_baud(uint32_t bit_us, float *err_pct_out) {
  if (!bit_us) { if (err_pct_out) *err_pct_out = 0; return 0; }
  double measured = 1e6 / (double)bit_us;
  uint32_t best = 0; double bestrel = 1e9;
  for (unsigned i = 0; i < sizeof GPS_BAUDS / sizeof GPS_BAUDS[0]; i++) {
    double rel = fabs(measured - (double)GPS_BAUDS[i]) / (double)GPS_BAUDS[i];
    if (rel < bestrel) { bestrel = rel; best = GPS_BAUDS[i]; }
  }
  if (err_pct_out) *err_pct_out = (float)(bestrel * 100.0);
  return best;
}

// ---------------------------------------------------------------- which pin is the module on
// GPS_RX / GPS_TX are the DOCUMENTED wiring, and on mach the pair is reversed. A swapped UART pair
// is invisible from the protocol side -- you get a silent line and no ACK, which reads exactly like
// a dead module -- so it gets found by measurement instead. Only one of the two can be carrying a
// transmitter, and gps_min_pulse_us() says which without needing to know the baud or the protocol.
//
// Run BEFORE Serial1.begin(), while both pins are still inputs; afterwards one of them is an output
// this node drives, and probing it would only measure ourselves.
static int gps_rx_pin = GPS_RX, gps_tx_pin = GPS_TX;
static const char *gps_pin_src = "default (not probed)";
static uint32_t gps_pulse_d7 = 0, gps_pulse_d6 = 0;

static void gps_pick_pins() {
  gps_pulse_d7 = gps_min_pulse_us(GPS_RX, 250);
  gps_pulse_d6 = gps_min_pulse_us(GPS_TX, 250);
  if (gps_pulse_d7 && !gps_pulse_d6) {
    gps_rx_pin = GPS_RX; gps_tx_pin = GPS_TX; gps_pin_src = "measured: as documented";
  } else if (gps_pulse_d6 && !gps_pulse_d7) {
    // The wires are reversed. Follow the hardware rather than refuse it: the node's job is to hear
    // the module, and which copper it arrives on is not a thing worth being principled about.
    gps_rx_pin = GPS_TX; gps_tx_pin = GPS_RX; gps_pin_src = "measured: SWAPPED at the module";
  } else if (gps_pulse_d6 && gps_pulse_d7) {
    gps_pin_src = "both pins toggled -- ambiguous, using documented wiring";
  } else {
    gps_pin_src = "neither pin toggled -- module silent, using documented wiring";
  }
  logf("gps   pins %s (D7 %lu us, D6 %lu us) -> RX=GPIO%d TX=GPIO%d\n", gps_pin_src,
       (unsigned long)gps_pulse_d7, (unsigned long)gps_pulse_d6, gps_rx_pin, gps_tx_pin);
}

static void i2c_scan() {
  int n = 0; i2c_found[0] = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() != 0) continue;
    char b[80];
    snprintf(b, sizeof b, "%s0x%02X %s", n ? "; " : "", a, i2c_name(a));
    if (strlen(i2c_found) + strlen(b) < sizeof(i2c_found) - 1) strcat(i2c_found, b);
    n++;
  }
  if (!n) snprintf(i2c_found, sizeof i2c_found, "nothing on the bus");
  logf("i2c   SDA=%d SCL=%d: %s\n", I2C_SDA, I2C_SCL, i2c_found);
}

// ---------------------------------------------------------------- u-blox UBX
// Key IDs and message layouts taken from u-blox M10 SPG 5.10 Interface Description UBX-21035062,
// not from memory. The top nibble of a key encodes its size: 0x1=1 bit, 0x2=1 B, 0x4=4 B.
#define K_TP1_ENA        0x10050007UL   // L
#define K_SYNC_GNSS_TP1  0x10050008UL   // L  set: sync to GNSS when valid, else local clock
#define K_USE_LOCKED_TP1 0x10050009UL   // L  set: *_LOCK_* apply once locked
#define K_ALIGN_TOW_TP1  0x1005000aUL   // L
#define K_POL_TP1        0x1005000bUL   // L  1 = rising edge at top of second
#define K_PULSE_DEF      0x20050023UL   // E1 0 = period
#define K_PULSE_LEN_DEF  0x20050030UL   // E1 1 = length (so LEN_* are used, not DUTY_*)
#define K_PERIOD_TP1     0x40050002UL   // U4 us, unlocked
#define K_PERIOD_LOCK    0x40050003UL   // U4 us, locked
#define K_LEN_TP1        0x40050004UL   // U4 us, unlocked
#define K_LEN_LOCK       0x40050005UL   // U4 us, locked
// The module's SOLUTION rate, which this firmware never set -- so each one ran at whatever it
// shipped with. Measured: nyquist's emits NAV-PVT at 4.98/s, mach's at 9.65/s, against a
// labelling path that assumes roughly one report per PPS edge. That mismatch is why mach rejects
// about two labellings a second and nyquist rejects none: at 10 Hz, far more reports land in the
// window belonging to an edge that has not advanced yet, and the +1s-per-edge guard correctly
// throws them out. Both nodes end up with valid time, but they are not behaving the same way, and
// they are about to be asked to agree with each other to microseconds.
#define K_RATE_MEAS      0x30210001UL   // U2 ms between measurements
#define K_RATE_NAV       0x30210002UL   // U2 measurements per navigation solution
#define K_MSG_NAV_PVT    0x20910007UL   // U1 rate on UART1
#define K_MSG_TIM_TP     0x2091017eUL   // U1 rate on UART1

static uint8_t ubx_buf[256];

static void ubx_send(uint8_t cls, uint8_t id, const uint8_t *pl, uint16_t n) {
  uint8_t h[6] = {0xB5, 0x62, cls, id, (uint8_t)(n & 0xFF), (uint8_t)(n >> 8)};
  uint8_t a = 0, b = 0;
  for (int i = 2; i < 6; i++) { a += h[i]; b += a; }
  for (uint16_t i = 0; i < n; i++) { a += pl[i]; b += a; }
  Serial1.write(h, 6); if (n) Serial1.write(pl, n);
  uint8_t ck[2] = {a, b}; Serial1.write(ck, 2);
}

static uint16_t vs_i;
static void vs_begin() { vs_i = 0; ubx_buf[vs_i++] = 0; ubx_buf[vs_i++] = 0x01;   // version 0, RAM layer
                         ubx_buf[vs_i++] = 0; ubx_buf[vs_i++] = 0; }
static void vs_add(uint32_t key, uint32_t val) {
  // Key size lives in bits 30-28: 1=bit, 2=U1, 3=U2, 4=U4. U2 was not needed until CFG-RATE, and
  // sending a U2 key with one byte of payload gets the whole VALSET NAKed.
  int st = (key >> 28) & 0x7;
  int w = st == 4 ? 4 : st == 3 ? 2 : 1;
  for (int i = 0; i < 4; i++) ubx_buf[vs_i++] = (key >> (8 * i)) & 0xFF;
  for (int i = 0; i < w; i++) ubx_buf[vs_i++] = (val >> (8 * i)) & 0xFF;
}
static void vs_send() { ubx_send(0x06, 0x8A, ubx_buf, vs_i); }

// Applied at every boot into the RAM layer only -- the module's own flash is never written, so
// nothing here is a permanent change to the operator's hardware. Power-cycle and it is stock.
static void gps_valget() {           // ask the module what TP1 is actually set to
  vs_i = 0; ubx_buf[vs_i++] = 0; ubx_buf[vs_i++] = 0x00;   // version 0, layer 0 = RAM
  ubx_buf[vs_i++] = 0; ubx_buf[vs_i++] = 0;
  const uint32_t keys[] = {K_TP1_ENA, K_PULSE_DEF, K_PERIOD_TP1, K_PERIOD_LOCK,
                           K_LEN_TP1, K_LEN_LOCK, K_USE_LOCKED_TP1,
                           // POL decides which way round the pulse is, and /pps's "~10% high"
                           // verdict is only true for POL=1. Reading everything EXCEPT the field
                           // that could invalidate the reading was not a useful read-back.
                           K_POL_TP1, K_ALIGN_TOW_TP1, K_SYNC_GNSS_TP1,
                           K_RATE_MEAS, K_RATE_NAV};
  for (unsigned k = 0; k < sizeof(keys) / sizeof(keys[0]); k++)
    for (int i = 0; i < 4; i++) ubx_buf[vs_i++] = (keys[k] >> (8 * i)) & 0xFF;
  ubx_send(0x06, 0x8B, ubx_buf, vs_i);
}

// Change ONLY the pulse length, live, without disturbing anything else. For finding the timepulse
// on a bench: a 100 ms pulse in 1000 ms is a 10% duty that a multimeter averages to ~0.33 V and
// that is easy to miss, whereas 500 ms is a square wave reading ~1.65 V -- unmistakably different
// from both a 0 V ground and a 3.3 V rail, with no scope needed.
//
// RAM layer only, like every other write here, so a power cycle restores the shipped 100 ms even
// if nobody remembers to. That is the safety property that makes this endpoint reasonable to
// expose at all.
static void gps_set_pulse_len(uint32_t us) {
  vs_begin();
  vs_add(K_LEN_TP1, us); vs_add(K_LEN_LOCK, us);
  vs_send();
}

static void gps_configure() {
  vs_begin();
  vs_add(K_PULSE_DEF, 0); vs_add(K_PULSE_LEN_DEF, 1);
  vs_add(K_PERIOD_TP1, 1000000); vs_add(K_PERIOD_LOCK, 1000000);   // 1 Hz locked AND unlocked
  vs_add(K_LEN_TP1, 100000); vs_add(K_LEN_LOCK, 100000);           // 100 ms, visible on the LED
  vs_add(K_TP1_ENA, 1); vs_add(K_USE_LOCKED_TP1, 1);
  vs_add(K_ALIGN_TOW_TP1, 1); vs_add(K_POL_TP1, 1);
  vs_add(K_SYNC_GNSS_TP1, 1);
  vs_add(K_RATE_MEAS, 1000); vs_add(K_RATE_NAV, 1);   // exactly one solution per PPS edge
  vs_add(K_MSG_NAV_PVT, 1); vs_add(K_MSG_TIM_TP, 1);
  vs_send();
}

// UBX receive: NAV-PVT for fix/sats/tAcc, TIM-TP for the pulse quantisation error.
static uint8_t ux[128]; static int ux_n = 0, ux_state = 0; static uint16_t ux_len = 0;
static uint8_t ux_cls, ux_id, ux_a, ux_b;

static void ubx_msg() {
  if (ux_cls == 0x01 && ux_id == 0x07 && ux_len >= 24) {          // NAV-PVT
    {
      uint16_t yr = (uint16_t)ux[4] | ((uint16_t)ux[5] << 8);
      uint8_t mo = ux[6], dy = ux[7], hh = ux[8], mi = ux[9], ss = ux[10], vf = ux[11];
      last_nano = (int32_t)((uint32_t)ux[16] | ((uint32_t)ux[17] << 8) |
                            ((uint32_t)ux[18] << 16) | ((uint32_t)ux[19] << 24));
      // validDate|validTime|fullyResolved = bits 0|1|2
      if ((vf & 0x07) == 0x07 && yr > 2000) {
        // days from civil (Howard Hinnant's algorithm) -- no time.h, no timezone, no surprises
        int y = yr; int m = mo;
        y -= m <= 2;
        int era = (y >= 0 ? y : y - 399) / 400;
        unsigned yoe = (unsigned)(y - era * 400);
        unsigned doy = (153u * (m + (m > 2 ? -3 : 9)) + 2u) / 5u + dy - 1;
        unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        long long days = (long long)era * 146097 + (long long)doe - 719468;
        long long unix_s = days * 86400LL + hh * 3600LL + mi * 60LL + ss;
        uint64_t now_us = (uint64_t)esp_timer_get_time();
        // Only label an edge we actually saw, and only if this solution is for THAT second: the
        // report follows its own epoch by well under a second. Outside that window we do not
        // guess -- an unlabelled edge is honest, a mislabelled one is 343 m of lie.
        if (pend_edge_n && (now_us - pend_local_us) < 900000ULL) {
          // The time window alone is not enough. NAV-PVT's own epoch sits ~200 ms past the second
          // here, so a LATE report can arrive just after the NEXT edge and land inside the window
          // -- labelling that edge with the previous second. That is the 343 m error, and it looks
          // completely normal. So require the label to advance exactly one second per edge.
          bool ok = true;
          if (prev_edge_n && prev_unix_s) {
            long long d_sec = (long long)unix_s - prev_unix_s;
            long long d_edge = (long long)pend_edge_n - (long long)prev_edge_n;
            if (d_sec != d_edge) { ok = false; time_glitch++; }
          }
          if (ok) {                                   // commit local+utc as one matched pair
            edge_local_us = pend_local_us;
            edge_unix_us = (int64_t)unix_s * 1000000LL;
            time_valid = true;
          }
          prev_unix_s = unix_s; prev_edge_n = pend_edge_n;   // re-sync either way
        }
      }
    }
    gps_tacc_ns = (uint32_t)ux[12] | ((uint32_t)ux[13] << 8) | ((uint32_t)ux[14] << 16) | ((uint32_t)ux[15] << 24);
    gps_fix = ux[20]; gps_sats = ux[23];
    // lon/lat/hMSL/hAcc live at 24/28/36/40 -- so this needs a payload of 48, not the 24 the
    // guard above asks for. Check before reading rather than trusting the message length.
    if (ux_len >= 48) {
      int32_t lon = (int32_t)((uint32_t)ux[24] | ((uint32_t)ux[25] << 8) | ((uint32_t)ux[26] << 16) | ((uint32_t)ux[27] << 24));
      int32_t lat = (int32_t)((uint32_t)ux[28] | ((uint32_t)ux[29] << 8) | ((uint32_t)ux[30] << 16) | ((uint32_t)ux[31] << 24));
      int32_t hel = (int32_t)((uint32_t)ux[32] | ((uint32_t)ux[33] << 8) | ((uint32_t)ux[34] << 16) | ((uint32_t)ux[35] << 24));
      int32_t hms = (int32_t)((uint32_t)ux[36] | ((uint32_t)ux[37] << 8) | ((uint32_t)ux[38] << 16) | ((uint32_t)ux[39] << 24));
      uint32_t ha = (uint32_t)ux[40] | ((uint32_t)ux[41] << 8) | ((uint32_t)ux[42] << 16) | ((uint32_t)ux[43] << 24);
      uint32_t va = (uint32_t)ux[44] | ((uint32_t)ux[45] << 8) | ((uint32_t)ux[46] << 16) | ((uint32_t)ux[47] << 24);
      pos_lat_e7 = lat; pos_lon_e7 = lon;
      pos_hell_mm = hel; pos_hmsl_mm = hms; pos_hacc_mm = ha; pos_vacc_mm = va;
      // Only a 3D fix inside the accuracy cap joins the average. A 2D fix has no height and a
      // wandering horizontal solution, and averaging it in makes the mean worse, not noisier.
      if (gps_fix == 3 && ha && ha < POS_HACC_MAX_MM) {
        pos_sum_lat += (double)lat; pos_sum_lon += (double)lon;
        pos_sum_hell += (double)hel; pos_sum_hmsl += (double)hms; pos_n++;
      }
    }
    snprintf(gps_utc, sizeof gps_utc, "%02u:%02u:%02u", ux[8], ux[9], ux[10]);
    ubx_pvt++;
  } else if (ux_cls == 0x0D && ux_id == 0x01 && ux_len >= 16) {   // TIM-TP
    gps_qerr_ps = (int32_t)((uint32_t)ux[8] | ((uint32_t)ux[9] << 8) | ((uint32_t)ux[10] << 16) | ((uint32_t)ux[11] << 24));
    timtp_flags = ux[14];          // bit0 timeBase, bit1 utc, bit4 qErrInvalid
    ubx_timtp++;
  } else if (ux_cls == 0x06 && ux_id == 0x8B && ux_len > 4) {     // CFG-VALGET response
    char o[256]; int n = 0; uint16_t i = 4;
    while (i + 4 <= ux_len && n < (int)sizeof(o) - 24) {
      uint32_t key = (uint32_t)ux[i] | ((uint32_t)ux[i+1] << 8) | ((uint32_t)ux[i+2] << 16) | ((uint32_t)ux[i+3] << 24);
      // Same size table as vs_add. Reading a U2 key as one byte does not just print the low
      // byte (1000 -> 232); it advances the cursor by one byte too few, so every field
      // after it decodes from the wrong offset. The value was written correctly -- the
      // module measurably runs at 1 Hz -- only the read-back lied about it.
      int stz = (key >> 28) & 0x7;
      int w = stz == 4 ? 4 : stz == 3 ? 2 : 1;
      uint32_t v = 0;
      for (int k = 0; k < w && i + 4 + k < ux_len; k++) v |= (uint32_t)ux[i + 4 + k] << (8 * k);
      const char *nm = key == K_TP1_ENA ? "TP1_ENA" : key == K_PERIOD_TP1 ? "PERIOD"
                     : key == K_PERIOD_LOCK ? "PERIOD_LOCK" : key == K_LEN_TP1 ? "LEN"
                     : key == K_LEN_LOCK ? "LEN_LOCK" : key == K_PULSE_DEF ? "PULSE_DEF"
                     : key == K_USE_LOCKED_TP1 ? "USE_LOCKED" : key == K_POL_TP1 ? "POL"
                     : key == K_ALIGN_TOW_TP1 ? "ALIGN_TOW" : key == K_SYNC_GNSS_TP1 ? "SYNC_GNSS"
                     : key == K_RATE_MEAS ? "RATE_MEAS" : key == K_RATE_NAV ? "RATE_NAV"
                     : "?";
      n += snprintf(o + n, sizeof(o) - n, "%s%s=%lu", n ? " " : "", nm, (unsigned long)v);
      i += 4 + w;
    }
    strncpy(tp_readback, o, sizeof(tp_readback) - 1); tp_readback[sizeof(tp_readback) - 1] = 0;
  } else if (ux_cls == 0x05) { if (ux_id == 0x01) ubx_ack++; else ubx_nak++; }
}

static void ubx_feed(uint8_t c) {
  switch (ux_state) {
    case 0: if (c == 0xB5) ux_state = 1; break;
    case 1: ux_state = (c == 0x62) ? 2 : 0; break;
    case 2: ux_cls = c; ux_a = c; ux_b = c; ux_state = 3; break;
    case 3: ux_id = c; ux_a += c; ux_b += ux_a; ux_state = 4; break;
    case 4: ux_len = c; ux_a += c; ux_b += ux_a; ux_state = 5; break;
    case 5: ux_len |= (uint16_t)c << 8; ux_a += c; ux_b += ux_a; ux_n = 0;
            ux_state = (ux_len > sizeof(ux)) ? 0 : (ux_len ? 6 : 7); break;
    case 6: ux[ux_n++] = c; ux_a += c; ux_b += ux_a; if (ux_n >= (int)ux_len) ux_state = 7; break;
    case 7: ux_state = (c == ux_a) ? 8 : 0; break;
    case 8: if (c == ux_b) ubx_msg(); ux_state = 0; break;
  }
}

// ---------------------------------------------------------------- sketch
// Same log-mel sketch hear/sketch.py produces, at the PDM mic's rate. A detection that carries
// only a timestamp says something happened; the sketch says what it sounded like, and the central
// side can retrain on the same bytes forever. That is the whole argument for a sketch over a
// verdict (docs/uplink.md).
#define ARING 4096                       // ~256 ms at 16 kHz: pre-roll plus the sketch window
static int16_t aring[ARING];
static volatile uint32_t aring_w = 0;
static volatile uint32_t aring_total = 0;   // so we never sketch a buffer that has not filled

static float fft_re[MEL16_NFFT], fft_im[MEL16_NFFT];
static float tw_re[MEL16_NFFT / 2], tw_im[MEL16_NFFT / 2];
static void fft_init() {
  for (int k = 0; k < MEL16_NFFT / 2; k++) {
    float a = -2.0f * (float)M_PI * k / MEL16_NFFT; tw_re[k] = cosf(a); tw_im[k] = sinf(a);
  }
}
static void fft256() {
  const int N = MEL16_NFFT;
  for (int i = 1, j = 0; i < N; i++) {
    int bit = N >> 1; for (; j & bit; bit >>= 1) j ^= bit; j ^= bit;
    if (i < j) { float t = fft_re[i]; fft_re[i] = fft_re[j]; fft_re[j] = t;
                 t = fft_im[i]; fft_im[i] = fft_im[j]; fft_im[j] = t; }
  }
  for (int len = 2; len <= N; len <<= 1) {
    int step = N / len;
    for (int i = 0; i < N; i += len)
      for (int k = 0; k < len / 2; k++) {
        int a = i + k, b = a + len / 2;
        float cr = tw_re[k * step], ci = tw_im[k * step];
        float xr = fft_re[b] * cr - fft_im[b] * ci, xi = fft_re[b] * ci + fft_im[b] * cr;
        fft_re[b] = fft_re[a] - xr; fft_im[b] = fft_im[a] - xi;
        fft_re[a] += xr;            fft_im[a] += xi;
      }
  }
}

// Build the 172 B frame from the ring, starting `back` samples before the write head.
//: Samples the sketch spans: (FRAMES-1) hops plus one analysis window.
#define SKETCH_SPAN ((uint32_t)(MEL16_NFFT + (MEL16_FRAMES - 1) * MEL16_HOP))
//: How far BEFORE the trigger the window starts. One hop, matching hear/node/detect.py
//: SKETCH_BACK_S = 0.004 s, which is exactly MEL16_HOP at 16 kHz.
#define SKETCH_BACK ((uint32_t)MEL16_HOP)
//: Two detections closer than this are one event's decay. hear/node/detect.py RETRIGGER_S.
#define RETRIGGER_SAMPLES ((uint32_t)(0.060 * (double)FS_NOMINAL))

// ⚠️TAKES AN ABSOLUTE SAMPLE INDEX AND RUNS FORWARD FROM IT. It used to take a look-BACK from
// the write pointer, which put the window at [T-46.0 ms, T-2.0 ms] -- ending 2 ms before the
// trigger, so not one shipped frame has ever contained the event that caused it. The comment at
// the call site claimed the opposite. Measured over 1123 field frames: the loudest time frame is
// the LAST of 8 in 33.9% of them (uniform would be 12.5%) and one of the last two in 47.3%,
// which is the event arriving at the edge of a window that stops before it.
static int sketch_frame(uint8_t *out, uint32_t start_abs, uint32_t node_us, uint16_t peak, uint16_t flags) {
  static float db[MEL16_BANDS * MEL16_FRAMES];
  uint32_t start = start_abs % ARING;
  for (int t = 0; t < MEL16_FRAMES; t++) {
    uint32_t s0 = start + (uint32_t)t * MEL16_HOP;
    for (int i = 0; i < MEL16_NFFT; i++) {
      fft_re[i] = (float)aring[(s0 + i) % ARING] * MEL16_WIN[i];
      fft_im[i] = 0.0f;
    }
    fft256();
    const float *w = MEL16_FB_W;
    for (int b = 0; b < MEL16_BANDS; b++) {
      int lo = MEL16_FB_LO[b], cnt = MEL16_FB_N[b];
      float acc = 0.0f;
      for (int i = 0; i < cnt; i++) {
        int bin = lo + i; acc += w[i] * (fft_re[bin] * fft_re[bin] + fft_im[bin] * fft_im[bin]);
      }
      w += cnt;
      db[b * MEL16_FRAMES + t] = 10.0f * log10f(acc + 1e-12f);
    }
  }
  float ref = db[0];
  for (int i = 1; i < MEL16_BANDS * MEL16_FRAMES; i++) if (db[i] > ref) ref = db[i];
  int16_t r4 = (int16_t)lrintf(ref * 4.0f);
  out[0] = node_us; out[1] = node_us >> 8; out[2] = node_us >> 16; out[3] = node_us >> 24;
  out[4] = r4; out[5] = r4 >> 8; out[6] = peak; out[7] = peak >> 8;
  out[8] = MEL16_BANDS; out[9] = MEL16_FRAMES; out[10] = flags; out[11] = flags >> 8;
  for (int i = 0; i < MEL16_BANDS * MEL16_FRAMES; i++) {
    float v = roundf((db[i] - ref) * 2.0f);
    out[12 + i] = (uint8_t)(int8_t)(v < -128 ? -128 : (v > 127 ? 127 : v));
  }
  return MEL16_FRAME_BYTES;
}

// ---------------------------------------------------------------- gate (as hear/node/detect.py)
static float g_amb = 0, env_sum = 0, env_buf[16]; static int env_i = 0, armed = 1;
static const float ENV_INV = 1.0f / 16.0f, ALPHA = 1.0f / 10000.0f;
static const float RATIO = 8.0f, FLOOR_DEFAULT = 800.0f, REARM = 0.35f;
// THE FLOOR IS RUNTIME-SETTABLE (POST /gate?floor=N). It used to be a compile-time 800, and over
// the 2026-09-07 capture it -- not the adaptive 8 x ambient limb -- was what the gate actually
// ran on: gate_thr was exactly 800.0 in 1418 of 1450 health.csv rows (97.8%), max 1715. Median
// ambient over those rows is 28.9, so 8 x ambient is ~231 and the floor was ~3.5x above it all
// night. 800 was chosen for gunshots; retuning it for anything quieter meant a reflash and a walk
// outside, which is why it never got retuned.
//
// GUARD, AND WHY THESE TWO BOUNDS.
//   FLOOR_MIN = 100. Below roughly this value the knob stops doing anything: counting the 30 s
//   health rows whose gate_e_max exceeded max(floor, 8 x ambient) -- windows that would have
//   contained at least one crossing -- gives 1043 of 1450 at floor 100 and 1045 at floor 0, a
//   0.2 pp difference, because the adaptive limb has taken over as the binding one. A setting
//   that reads as a change and is not one is worse than a refusal. For scale on the same recipe:
//   800 -> 38 (2.6%), 600 -> 100, 400 -> 443, 200 -> 982, 100 -> 1043 (71.9%).
//   FLOOR_MAX = 32768. The envelope is a 16-sample mean of |int16|, so it cannot exceed 32768 by
//   construction; a floor at or above that is a gate that can never fire, which is indis-
//   tinguishable from a dead microphone -- the exact failure env_e_max_win exists to rule out.
//
// ⚠️NEITHER BOUND PROTECTS THE CARD, and it would be a lie to imply one does. Floor 100 gives 27x
// the crossing rate of floor 800 on the measured night. What bounds the card is downstream:
// det_flush writes at most 16 rows per second (16 x 437 B = 6992 B/s, so 19 MiB in 47 min at
// the absolute cap), and the clip writer -- which at 128044 B a clip would fill the card in about
// three minutes at that rate -- is held by its own byte budget (CLIP_BUDGET_B). Lower this floor
// on an unattended node only with that budget in place.
static const float FLOOR_MIN = 100.0f, FLOOR_MAX = 32768.0f;
static float g_floor = FLOOR_DEFAULT;
// "default" until something overrides it, then "file" or "http". Reported in /status so the
// active value never has to be inferred from gate_thr, which only bounds it from above whenever
// the adaptive limb is the binding one (32 of the capture's 1450 rows).
static const char *g_floor_src = "default";
// The value /gate.cfg currently holds, or NaN for "no file". Cached rather than re-read, and
// reported as a NUMBER rather than a yes/no: a bare "persisted: true" beside an active floor of
// 300 while the file still said 800 would be true and useless -- what the operator needs to know
// is what the node will come back as after the plug timer cuts it.
static float g_floor_saved = NAN;
// A night that records nothing is ambiguous: was it quiet, or was the threshold set above
// everything that happened? Track the highest envelope actually reached between health rows.
// e_max well under thr all night says the gate was too high; e_max grazing thr says it was tuned
// about right and the night was genuinely still. Without this the run cannot be told apart from
// a dead microphone.
static float env_e_max_win = 0.0f;
// Ambient must keep being learned while DISARMED, or the gate deadlocks. Confining the update to
// the armed branch means a noise floor that rises above thr can never be learned, so e can never
// fall below thr*REARM, so the gate never re-arms. Measured outdoors on this node: 156 s solid
// disarmed, envelope 1400-1600 against thr 800, ambient frozen at 73.2, two detections all night
// -- both from before it locked. A rising floor must move the floor estimate even when it is loud,
// just slowly enough that a millisecond-long shockwave does not desensitise the node to itself.
static const float ALPHA_UP = 1.0f / 200000.0f;    // ~12.5 s at 16 kHz, vs 0.625 s for ALPHA
// And a watchdog under that, because a gate that has gone deaf looks exactly like a quiet night.
// 30 s is far longer than any real event and far shorter than a night.
static const uint32_t REARM_MAX_SAMPLES = 30u * FS_NOMINAL;
static uint32_t disarm_samples = 0, gate_forced = 0;
// The PDM mic sits on a large positive DC pedestal. Measured on this node: mean(s) = 1285.8
// against mean(|s|) = 1285.3 -- the same number, which can only happen if the waveform never
// crosses zero. So the envelope detector was measuring the offset rather than any sound, and
// pinned the threshold at 8 x 1285 = 10280: a real event had to swing 9000 counts (27% of full
// scale) before it could register. Every sample is DC-blocked before anything looks at it.
// One pole at ~1.6 Hz -- two decades below the acoustic band, so no transient is reshaped, and
// it still settles within a fraction of a second at boot.
static const float ALPHA_DC = 1.0f / 1600.0f;      // tau ~0.1 s at 16 kHz
static float    sig_dc = 0.0f;                     // the pedestal being subtracted
static bool     dc_ready = false;
static float gate_thr() { float t = g_amb * RATIO; return t < g_floor ? g_floor : t; }
static int gate(int16_t s) {
  float a = fabsf((float)s);
  env_sum += a - env_buf[env_i]; env_buf[env_i] = a;
  if (++env_i >= 16) env_i = 0;
  float e = env_sum * ENV_INV;
  if (e > env_e_max_win) env_e_max_win = e;
  // g_floor, not a second literal: this used to recompute the threshold with FLOOR while
  // gate_thr() above computed its own, so a runtime floor changed at one site only would have
  // left /status and health.csv reporting a threshold the gate was not using -- a record that
  // looks correct and is not. Both sites read the same variable now.
  float thr = g_amb * RATIO; if (thr < g_floor) thr = g_floor;
  // Track the floor unconditionally: fast while below threshold, slow while above it. The slow
  // limb is what breaks the deadlock, and it is slow enough that a real transient is over long
  // before it shifts the estimate.
  g_amb += (e <= thr ? ALPHA : ALPHA_UP) * (e - g_amb);
  if (!armed) {
    if (e < thr * REARM) { armed = 1; disarm_samples = 0; }
    else if (++disarm_samples > REARM_MAX_SAMPLES) { armed = 1; disarm_samples = 0; gate_forced++; }
    return 0;
  }
  disarm_samples = 0;
  if (e <= thr) return 0;
  armed = 0; return 1;
}

// clip_st: what happened to this detection's WAV. PENDING is the only transient value, and
// det_flush refuses to write a row while it is set -- see the clip writer for why the row has to
// wait, and for the deadline that guarantees it stops waiting.
enum { CLIP_PENDING = 0, CLIP_OK, CLIP_BUDGET, CLIP_CARDFULL, CLIP_DEDUPE, CLIP_RING,
       CLIP_NOCARD, CLIP_FAIL, CLIP_STALLED };
struct Det { uint32_t sample; uint32_t pps_n; int32_t us_since_pps; int64_t utc_us;
              int16_t trigger; uint16_t flags; uint32_t uptime_s; double fs_at;
              uint8_t clip_st;
              uint8_t sk_st;          // 0 = the sketch is still waiting for post-onset audio
              uint8_t frame[MEL16_FRAME_BYTES]; };
// A RING. dets[] used to be a hard cap -- past the 64th, a detection incremented the counter and
// stored nothing, so a windy night reported hundreds of events and kept the first 64. det_n is
// the monotonic total; the slot is det_n % MAXDET.
static Det dets[MAXDET];
static uint32_t det_n = 0;        // total ever detected
static uint32_t det_flushed = 0;  // total written to the card
static uint32_t det_lost = 0;     // overwritten in the ring before they could be written
// The card the node is on exposes a 40 MB FAT partition, ~20 MB of it free. That is ~50k
// detections -- ample for a night, but not infinite, and a full card fails by returning a short
// write, not by raising anything. Counting failures is what stops a card that filled at 03:00
// from looking exactly like a night that went quiet at 03:00.
static uint32_t det_write_fail = 0;
// us_since_pps is SIGNED: a sample captured a few hundred us before an edge is back-dated across
// it, and belongs to the previous second. Reporting that as a huge unsigned number would put the
// event 999 ms from where it happened -- 343 m.

// ---------------------------------------------------------------- state
static I2SClass i2s;
static WebServer http(80);
static bool sd_ok = false, sta_ok = false;
static int sd_cs = 0;
static uint32_t boot_ms = 0;

// ---------------------------------------------------------------- OTA + failback
// Failback does NOT rely on the bootloader's rollback feature: this core ships a prebuilt
// bootloader and I could not confirm CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE is set, so depending on
// it would be depending on something unverified. Instead the app counts its own boots in RTC
// memory, which survives a reset. Three boots without reaching healthy and it flips the boot
// partition back itself.
//
// ⚠️What this CANNOT save you from: a build that faults before setup() runs -- a bad global
// constructor, say -- because nothing then increments the counter. That case still needs USB.
// The counter is incremented as the first statement of setup() to shrink that window to almost
// nothing.
#define BOOT_MAGIC 0xB0074A11UL
#define BOOT_MAX_TRIES 3
#define HEALTHY_AFTER_MS 30000
#define UNHEALTHY_REBOOT_MS 90000     // boots fine but never becomes reachable -> force a reboot
#ifndef BUILD_TAG
#define BUILD_TAG "A"
#endif
RTC_NOINIT_ATTR static uint32_t boot_magic;
RTC_NOINIT_ATTR static uint32_t boot_try;
RTC_NOINIT_ATTR static uint32_t proven_ok;   // this image reached healthy at least once
static bool marked_healthy = false;
static char ota_msg[96] = "idle";

// ---------------------------------------------------------------- log ring
// Every diagnosis tonight -- the 230400/UBX baud scan, the driven-vs-floating pin probes, the I2C
// scan -- came out of the boot log, which only existed on the USB cable. Once the node is carried
// somewhere there is no cable, so the log has to be readable over the link that remains.
#define LOGBUF 6144
static char logbuf[LOGBUF];
static volatile size_t log_w = 0;
static volatile bool log_wrapped = false;

static void log_put(const char *s, size_t n) {
  for (size_t i = 0; i < n; i++) {
    logbuf[log_w] = s[i];
    log_w = (log_w + 1) % LOGBUF;
    if (log_w == 0) log_wrapped = true;
  }
}
static void logf(const char *fmt, ...) {
  char b[256]; va_list ap; va_start(ap, fmt);
  int n = vsnprintf(b, sizeof b, fmt, ap); va_end(ap);
  if (n < 0) return;
  if (n > (int)sizeof b - 1) n = sizeof b - 1;
  Serial.write((const uint8_t *)b, n); log_put(b, n);
}
static void logln(const char *s) { logf("%s\n", s); }
static void logln(const String &s) { logf("%s\n", s.c_str()); }

static void boot_guard() {
  if (boot_magic != BOOT_MAGIC) { boot_magic = BOOT_MAGIC; boot_try = 0; proven_ok = 0; }
  boot_try++;
  // ⚠️Only ever revert an UNPROVEN image. Once this build has reached healthy, being unreachable
  // means the node moved, the AP changed, or the weather did -- not that the firmware is bad.
  // Without this, carrying the node out of WiFi range for three boots would roll back a working
  // image, which is the failback doing real harm in the name of safety.
  if (proven_ok) { boot_try = 0; return; }
  if (boot_try > BOOT_MAX_TRIES) {
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    boot_try = 0;
    if (other && esp_ota_set_boot_partition(other) == ESP_OK) {
      logf("\nBOOT GUARD: %d boots without reaching healthy -- reverting to %s\n",
                    BOOT_MAX_TRIES, other->label);
      Serial.flush(); delay(200); esp_restart();
    }
  }
}

static void mark_healthy_once() {
  // An image that runs happily but never joins WiFi cannot be recovered over the air and will
  // never reboot on its own, so the boot counter never advances. Force it: unreachable for long
  // enough is a failed boot, and three of those revert the partition.
  if (!marked_healthy && !proven_ok && millis() - boot_ms > UNHEALTHY_REBOOT_MS) {
    logln("boot  never became reachable -- rebooting so the failback counter advances");
    Serial.flush(); delay(200); esp_restart();
  }
  if (marked_healthy || millis() - boot_ms < HEALTHY_AFTER_MS) return;
  if (!sta_ok) return;                       // healthy MUST include "reachable", or a node that
  marked_healthy = true;                     // boots into a corner cannot be recovered over the air
  boot_try = 0; proven_ok = 1;               // this image has earned the benefit of the doubt
  esp_ota_mark_app_valid_cancel_rollback();  // harmless if the bootloader ignores it
  logln("boot  marked healthy; failback counter cleared");
}
static float env_peak_seen = 0;

// ppm error of the ESP32's own oscillator against GPS. esp_timer should advance exactly 1e6 us
// between edges; whatever it actually does is the crystal error, and every I2S rate on this part
// is derived from that same crystal. Works with no microphone attached.
// Local esp_timer microseconds -> UTC microseconds. Returns false when the anchor is not trusted.
static bool local_to_utc(uint64_t local_us, int64_t *utc_us) {
  if (!time_valid || !edge_unix_us) return false;
  *utc_us = edge_unix_us + (int64_t)(local_us - edge_local_us);
  return true;
}

static double esp_clock_ppm(uint32_t *secs_out) {
  if (pps_count < 3) return 0.0;
  uint32_t n = pps_count - 1;                       // intervals between first and last edge
  double us = (double)(pps_us_last - pps_us_first);
  if (secs_out) *secs_out = n;
  return (us / (double)n / 1e6 - 1.0) * 1e6;
}

// ---------------------------------------------------------------- raw ring (PSRAM)
// The sketch is 44 ms of log-mel and only exists when the gate fires. That is enough to say what
// a transient sounded like to a classifier built beforehand, and useless for anything else: the
// run produced 48 in-run detections and, until the clip writer below, no way to listen to any of
// them. 48 is measured -- dets.csv from the 2026-09-07 capture holds 62 rows, 14 of them at
// uptime_s == 12 (the first seconds of a boot, before the mic has settled) and 48 above it. This comment
// used to say 45 with the counter "ending on 47"; neither reproduces from the capture and both
// are deleted rather than adjusted. (The file holds 62 rows against a final det_n of 50 because
// dets.csv survives a reboot and det_n does not: 48 in-run plus that boot's own 2 startup
// triggers is exactly 50, and the other 12 are two apiece from six earlier boots. g_samples
// resets too, which is why two sample values repeat across boots with different triggers.)
// This ring keeps the last few minutes of actual PCM so a detection can be heard, or re-analysed
// off-box with a feature the node has never been taught.
//
// 240 s x 16000 Hz x 2 B = 7 680 000 B = 7.68 MB, against the 8.34 MB of PSRAM this board
// reported free at runtime. It fits -- but ps_malloc needs one CONTIGUOUS block and total-free is
// not largest-free, so ask for progressively less rather than fail outright, and treat failure as
// a missing feature rather than an error: praw == NULL disables /audio and changes nothing else.
static int16_t  *praw = NULL;
static uint32_t  praw_cap = 0;              // samples the ring holds; 0 = not allocated
static uint32_t  praw_want_s = 0;           // the span that actually got allocated, for the log

// The ring is contiguous in WRITE order, not in time. A lost block leaves no hole in it, and the
// night lost 18 seconds of 40791 (42749 samples), so reading it back at a flat rate would be
// wrong by up to that much. Anchor it the way everything else here is anchored instead: one
// (UTC, sample) pair per GPS second, which stays exact across a drop.
#define PRAW_MARKS 300                      // >= the longest ring ever allocated, in seconds
struct RawMark { int64_t utc_us; uint32_t sample; };
static RawMark  praw_mark[PRAW_MARKS];
static uint32_t praw_mark_n = 0;            // total ever recorded; slot is n % PRAW_MARKS

static uint32_t praw_oldest() {             // oldest sample index the ring still holds
  uint32_t now = g_samples;
  return (praw_cap && now > praw_cap) ? now - praw_cap : 0;
}
static uint32_t praw_mark_first() { return praw_mark_n > PRAW_MARKS ? praw_mark_n - PRAW_MARKS : 0; }

// What a mark is worth. Its sample index is interpolated from the last completed block to the
// edge and clamped to one block, so in steady running it is good to about a sample; if the reader
// stalls past a block the clamp caps the error at BLOCK = 256 samples = 15.9 ms, which is the
// error the un-interpolated version carried ALL the time. Between marks we extrapolate from the
// nearest one and claim nothing, because nothing has been measured there. None of this has been
// checked against an external reference -- the node has no second clock to check it with -- so
// treat these as bounds on the arithmetic, not as a measured accuracy.
static bool sample_to_utc(uint32_t s, int64_t *utc) {
  if (!praw_mark_n) return false;
  uint32_t first = praw_mark_first();
  const RawMark *best = &praw_mark[first % PRAW_MARKS];
  for (uint32_t k = first; k < praw_mark_n; k++) {
    const RawMark *m = &praw_mark[k % PRAW_MARKS];
    if ((int32_t)(m->sample - s) > 0) break;
    best = m;
  }
  double fsu = fs_clean > 1000.0 ? fs_clean : (double)FS_NOMINAL;
  *utc = best->utc_us + (int64_t)llrint((double)(int32_t)(s - best->sample) * 1e6 / fsu);
  return true;
}
static bool utc_to_sample(int64_t utc, uint32_t *s) {
  if (!praw_mark_n) return false;
  uint32_t first = praw_mark_first();
  const RawMark *best = &praw_mark[first % PRAW_MARKS];
  for (uint32_t k = first; k < praw_mark_n; k++) {
    const RawMark *m = &praw_mark[k % PRAW_MARKS];
    if (m->utc_us > utc) break;
    best = m;
  }
  double fsu = fs_clean > 1000.0 ? fs_clean : (double)FS_NOMINAL;
  int64_t v = (int64_t)best->sample + (int64_t)llrint((double)(utc - best->utc_us) * fsu / 1e6);
  if (v < 0) return false;
  *s = (uint32_t)v;
  return true;
}

// ---------------------------------------------------------------- scene feature
// The detection sketch is an IMPULSE descriptor: 8 frames at hop 64 = 704 samples = 44 ms, and it
// is gated. Nothing in 11.33 h of running described the BACKGROUND, which is what separates a
// chorus from a road. This is the scene-scale counterpart -- the node's analogue of the 0.96 s
// patch hugbot's YAMNet consumes -- and it runs whether or not anything triggers.
//
// ⚠️IT NO LONGER SHARES THE DETECTION SKETCH'S FILTERBANK. It used to, and the comment here used
// to say so. Two measurements moved it:
//
//   1. Every in-run detection peaked in the BOTTOM mel band and none of them peaked anywhere
//      else. Recipe: decode frame_hex from the 48 rows of the 2026-09-07 capture's dets.csv that
//      have uptime_s>12 and utc_us!=0, un-quantise to dB, take each band's peak over the 8
//      frames. argmax == band 0 in 48 of 48; band 0 > band 1 in 48 of 48, median gap 5.5 dB. A
//      feature whose extreme band is always the winner is reporting that the spectrum is still
//      climbing where the filterbank stops, not that it has found the peak.
//   2. A 10 s pull off this node's own ring, energy relative to the total:
//        2-62 Hz -6.2 dB | 62-312 Hz -1.9 dB | 312-500 Hz -11.9 dB | 500-1k -16.2 dB
//        1-2k -22.3 dB | 2-4k -27.8 dB | 4-8k -26.3 dB
//      62-312 Hz carries +8.2 dB MORE than the whole 312-8000 Hz span the sketch can see.
//
// MEL16_FB_LO[0] is 5, so the detection bank's band 0 starts at FFT bin 5 = 312.5 Hz and
// everything below is thrown away after the DC block has already paid for it. The scene bank
// (mel_scene.h, from firmware/gen_mel_scene.py) starts at bin 1 instead: band 0 is bins 1-4 =
// 62.5-250.0 Hz, and the top band still ends at bin 125 = 7812.5 Hz. No band is empty; the
// scene bank has 233 nonzero weights against the shipped MEL16_FB_W[245].
//
// ⚠️THE TWO BANKS SHARE NO BAND. Scene band 2 is bins 5-8 and detection band 0 is bins 5-10 --
// same first bin, different support -- so they overlap without being the same band, and no
// (FB_LO, FB_N) pair of one bank equals any pair of the other. Band k on a scene row and band k
// on a sketch frame are DIFFERENT FREQUENCIES and must never be stacked; tests/
// test_firmware_mel_scene.py asserts both banks and their disjointness against the headers.
//
// The DETECTION bank is untouched, and must stay untouched: firmware/hear_poc checks it byte-
// exact against golden vectors and hear/wire.py profile 0 IS the 20x8 f_lo=300 shape, so moving
// it would silently reinterpret every frame already on the wire. The scene descriptor has no wire
// profile and no model behind it, which is the whole reason this is the half that gets to move.
//
// The WINDOW is still shared: np.hanning(nfft) depends on nfft alone, so MEL16_WIN is bit-for-bit
// what a scene-only generator would emit, and duplicating it would cost 1024 B of flash for a
// second copy of the same numbers. The static_assert below is what makes that reuse checkable
// rather than assumed. The measured +10.6 ppm rate error (16000.169 Hz) is far inside one 62.5 Hz
// bin of a 256-point FFT, so tables built for 16000.0 stay correct for both banks.
//
// BLOCK is 256 and MEL16_NFFT is 256, so one I2S block IS one FFT frame. That is deliberate: it
// lets the descriptor be built 16 ms at a time at a steady 62.5 FFT/s instead of a 64-FFT burst
// once a second, which would have to finish inside the I2S DMA's 6 x 240 frames = 90 ms of
// headroom or drop audio. Cost is measured, not assumed -- see scene_fft_us_last and /status.
#define SCENE_SLICES           4
#define SCENE_FRAMES_PER_SLICE 16
#define SCENE_FRAMES (SCENE_SLICES * SCENE_FRAMES_PER_SLICE)   // 64 x 256 = 16384 samples
static_assert(BLOCK == MEL16_NFFT, "one I2S block must be exactly one scene FFT frame");
// The window is shared with the detection bank; the filterbank is not. If the two NFFTs ever
// diverge, MEL16_WIN stops being the right window for MELS_FB_* and the reuse becomes silent
// nonsense rather than a build error.
static_assert(MELS_NFFT == MEL16_NFFT, "scene bank and shared window must be the same NFFT");
// Card arithmetic, because this writes continuously to a 40 MB partition with 19 MB free:
// 20 bands x 4 slices = 80 B of mel, hex-encoded to 160 chars, plus ~66 chars of columns and the
// newline = ~227 B a row. 16384 samples is 1.024 s, so 12 h is 42188 rows = 9.6 MB. That fits
// alongside health.csv (1360 rows x ~200 B = 0.3 MB per 12 h) and dets.csv, and still leaves the
// card about half empty. A finer slice would not: 8 slices would be 16 MB and would not fit.
// Band COUNT is held at MEL16_BANDS for exactly that reason -- the span moved, the row size did
// not, so the arithmetic above still describes the file being written.
static_assert(MELS_BANDS == MEL16_BANDS, "scene row size arithmetic above assumes equal band counts");
// The two f_lo/f_hi columns are the schema bump. They are appended AFTER mel_hex rather than
// inserted next to bands/slices, because tools/hear_bridge.py checks the header as a PREFIX
// (head[:len(columns)]) and documents trailing columns as the supported way to add one -- put
// them in the middle and every existing reader breaks instead of carrying them. They also make
// the file self-describing: a scene.csv on a card no longer needs the firmware version to say
// what its band 0 covered.
static const char SCENE_HDR[] =
  "node,utc_us,uptime_s,sample,bands,slices,span_ms,ref_db4,frames,fft_us,mel_hex,f_lo_hz,f_hi_hz";
static float    scene_acc[MELS_BANDS];               // power summed over the slice in progress
static float    scene_db[MELS_BANDS * SCENE_SLICES]; // band-major, as sketch_frame's db[]
static uint32_t scene_frame_i = 0, scene_slice_i = 0;
// Where the row in progress started. NOT derived from g_samples at emit time: a short I2S read is
// skipped rather than padded, so the 64 frames of a row are not guaranteed to be 16384 contiguous
// samples. Recording the index when the row opens keeps the row honest across that.
static uint32_t scene_start = 0;
static uint32_t scene_rows = 0;             // rows produced, card or no card
static uint32_t scene_written = 0;          // rows that actually reached it. det_n/det_written
                                            // exist for the same reason (commit 812ab4c): a row
                                            // counted on intent makes a dead card read as healthy.
static uint32_t scene_short_blocks = 0;     // I2S reads that came up short of a whole frame
static uint32_t scene_write_fail = 0;
static uint32_t scene_fft_us = 0;           // accumulating over the row in progress
static uint32_t scene_fft_us_last = 0;      // us of FFT+filterbank per 1.024 s row, MEASURED
static uint32_t scene_fft_us_max = 0;
static File     scenef;

// ---------------------------------------------------------------- append-CSV, header guaranteed
// Every durable record this node keeps is an append-only CSV whose first line must be its header,
// and there are three ways that has actually failed here:
//
//   1. The file does not exist yet. Obvious, and the only one the original code handled.
//   2. It exists and is EMPTY. size()==0 catches this; SD.exists() alone does not, and a fix
//      written with exists() alone reintroduces exactly the headerless file it set out to remove.
//   3. It exists with a DIFFERENT header, or -- as happened over the 11.33 h run -- with no header
//      at all. Appending to that makes every row in the file ambiguous, so the old file is rolled
//      aside instead. This is also what repairs a headerless file: its first line does not match,
//      so it is preserved under <name>-prev.csv and a correct one is started.
//
// Why size() is not used to decide (2) before opening: FS::size() returns VFSFileImpl::_stat
// .st_size, filled by a stat() run BEFORE the open (core 3.0.5, vfs_api.cpp:274). On a file the
// open CREATES that stat fails, _stat is left uninitialised, and size() returns heap garbage --
// deterministic per build, which is how both CSVs ran a whole night with no header. So existence
// is tested first, with exists(), and size() is consulted only on a file already known to be there.
// ---- daily files, oldest rolled off ---------------------------------------------------------
// scene.csv was ONE growing file. At the measured 335 B/row and one row per 1.024 s that is
// 1.18 MB/h -- 10 GB a year in a single CSV, where one bad write costs the lot and nothing ever
// bounds it. Fine for a night; not for a fleet that runs continuously.
//
// So each stream writes /scene-YYYYMMDD.csv, and when the card runs low the OLDEST day goes. The
// data is on the card if it is wanted and it is not kept by default, which is the trade asked for.
//
// ⚠️THE DATE COMES FROM THE PPS-DISCIPLINED UTC, NOT FROM millis(). A node with no fix yet has no
// date, and writing one anyway would put a day's rows under whatever the clock guessed at boot.
// Until the anchor is valid, rows go to -00000000, which is a real file that gets rolled off like
// any other rather than a gap nobody can account for.
#define KEEP_FREE_MB 64u        // stop pruning here: a full card fails writes, and a node that
                                // cannot write is worse than one missing last week.

static void utc_yyyymmdd(int64_t utc_us, char *out, size_t n) {
  if (utc_us <= 0) { snprintf(out, n, "00000000"); return; }
  // days from civil, inverted. Same algorithm as the NAV-PVT path, run backwards -- no time.h.
  long long z = utc_us / 1000000LL / 86400LL + 719468LL;
  long long era = (z >= 0 ? z : z - 146096) / 146097;
  unsigned long doe = (unsigned long)(z - era * 146097);
  unsigned long yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  long long y = (long long)yoe + era * 400;
  unsigned long doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  unsigned long mp = (5 * doy + 2) / 153;
  unsigned long d = doy - (153 * mp + 2) / 5 + 1;
  unsigned long m = mp + (mp < 10 ? 3 : -9);
  y += (m <= 2);
  snprintf(out, n, "%04lld%02lu%02lu", y, m, d);
}

// Delete the oldest <stem>-YYYYMMDD.csv until the card has KEEP_FREE_MB again. Returns how many
// went. Oldest is by NAME, which sorts chronologically because the name is a date -- no stat call
// per file, and no dependence on FAT timestamps, which a node with no RTC writes wrong anyway.
static int prune_oldest(const char *stem, const char *keep_name) {
  int gone = 0;
  for (int guard = 0; guard < 8; guard++) {
    if ((uint32_t)((SD.totalBytes() - SD.usedBytes()) / 1048576ULL) >= KEEP_FREE_MB) break;
    char oldest[40]; oldest[0] = 0;
    File d = SD.open("/");
    for (File e = d.openNextFile(); e; e = d.openNextFile()) {
      const char *nm = e.name();
      if (!e.isDirectory() && strncmp(nm, stem, strlen(stem)) == 0 &&
          strcmp(nm, keep_name) != 0 && (!oldest[0] || strcmp(nm, oldest) < 0))
        snprintf(oldest, sizeof oldest, "%s", nm);
      e.close();
    }
    d.close();
    // The pre-rotation file is not named scene-YYYYMMDD.csv, so the loop above will never pick
    // it -- 20.7 MB stranded on nyquist, uncollectable, for as long as the node lives. Take it
    // once there is a dated file to take it in favour of.
    if (!oldest[0]) {
      char legacy[24]; snprintf(legacy, sizeof legacy, "/%.*s.csv", (int)(strlen(stem) - 1), stem);
      if (SD.exists(legacy) && SD.remove(legacy)) {
        logf("sd    pruned legacy %s (superseded by daily files)\n", legacy);
        gone++;
        continue;
      }
      break;                                   // nothing left to give
    }
    char path[48]; snprintf(path, sizeof path, "/%s", oldest);
    if (!SD.remove(path)) { logf("sd    could NOT remove %s\n", path); break; }
    logf("sd    pruned %s to free space\n", path);
    gone++;
  }
  return gone;
}

static File csv_open(const char *path, const char *prev, const char *hdr) {
  bool fresh = !SD.exists(path);
  if (!fresh) {
    File r = SD.open(path, FILE_READ);
    if (r) {
      String first = r.readStringUntil('\n'); r.close(); first.trim();
      if (first.length() == 0) {
        fresh = true;                       // empty: just write the header, nothing to preserve
      } else if (first != String(hdr)) {
        SD.remove(prev);
        // An unchecked rename appends new-schema rows under the old header -- the exact ambiguity
        // the roll exists to prevent. Say so rather than corrupt quietly.
        if (SD.rename(path, prev)) { fresh = true; logf("sd    rolled %s -> %s (header changed)\n", path, prev); }
        else logf("sd    could NOT roll %s aside -- this boot's rows land under the old header\n", path);
      }
    }
  }
  File f = SD.open(path, FILE_APPEND);
  if (f && (fresh || f.size() == 0)) f.println(hdr);
  return f;
}

static void scene_emit() {
  scene_fft_us_last = scene_fft_us;
  if (scene_fft_us > scene_fft_us_max) scene_fft_us_max = scene_fft_us;
  scene_fft_us = 0;
  scene_rows++;
  uint32_t start = scene_start;
  int64_t utc = 0;
  sample_to_utc(start, &utc);               // 0 = no PPS anchor yet, same convention as dets.csv
  float ref = scene_db[0];
  for (int i = 1; i < MELS_BANDS * SCENE_SLICES; i++) if (scene_db[i] > ref) ref = scene_db[i];
  if (!sd_ok) return;
  // One file per UTC day. Reopened when the date rolls, so a capture spanning midnight lands in
  // two files rather than one that has to be split later by whoever reads it.
  {
    static char cur_day[12] = "";
    char day[12]; utc_yyyymmdd(utc, day, sizeof day);
    if (strcmp(day, cur_day) != 0) {
      if (scenef) { scenef.close(); }
      snprintf(cur_day, sizeof cur_day, "%s", day);
      char path[36], prev[40];
      snprintf(path, sizeof path, "/scene-%s.csv", day);
      snprintf(prev, sizeof prev, "/scene-%s-prev.csv", day);
      // Prune BEFORE opening the new day, so the space is there to write into. The file about to
      // be written is named so it cannot prune itself.
      prune_oldest("scene-", path + 1);
      scenef = csv_open(path, prev, SCENE_HDR);
      logf("sd    scene -> %s\n", path);
    }
    if (!scenef) return;
  }
  char line[MELS_BANDS * SCENE_SLICES * 2 + 192];
  int m = snprintf(line, sizeof line, "%s,%lld,%lu,%lu,%d,%d,%d,%d,%d,%lu,",
                   node_id, (long long)utc, (unsigned long)((millis() - boot_ms) / 1000),
                   (unsigned long)start, MELS_BANDS, SCENE_SLICES,
                   (int)((uint32_t)SCENE_FRAMES * MELS_NFFT * 1000u / FS_NOMINAL),
                   (int)lrintf(ref * 4.0f), SCENE_FRAMES, (unsigned long)scene_fft_us_last);
  static const char hx[] = "0123456789abcdef";
  for (int i = 0; i < MELS_BANDS * SCENE_SLICES && m < (int)sizeof line - 24; i++) {
    float v = roundf((scene_db[i] - ref) * 2.0f);      // 0.5 dB steps, as the detection sketch
    uint8_t q = (uint8_t)(int8_t)(v < -128 ? -128 : (v > 127 ? 127 : v));
    line[m++] = hx[q >> 4]; line[m++] = hx[q & 0xF];
  }
  int add = snprintf(line + m, sizeof line - m, ",%.1f,%.1f", MELS_F_LO, MELS_F_HI);
  if (add > 0) m += (add < (int)sizeof line - m) ? add : ((int)sizeof line - m - 1);
  line[m++] = '\n';
  // Short write closes the handle so the next row reopens, exactly as det_flush does: leaving it
  // open turns a transient card error into a permanent silent stop.
  if (scenef.write((const uint8_t *)line, m) != (size_t)m) { scene_write_fail++; scenef.close(); }
  else scene_written++;
}

// One 256-sample frame into the slice accumulator. Shares fft_re/fft_im with sketch_frame, which
// is safe only because both are called from the loop task and never concurrently -- putting
// either on a second FreeRTOS task would need its own buffers.
static void scene_frame(const int16_t *s) {
  if (!scene_frame_i && !scene_slice_i) scene_start = g_samples - MELS_NFFT;
  uint32_t t0 = (uint32_t)esp_timer_get_time();
  // MEL16_WIN, not a scene copy: same nfft, same np.hanning, checked by the static_assert above.
  for (int i = 0; i < MELS_NFFT; i++) {
    fft_re[i] = (float)s[i] * MEL16_WIN[i]; fft_im[i] = 0.0f;
  }
  fft256();
  const float *w = MELS_FB_W;
  for (int b = 0; b < MELS_BANDS; b++) {
    int lo = MELS_FB_LO[b], cnt = MELS_FB_N[b];
    float acc = 0.0f;
    for (int i = 0; i < cnt; i++) {
      int bin = lo + i; acc += w[i] * (fft_re[bin] * fft_re[bin] + fft_im[bin] * fft_im[bin]);
    }
    w += cnt;
    scene_acc[b] += acc;
  }
  scene_fft_us += (uint32_t)esp_timer_get_time() - t0;
  if (++scene_frame_i < SCENE_FRAMES_PER_SLICE) return;
  scene_frame_i = 0;
  // Mean power over the slice, then dB. Averaging power and not dB, so a single loud frame does
  // not dominate the slice through the log.
  for (int b = 0; b < MELS_BANDS; b++) {
    scene_db[b * SCENE_SLICES + scene_slice_i] =
      10.0f * log10f(scene_acc[b] / (float)SCENE_FRAMES_PER_SLICE + 1e-12f);
    scene_acc[b] = 0.0f;
  }
  if (++scene_slice_i < SCENE_SLICES) return;
  scene_slice_i = 0;
  scene_emit();
}

// ---------------------------------------------------------------- /audio limits and WAV
// Serving the whole ring is 7.68 MB, and WiFi on this node measured 335 kB/s, so that is ~23 s
// inside one handler. The I2S DMA holds 6 x 240 frames = 90 ms, so a handler that does not drain
// it would throw away more audio than the entire night lost (18 s of 40791). Hence: bounded
// requests, and the loop's own audio path pumped between chunks -- the same reason /tp pumps
// Serial1 rather than delay()ing.
#define AUDIO_MAX_S   30
// 4096 B is 12 ms of link time at the measured 335 kB/s, against the 16 ms of audio that one
// pumped block covers, so the DMA drains faster than it fills. The pump then paces the loop at one
// block per iteration, which is 62.5 x 4096 = 256 kB/s -- arithmetic from the block rate, not a
// throughput anyone has measured on this endpoint yet.
#define AUDIO_CHUNK_B 4096
// ESP_I2S.cpp ships dma_desc_num 6 x dma_frame_num 240 = 1440 samples = 90 ms, so six BLOCKs is
// everything the peripheral can be holding. Asking for more would block on audio that does not
// exist yet.
#define AUDIO_PUMP_MAX 6
// The writer does not stop while the response is sent, so refuse to serve the oldest part of the
// ring. By that arithmetic a 30 s request takes about 4 s, in which the head advances 4 s of
// samples; 16 s covers it even if the link turns out four times slower than the 335 kB/s measured.
#define AUDIO_GUARD_S 16

// Canonical 44-byte PCM WAV header. The rate field is an integer and cannot carry the measured
// 16000.169 Hz, so the exact figure goes out in X-Audio-Fs-Hz instead; anything doing timing work
// must use that and not what the WAV claims.
static void wav_header(uint8_t *h, uint32_t data_bytes, uint32_t fs) {
  uint32_t riff = 36 + data_bytes, brate = fs * 2;
  memcpy(h, "RIFF", 4);
  h[4] = riff; h[5] = riff >> 8; h[6] = riff >> 16; h[7] = riff >> 24;
  memcpy(h + 8, "WAVEfmt ", 8);
  h[16] = 16; h[17] = 0; h[18] = 0; h[19] = 0;      // fmt chunk length
  h[20] = 1;  h[21] = 0;                            // PCM
  h[22] = 1;  h[23] = 0;                            // mono
  h[24] = fs; h[25] = fs >> 8; h[26] = fs >> 16; h[27] = fs >> 24;
  h[28] = brate; h[29] = brate >> 8; h[30] = brate >> 16; h[31] = brate >> 24;
  h[32] = 2;  h[33] = 0;                            // block align
  h[34] = 16; h[35] = 0;                            // bits per sample
  memcpy(h + 36, "data", 4);
  h[40] = data_bytes; h[41] = data_bytes >> 8; h[42] = data_bytes >> 16; h[43] = data_bytes >> 24;
}

// ---------------------------------------------------------------- clips: a WAV per detection
// The PSRAM ring already holds 240 s of PCM and /audio can already serve any window of it. What
// it cannot do is outlive those 240 s: an event heard at 03:00 is gone by 03:04 unless somebody
// was awake and fetching. This writes a fixed-length WAV around each detection to the card, so
// the audio survives the night the same way dets.csv does.
//
// It addresses the ring BY SAMPLE, not by UTC. dets[].sample is a direct praw index (both are
// counted in g_samples), so unlike /audio -- which refuses outright with "the ring cannot be
// addressed by time" -- a clip still works for a detection stamped utc_us == 0. Three of the 62
// rows in the 2026-09-07 capture's dets.csv are exactly that.
//
// LENGTH: 1 s before the trigger, 3 s after. The post-roll is the long half because the events
// are longer than the descriptor: across the 8 frames of each in-run sketch the median energy
// varies only ~4 dB and the peak frame is spread over all 8 positions, so what fired the gate is
// not an impulse that has finished inside the 44 ms window.
#define CLIP_PRE_SAMPLES  ((uint32_t)FS_NOMINAL)          // 1.0 s
#define CLIP_POST_SAMPLES ((uint32_t)(3 * FS_NOMINAL))    // 3.0 s
#define CLIP_SAMPLES      (CLIP_PRE_SAMPLES + CLIP_POST_SAMPLES)   // 64000
#define CLIP_BYTES        (44u + CLIP_SAMPLES * 2u)       // 128044 B, header included
// Every written clip is exactly CLIP_BYTES. A clip whose window has fallen off either end of the
// ring is refused rather than shortened, which is what makes the budget arithmetic below exact
// instead of an estimate -- and what stops a caller believing it has audio it does not have, the
// same reason /audio sends X-Audio-Clipped.
//
// ONE CLIP PER EVENT, NOT PER TRIGGER. The capture's 48 in-run detections collapse to 33 distinct
// events at 1 s clustering -- 16 of the 47 gaps are under 1 s, ~1.5 triggers an event, the gate
// retriggering on a decay tail. Clipping per trigger would spend 1.5x the card for the same
// audio, and the second clip is a 4 s window that overlaps the first by 3 s.
#define CLIP_DEDUPE_SAMPLES ((uint32_t)FS_NOMINAL)        // 1.0 s, the clustering that gave 33
//
// BUDGET. Measured free space on this card is 19 MiB (sd_free_mb is a floor: (total-used)/1048576,
// and it read 19 for most of the run). Over a 12 h night the CSVs take, from row sizes measured
// on the capture's own files:
//     scene.csv  42188 rows x 227 B  = 9576676 B     (rows = 12 h / 1.024 s)
//     health.csv  1440 rows x 151.2 B =  217728 B    (219244 B / 1450 rows, measured)
//     dets.csv      51 rows x  437 B  =   22287 B    (403.1 B measured + 34 B of clip columns:
//                                                     ",<30 char path>,<2 char why>". The path
//                                                     is fixed width -- 8 hex of boot id and a
//                                                     %010lu sample -- so 437 is exact, not an
//                                                     estimate, and it is the worst case: a row
//                                                     with no clip is 411 B.)
//                                      ---------
//                                       9816691 B = 9.36 MiB, leaving 9.64 MiB
// At the measured event rate -- 33 events in 11.33 h = 2.91/h = 35 over 12 h -- clips cost
// 35 x 128044 = 4481540 B = 4.27 MiB, a 2.26x margin. The budget is set above that rather than at
// it, because the events are not spread evenly: 34 of the 48 triggers fall in the two hours
// 09:00-10:59, so a per-night average protects nothing. A byte budget does.
#define CLIP_BUDGET_B  6291456u   // 6 MiB = 49 clips = 1.4x the measured 12 h event count, and
                                  // leaves 3.64 MiB of the remainder for the CSVs to overrun into
// And a live floor under that, because the budget assumes the card started at 19 MiB free and
// nothing here can know that it did. 2 MiB is ~2.6 h of scene rows (227 B per 1.024 s = 221.7
// B/s), so the record keeps running for hours after clips stop.
#define CLIP_FREE_RESERVE_MB 2
// Chunk size, and the reason there is one: a 128044 B write inside loop() would stall the I2S
// reader far past the DMA's 6 x 240 frames = 90 ms and drop the audio this exists to keep. One
// 4096 B chunk per loop iteration instead -- loop() calls audio_pump() every pass, so the DMA is
// drained between chunks by the code that already does it, with no nested pump inside a card
// write. 4096 B is what /audio streams for the same reason. A whole clip is 32 chunks, so at the
// ~16 ms an I2S block takes it lands in about half a second. If the card is slower than that the
// cost is not hidden: it shows up in drop_s, which is the instrument for exactly this.
#define CLIP_CHUNK_B 4096
#define CLIP_DIR "/clips"
// Hard deadline on the PENDING state. det_flush will not write a detection's row until its clip
// resolves, so a clip that can never resolve would stall dets.csv -- the record mattering more
// than the audio, that must not be possible. 10 s is well past the 3 s post-roll and well short
// of the 30 s health interval.
#define CLIP_WAIT_MAX_S 10

static uint32_t clip_written = 0;        // advances ONLY after the full CLIP_BYTES landed
static uint32_t clip_skip_budget = 0;    // wanted, refused by CLIP_BUDGET_B -- working as designed
static uint32_t clip_skip_full = 0;      // wanted, refused because the CARD is nearly out
static uint32_t clip_skip_dedupe = 0;
static uint32_t clip_skip_ring = 0;      // window not in the ring, or no PSRAM ring at all
static uint32_t clip_nocard = 0;
static uint32_t clip_fail = 0;           // short write or failed open
static uint32_t clip_budget_left = CLIP_BUDGET_B;
static uint32_t sd_free_mb_last = 0;     // sampled on the 30 s health tick, not per clip:
                                         // SD.usedBytes() is a free-cluster walk on FATFS and its
                                         // cost on this card has not been measured.
static File     clipf;
static bool     clip_busy = false;
static uint32_t clip_k = 0, clip_at_sample = 0, clip_s = 0, clip_left = 0;
static uint32_t clip_last_sample = 0;
static bool     clip_have_last = false;
// Boot-unique filename prefix. NOT derived from utc_us (three of 62 capture rows have utc_us == 0
// and zeros collide) and not from det_n or sample alone (both restart at 0 every boot, so a
// second night would overwrite the first's clips). esp_random() is seeded by hardware entropy and
// needs no GPS fix, which a boot-time name must not wait for.
static char clip_boot[9] = "00000000";

static void clip_name(char *out, size_t n, uint32_t sample) {
  // node first: a clip is the one artefact that gets copied off the card and mailed around,
  // so it has to carry its own provenance rather than depend on the directory it sits in.
  snprintf(out, n, CLIP_DIR "/%s-%s-%010lu.wav", node_id, clip_boot, (unsigned long)sample);
}

// Delete the oldest clip in CLIP_DIR and return its bytes to the budget. Oldest is by NAME:
// clip_name() embeds the boot-unique prefix and a zero-padded sample index, so within a boot the
// name sorts chronologically. Across boots the prefix is random rather than ordered, so this can
// evict a newer clip from a previous boot -- accepted, because the alternative is a stat() per
// file on every eviction and the set is at most 49.
static bool clip_evict_oldest() {
  if (!sd_ok) return false;
  char oldest[64]; oldest[0] = 0;
  File d = SD.open(CLIP_DIR);
  if (!d) return false;
  for (File e = d.openNextFile(); e; e = d.openNextFile()) {
    if (!e.isDirectory() && (!oldest[0] || strcmp(e.name(), oldest) < 0))
      snprintf(oldest, sizeof oldest, "%s", e.name());
    e.close();
  }
  d.close();
  if (!oldest[0]) return false;
  char path[80]; snprintf(path, sizeof path, CLIP_DIR "/%s", oldest);
  if (!SD.remove(path)) { logf("clip  could NOT evict %s\n", path); return false; }
  clip_budget_left += CLIP_BYTES;
  logf("clip  evicted %s to make room\n", path);
  return true;
}

static const char *clip_why(uint8_t st) {
  switch (st) {
    case CLIP_OK:      return "ok";
    case CLIP_BUDGET:  return "budget";
    // Kept apart from "budget" on purpose. "budget" means this firmware refused to spend more
    // than CLIP_BUDGET_B and everything else is still being recorded as designed; "cardfull"
    // means the card is nearly out and health.csv and dets.csv are next. They want different
    // responses from whoever reads the file, so they must not share a token.
    case CLIP_CARDFULL: return "cardfull";
    case CLIP_DEDUPE:  return "dedupe";
    case CLIP_RING:    return "ring";
    case CLIP_NOCARD:  return "nocard";
    case CLIP_FAIL:    return "fail";
    case CLIP_STALLED: return "stalled";
    default:           return "pending";
  }
}

// One chunk of work, called once per loop() pass. Either finishes a chunk of the clip in flight
// or decides what to do with the oldest unresolved detection. Never blocks on audio that has not
// been captured yet: if the post-roll is not in the ring, it returns and is asked again.
static void clip_pump() {
  if (clip_busy) {
    uint32_t nsamp = clip_left > CLIP_CHUNK_B / 2 ? CLIP_CHUNK_B / 2 : clip_left;
    static uint8_t cbuf[CLIP_CHUNK_B];
    // The ring wraps mid-chunk; this is the same two-memcpy the /audio handler uses, and it is
    // the only place in this file that knows how praw wraps.
    uint32_t idx = clip_s % praw_cap;
    uint32_t run = praw_cap - idx; if (run > nsamp) run = nsamp;
    memcpy(cbuf, praw + idx, (size_t)run * 2);
    if (run < nsamp) memcpy(cbuf + run * 2, praw, (size_t)(nsamp - run) * 2);
    bool ok = clipf.write(cbuf, (size_t)nsamp * 2) == (size_t)nsamp * 2;
    clip_s += nsamp; clip_left -= nsamp;
    if (!ok || !clip_left) {
      clipf.close();
      clip_busy = false;
      Det &d = dets[clip_k % MAXDET];
      // The slot could in principle have been recycled under us -- 128 detections inside the half
      // second a clip takes. Check rather than stamp a state onto somebody else's detection.
      bool mine = (d.sample == clip_at_sample);
      if (ok) {
        clip_written++;                       // only here: the full CLIP_BYTES is on the card
        clip_budget_left -= CLIP_BYTES;
        clip_last_sample = clip_at_sample; clip_have_last = true;
        if (mine) d.clip_st = CLIP_OK;
      } else {
        clip_fail++;
        char p[48]; clip_name(p, sizeof p, clip_at_sample);
        SD.remove(p);                         // a truncated WAV is worse than no WAV
        if (mine) d.clip_st = CLIP_FAIL;
      }
    }
    return;
  }

  // Oldest unresolved detection still in the ring. Anything older than that has been overwritten
  // and det_flush counts it in det_lost; there is nothing left to clip.
  uint32_t first = det_flushed;
  if (det_n > MAXDET && det_n - MAXDET > first) first = det_n - MAXDET;
  uint32_t k = first;
  while (k < det_n && dets[k % MAXDET].clip_st != CLIP_PENDING) k++;
  if (k >= det_n) return;
  Det &d = dets[k % MAXDET];

  if (!sd_ok)                        { d.clip_st = CLIP_NOCARD; clip_nocard++; return; }
  if (!praw || !praw_cap)            { d.clip_st = CLIP_RING;   clip_skip_ring++; return; }
  if (clip_have_last && d.sample - clip_last_sample < CLIP_DEDUPE_SAMPLES)
                                     { d.clip_st = CLIP_DEDUPE; clip_skip_dedupe++; return; }
  // THE BUDGET IS A WINDOW, NOT AN ALLOWANCE. It used to be a per-boot counter that only ever
  // decremented, sized in its own comment as "1.4x the measured 12 h event count" -- correct for
  // one night, and wrong the moment the fleet runs continuously. Every node went permanently deaf
  // to audio after its first busy day and stayed that way until someone rebooted it. Measured:
  // mach reached it and served 128 consecutive detections reading clip_why "budget", which is why
  // there was no audio to cross-correlate against rankine when it was first asked for.
  //
  // Now the oldest clip is deleted to make room, so retention is bounded by the BUDGET rather than
  // by uptime, and a node that has been up for a month still carries its last 49 events. Failing
  // to free space still refuses the clip -- that path is a full card, and it is reported as
  // "budget" exactly as before rather than pretending the write happened.
  if (clip_budget_left < CLIP_BYTES && !clip_evict_oldest()) {
    d.clip_st = CLIP_BUDGET; clip_skip_budget++; return;
  }
  if (sd_free_mb_last < CLIP_FREE_RESERVE_MB)
                                     { d.clip_st = CLIP_CARDFULL; clip_skip_full++; return; }
  if (d.sample < CLIP_PRE_SAMPLES)   { d.clip_st = CLIP_RING;   clip_skip_ring++; return; }

  // Wait for the post-roll to exist. It does not yet at the instant the gate fires -- that is the
  // whole reason this is a deferred queue and not something audio_pump could do inline.
  uint32_t up = (millis() - boot_ms) / 1000;
  if ((int32_t)(g_samples - (d.sample + CLIP_POST_SAMPLES)) < 0) {
    if (up - d.uptime_s > CLIP_WAIT_MAX_S) { d.clip_st = CLIP_STALLED; clip_skip_ring++; }
    return;                                   // otherwise: not an error, just not yet
  }
  uint32_t start = d.sample - CLIP_PRE_SAMPLES;
  if ((int32_t)(start - praw_oldest()) < 0)   { d.clip_st = CLIP_RING; clip_skip_ring++; return; }

  char path[48]; clip_name(path, sizeof path, d.sample);
  File f = SD.open(path, FILE_WRITE);
  if (!f) { d.clip_st = CLIP_FAIL; clip_fail++; return; }
  uint8_t hdr[44];
  // The rate field is an integer and cannot carry the measured 16000.169 Hz, exactly as
  // wav_header says. There are no HTTP headers on a file, so the exact rate travels in dets.csv's
  // fs_hz column instead -- per detection, which is where it belongs anyway.
  double fsu = fs_clean > 1000.0 ? fs_clean : (double)FS_NOMINAL;
  wav_header(hdr, CLIP_SAMPLES * 2, (uint32_t)lrint(fsu));
  if (f.write(hdr, sizeof hdr) != sizeof hdr) {
    f.close(); SD.remove(path); d.clip_st = CLIP_FAIL; clip_fail++; return;
  }
  clipf = f; clip_busy = true; clip_k = k; clip_at_sample = d.sample;
  clip_s = start; clip_left = CLIP_SAMPLES;
}

// ---------------------------------------------------------------- gate floor: set and persist
// One line of decimal in /gate.cfg. Persisting it is what makes the knob useful on a node that
// gets power-cycled by a plug timer -- and also the only thing here that can carry a bad setting
// across a reboot, so the clamp is applied on LOAD as well as on set. A file written by hand with
// "0" in it comes back as FLOOR_MIN, not as 0.
#define GATE_CFG "/gate.cfg"

static float gate_floor_clamp(float v) {
  if (!(v == v)) return FLOOR_DEFAULT;                  // NaN: strtof on garbage
  if (v < FLOOR_MIN) return FLOOR_MIN;
  if (v > FLOOR_MAX) return FLOOR_MAX;
  return v;
}

static void gate_floor_load() {
  if (!sd_ok || !SD.exists(GATE_CFG)) return;
  File f = SD.open(GATE_CFG, FILE_READ);
  if (!f) return;
  String s = f.readStringUntil('\n'); f.close(); s.trim();
  if (!s.length()) return;
  float v = gate_floor_clamp(strtof(s.c_str(), NULL));
  g_floor = v; g_floor_src = "file"; g_floor_saved = v;
  logf("gate  floor %.0f from " GATE_CFG " (file said \"%s\")\n", g_floor, s.c_str());
}

// Everything the operator needs to not have to guess, in one place: the active value, where it
// came from, the bounds that could have altered it, and the live evidence (ambient, e_max, thr)
// for judging whether it is anywhere near right.
static String gate_json() {
  char b[416], saved[16];
  // JSON null, not 0 and not "none": there is no floor saved, which is a different fact from a
  // saved floor that happens to be small.
  if (g_floor_saved == g_floor_saved) snprintf(saved, sizeof saved, "%.1f", g_floor_saved);
  else                                snprintf(saved, sizeof saved, "null");
  snprintf(b, sizeof b,
    "{\"floor\":%.1f,\"floor_default\":%.1f,\"floor_min\":%.1f,\"floor_max\":%.1f,"
    "\"source\":\"%s\",\"floor_saved\":%s,\"ratio\":%.1f,\"rearm\":%.2f,"
    "\"thr\":%.1f,\"ambient\":%.1f,\"e_max_win\":%.1f,\"armed\":%d,\"forced_rearms\":%lu,"
    "\"usage\":\"POST /gate?floor=<n>[&persist=1] | POST /gate?reset=1\"}",
    g_floor, FLOOR_DEFAULT, FLOOR_MIN, FLOOR_MAX, g_floor_src,
    saved, RATIO, REARM,
    gate_thr(), g_amb, env_e_max_win, armed, (unsigned long)gate_forced);
  return String(b);
}

static bool gate_floor_persist() {
  if (!sd_ok) return false;
  File f = SD.open(GATE_CFG, FILE_WRITE);              // FILE_WRITE truncates: one value, no log
  if (!f) return false;
  f.printf("%.1f\n", g_floor);
  f.close();
  g_floor_saved = g_floor;
  return true;
}

static double measured_fs() {
  if (pps_count < 3) return 0.0;
  double secs = (double)(pps_us_last - pps_us_first) / 1e6;
  if (secs < 1.0) return 0.0;
  return (double)(pps_samp_last - pps_samp_first) / secs;
}

static String status_json() {
  double fs = measured_fs();
  int64_t utc_now = 0; uint64_t nowl = (uint64_t)esp_timer_get_time();
  bool tv = local_to_utc(nowl, &utc_now);
  uint64_t since_edge = pps_count ? (nowl - edge_local_us) : 0;
  (void)tv;
  uint32_t r_new = g_samples, r_old = praw_oldest();
  uint32_t r_held = praw_cap ? r_new - r_old : 0;
  int64_t  r_from = 0, r_to = 0;
  if (praw_cap) { sample_to_utc(r_old, &r_from); sample_to_utc(r_new, &r_to); }
  // static, not a stack frame: this grew from 2048 with the gate-floor and clips objects, and the
  // Arduino loop task has 8 kB of stack that the WebServer is already using. Only ever called
  // from the loop task (h_status), so there is no second caller to race it.
  static char b[3072];
  // JSON has no NaN. A node that does not know its temperature emits null, which every parser
  // reads as absent -- printing nan would be invalid JSON, and a downstream coercion of it to 0.0
  // would look like a freezing night rather than a missing sensor.
  char envs_t[16], envs_p[16], envs_c[16];
  #define ENVF(dst, v) do { if ((v) == (v)) snprintf(dst, sizeof dst, "%.2f", (double)(v)); \
                            else snprintf(dst, sizeof dst, "null"); } while (0)
  ENVF(envs_t, bmp_temp_c); ENVF(envs_p, bmp_press_hpa); ENVF(envs_c, sound_speed_mps());
  char floor_saved[16];
  if (g_floor_saved == g_floor_saved) snprintf(floor_saved, sizeof floor_saved, "%.1f", g_floor_saved);
  else                                snprintf(floor_saved, sizeof floor_saved, "null");
  snprintf(b, sizeof b,
    // fw is FIRST after the identity, because the question it answers -- is this node running
    // the same binary as its neighbours -- is asked of the whole fleet at once.
    "{\"node\":\"%s\",\"class\":\"%s\",\"fw\":\"%s\",\"uptime_s\":%lu,\"heap\":%lu,\"psram\":%lu,"
    "\"gps\":{\"fix\":%d,\"sats\":%d,\"utc\":\"%s\",\"sentences\":%lu,\"valid_nmea\":%lu,\"baud\":%lu,"
    "\"tacc_ns\":%lu,\"qerr_ps\":%ld,\"ubx_pvt\":%lu,\"ubx_timtp\":%lu,\"ubx_ack\":%lu,\"ubx_nak\":%lu,\"config_acked\":%s,\"timtp_flags\":%u,\"qerr_valid\":%s},"
    // hell_m is height above the WGS84 ELLIPSOID and is the field a geodetic transform wants;
    // hmsl_m is the human-readable one and must not be fed to lat/lon/h -> ECEF.
    "\"pos\":{\"lat\":%.7f,\"lon\":%.7f,\"hell_m\":%.3f,\"hmsl_m\":%.3f,"
      "\"hacc_m\":%.2f,\"vacc_m\":%.2f,\"n\":%lu,"
      "\"mean_lat\":%.7f,\"mean_lon\":%.7f,\"mean_hell_m\":%.3f,\"mean_hmsl_m\":%.3f},"
    "\"pps\":{\"edges\":%lu,\"glitches\":%lu,\"probe_resyncs\":%lu,\"interval_min_us\":%lu,\"interval_max_us\":%lu,\"spread_us\":%ld},"
    "\"i2s\":{\"nominal_hz\":%d,\"measured_hz\":%.4f,\"ppm\":%.1f,\"samples\":%lu},"
    // measured_hz above is cumulative and stays poisoned by any stall. acq is the one to trust.
    "\"acq\":{\"fs_clean_hz\":%.4f,\"win_s\":%lu,\"drop_s\":%lu,\"drop_samples\":%lu,\"last_s\":%lu},"
    "\"esp_clock\":{\"ppm_vs_gps\":%.3f,\"pps_intervals\":%lu},"
    "\"time\":{\"valid\":%s,\"utc_us\":%lld,\"since_edge_us\":%llu,\"navpvt_nano\":%ld,\"label_rejects\":%lu},"
    "\"audio\":{\"enabled\":true,\"detections\":%lu,\"written\":%lu,\"lost\":%lu,"
    "\"ambient\":%.1f,\"env_peak\":%.0f},"
    // floor/floor_source: thr alone only BOUNDS the floor from above, and only while the
    // adaptive limb is binding (32 of the capture's 1450 rows), so a settable floor that was not
    // reported here would have to be guessed from the record.
    "\"gate\":{\"armed\":%d,\"thr\":%.0f,\"e_max_win\":%.1f,\"headroom\":%.2f,"
    "\"forced_rearms\":%lu,\"dc\":%.1f,\"floor\":%.0f,\"floor_default\":%.0f,"
    "\"floor_min\":%.0f,\"floor_max\":%.0f,\"floor_source\":\"%s\",\"floor_saved\":%s},"
    "\"write_fail\":%lu,"
    // raw: what /audio can actually serve. span_s is what was allocated, held_s what has been
    // written into it so far -- they differ only for the first few minutes after a boot.
    "\"raw\":{\"span_s\":%.1f,\"cap_samples\":%lu,\"held_samples\":%lu,\"fill_pct\":%.1f,"
    "\"from_utc_us\":%lld,\"to_utc_us\":%lld,\"marks\":%lu,\"bytes\":%lu},"
    // scene: fft_us_per_row is MEASURED on this part, summed over the 64 frames of one row.
    // bands/f_lo_hz/f_hi_hz: the scene bank is NOT the detection bank any more, and a reader
    // that assumes MEL16's 312 Hz band 0 would misread every row. Say which bank produced them.
    "\"scene\":{\"rows\":%lu,\"written\":%lu,\"row_span_ms\":%d,\"fft_us_per_row\":%lu,\"fft_us_max\":%lu,"
    "\"short_blocks\":%lu,\"write_fail\":%lu,\"bands\":%d,\"slices\":%d,\"f_lo_hz\":%.1f,\"f_hi_hz\":%.1f},"
    // clips: written advances only on a full CLIP_BYTES landing, skip_budget only when one was
    // wanted and refused. A card that filled at 03:00 and a night that went quiet at 03:00 differ
    // here and nowhere else.
    "\"clips\":{\"written\":%lu,\"skip_budget\":%lu,\"skip_cardfull\":%lu,"
    "\"skip_dedupe\":%lu,\"skip_ring\":%lu,"
    "\"nocard\":%lu,\"fail\":%lu,\"bytes_each\":%lu,\"budget_b\":%lu,\"budget_left_b\":%lu,"
    "\"budget_left_clips\":%lu,\"pre_s\":%.1f,\"post_s\":%.1f,\"dir\":\"%s\",\"boot\":\"%s\"},"
    "\"env\":{\"temp_c\":%s,\"press_hpa\":%s,\"c_mps\":%s,\"reads\":%lu,\"fail\":%lu},"
    "\"sd\":%s,\"sd_free_mb\":%lu,\"sd_total_mb\":%lu,\"i2c\":\"%s\"}",
    node_id, NODE_CLASS, FW_BUILD,
    (unsigned long)((millis() - boot_ms) / 1000), (unsigned long)ESP.getFreeHeap(),
    (unsigned long)ESP.getFreePsram(),
    gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences,
    (unsigned long)nmea_valid, (unsigned long)gps_baud,
    (unsigned long)gps_tacc_ns, (long)gps_qerr_ps, (unsigned long)ubx_pvt,
    (unsigned long)ubx_timtp, (unsigned long)ubx_ack, (unsigned long)ubx_nak,
    (ubx_ack ? "true" : "false"), (unsigned)timtp_flags,
    ((timtp_flags != 0xFF && !(timtp_flags & 0x10)) ? "true" : "false"),
    pos_lat_e7 * 1e-7, pos_lon_e7 * 1e-7,
    pos_hell_mm / 1000.0, pos_hmsl_mm / 1000.0,
    pos_hacc_mm / 1000.0, pos_vacc_mm / 1000.0, (unsigned long)pos_n,
    pos_n ? pos_sum_lat  / pos_n * 1e-7   : 0.0, pos_n ? pos_sum_lon  / pos_n * 1e-7   : 0.0,
    pos_n ? pos_sum_hell / pos_n / 1000.0 : 0.0, pos_n ? pos_sum_hmsl / pos_n / 1000.0 : 0.0,
    (unsigned long)pps_count, (unsigned long)pps_glitch, (unsigned long)pps_resyncs,
    (unsigned long)(pps_count > 1 ? pps_int_min : 0), (unsigned long)(pps_count > 1 ? pps_int_max : 0),
    (long)(pps_count > 1 ? (long)pps_int_max - (long)pps_int_min : 0),
    FS_NOMINAL, fs, fs > 0 ? (fs / FS_NOMINAL - 1.0) * 1e6 : 0.0, (unsigned long)g_samples,
    fs_clean, (unsigned long)fs_clean_secs, (unsigned long)drop_seconds,
    (unsigned long)drop_samples, (unsigned long)samp_sec_last,
    esp_clock_ppm(NULL), (unsigned long)(pps_count > 1 ? pps_count - 1 : 0),
    time_valid ? "true" : "false", (long long)utc_now, (unsigned long long)since_edge,
    (long)last_nano, (unsigned long)time_glitch,
    (unsigned long)det_n, (unsigned long)det_flushed, (unsigned long)det_lost,
    g_amb, env_peak_seen,
    armed, gate_thr(), env_e_max_win, env_e_max_win / gate_thr(),
    (unsigned long)gate_forced, sig_dc,
    g_floor, FLOOR_DEFAULT, FLOOR_MIN, FLOOR_MAX, g_floor_src, floor_saved,
    (unsigned long)det_write_fail,
    praw_cap ? (double)praw_cap / (fs_clean > 1000.0 ? fs_clean : (double)FS_NOMINAL) : 0.0,
    (unsigned long)praw_cap, (unsigned long)r_held,
    praw_cap ? 100.0 * (double)r_held / (double)praw_cap : 0.0,
    (long long)r_from, (long long)r_to, (unsigned long)praw_mark_n,
    (unsigned long)(praw_cap * 2UL),
    (unsigned long)scene_rows, (unsigned long)scene_written,
    (int)((uint32_t)SCENE_FRAMES * MELS_NFFT * 1000u / FS_NOMINAL),
    (unsigned long)scene_fft_us_last, (unsigned long)scene_fft_us_max,
    (unsigned long)scene_short_blocks, (unsigned long)scene_write_fail,
    MELS_BANDS, SCENE_SLICES, MELS_F_LO, MELS_F_HI,
    (unsigned long)clip_written, (unsigned long)clip_skip_budget, (unsigned long)clip_skip_full,
    (unsigned long)clip_skip_dedupe, (unsigned long)clip_skip_ring,
    (unsigned long)clip_nocard, (unsigned long)clip_fail,
    (unsigned long)CLIP_BYTES, (unsigned long)CLIP_BUDGET_B, (unsigned long)clip_budget_left,
    (unsigned long)(clip_budget_left / CLIP_BYTES),
    (double)CLIP_PRE_SAMPLES / FS_NOMINAL, (double)CLIP_POST_SAMPLES / FS_NOMINAL,
    CLIP_DIR, clip_boot,
    envs_t, envs_p, envs_c, (unsigned long)bmp_reads, (unsigned long)bmp_fail,
    sd_ok ? "true" : "false",
    (unsigned long)(sd_ok ? (SD.totalBytes() - SD.usedBytes()) / 1048576UL : 0UL),
    (unsigned long)(sd_ok ? SD.totalBytes() / 1048576UL : 0UL), i2c_found);
  return String(b);
}

static void h_root() {
  String p = "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
             "<title>dama-hear node</title>"
             "<style>body{font:14px system-ui;margin:2rem;max-width:44rem}"
             "td{padding:.2rem .8rem .2rem 0}b{font-variant-numeric:tabular-nums}"
             "code{background:#eee;padding:.1rem .3rem}</style>"
             "<h2>dama-hear night node</h2><table id=t></table>"
             "<p><a href='/detections'>detections</a> &middot; <a href='/status'>json</a></p>"
             "<script>async function u(){const s=await(await fetch('/status')).json();"
             "const f=s.i2s.measured_hz?s.i2s.measured_hz.toFixed(4)+' Hz ('+s.i2s.ppm.toFixed(1)+' ppm)':'waiting for 3 PPS edges';"
             "document.getElementById('t').innerHTML="
             "`<tr><td>uptime<td><b>${s.uptime_s} s</b>`+"
             "`<tr><td>GPS<td><b>fix ${s.gps.fix}, ${s.gps.sats} sats, ${s.gps.utc} UTC</b> @ ${s.gps.baud} baud, ${s.gps.valid_nmea} valid lines`+`<tr><td>time accuracy<td><b>${s.gps.tacc_ns} ns</b> &middot; pulse qErr ${s.gps.qerr_ps} ps`+`<tr><td>UBX<td>pvt ${s.gps.ubx_pvt}, tim-tp ${s.gps.ubx_timtp}, ack ${s.gps.ubx_ack}, nak ${s.gps.ubx_nak}`+`<tr><td>config<td><b>${s.gps.config_acked?'accepted':'NOT acked - node TX to module RX unwired?'}</b>`+"
             "`<tr><td>PPS edges<td><b>${s.pps.edges}</b> spread ${s.pps.spread_us} us, ${s.pps.glitches} rejected`+"
             "`<tr><td>I2S measured<td><b>${f}</b>`+`<tr><td>UTC now<td><b>${s.time.valid?new Date(s.time.utc_us/1000).toISOString():'anchor not valid'}</b> &middot; ${(s.time.since_edge_us/1000).toFixed(0)} ms since edge, ${s.time.label_rejects} rejected`+`<tr><td>ESP clock vs GPS<td><b>${s.esp_clock.pps_intervals>2?s.esp_clock.ppm_vs_gps.toFixed(3)+' ppm':'need 3 PPS edges'}</b> over ${s.esp_clock.pps_intervals} s`+"
             "`<tr><td>samples<td>${s.i2s.samples}`+"
             "`<tr><td>detections<td><b>${s.audio.detections}</b> (env peak ${s.audio.env_peak})`+"
             "`<tr><td>raw ring<td>${s.raw.span_s?s.raw.span_s.toFixed(0)+' s, '+s.raw.fill_pct.toFixed(0)+'% written, '+(s.raw.bytes/1048576).toFixed(2)+' MB PSRAM':'<b>not allocated</b>'}`+"
             "`<tr><td>scene<td>${s.scene.rows} rows &middot; FFT <b>${s.scene.fft_us_per_row} us</b> per ${s.scene.row_span_ms} ms (peak ${s.scene.fft_us_max})`+"
             "`<tr><td>gate<td>floor <b>${s.gate.floor}</b> (${s.gate.floor_source}${s.gate.floor_saved!==null?', saved '+s.gate.floor_saved:''}) &middot; thr ${s.gate.thr} &middot; ambient ${s.audio.ambient} &middot; peak ${s.gate.e_max_win}`+"
             "`<tr><td>clips<td><b>${s.clips.written}</b> written &middot; ${s.clips.budget_left_clips} left in budget &middot; skipped ${s.clips.skip_budget} budget / ${s.clips.skip_cardfull} cardfull / ${s.clips.skip_dedupe} dedupe / ${s.clips.skip_ring} ring / ${s.clips.fail} fail`+"
             "`<tr><td>SD<td>${s.sd}`+`<tr><td>I2C<td>${s.i2c}`+`<tr><td>heap / psram<td>${s.heap} / ${s.psram}`;}"
             "u();setInterval(u,2000);</script>";
  http.send(200, "text/html", p);
}
static void h_status() { http.send(200, "application/json", status_json()); }
static void h_dets() {
  uint32_t total = det_n;
  uint32_t n = total < MAXDET ? total : MAXDET;
  uint32_t first = total - n;                 // ring: the newest n, oldest first
  String o = "[";
  o.reserve(n * (MEL16_FRAME_BYTES * 2 + 280) + 64);
  for (uint32_t k = first; k < total; k++) {
    const Det &d = dets[k % MAXDET];
    char b[240];
    snprintf(b, sizeof b, "%s{\"i\":%lu,\"utc_us\":%lld,\"uptime_s\":%lu,\"sample\":%lu,\"pps_n\":%lu,"
                          "\"us_since_pps\":%ld,\"trigger\":%d,\"flags\":%u,\"fs_hz\":%.3f,"
                          "\"frame_len\":%d,\"frame\":\"",
             k == first ? "" : ",", (unsigned long)k, (long long)d.utc_us,
             (unsigned long)d.uptime_s, (unsigned long)d.sample,
             (unsigned long)d.pps_n, (long)d.us_since_pps, d.trigger, (unsigned)d.flags, d.fs_at,
             MEL16_FRAME_BYTES);
    o += b;
    static const char hx[] = "0123456789abcdef";
    for (int j = 0; j < MEL16_FRAME_BYTES; j++) {
      o += hx[d.frame[j] >> 4]; o += hx[d.frame[j] & 0xF];
    }
    // ⚠️tools/hear_bridge.py's rows_from_detections() selects DETS_COLUMNS[:-1], so these two keys
    // are dropped on that path until that list grows. They are here anyway: /detections is also
    // read by hand, and the live ring is the only place a still-PENDING clip is visible at all.
    char t[80];
    char cp[48] = "";
    if (d.clip_st == CLIP_OK) clip_name(cp, sizeof cp, d.sample);
    snprintf(t, sizeof t, "\",\"clip\":\"%s\",\"clip_why\":\"%s\"}", cp, clip_why(d.clip_st));
    o += t;
  }
  o += "]";
  http.send(200, "application/json", o);
}

// Hoisted above setup(): /format must close this before it unmounts, and the section that
// uses it sits further down the file. scenef is already declared earlier.
static File detf;

// GPS bring-up: choose the pins, then find the baud, then configure. Extracted from setup()
// so it can be RUN AGAIN, because running it exactly once at boot turned out to be the bug.
//
// mach is wired with its TX/RX pair reversed, and gps_pick_pins() reads which pin actually
// carries a transmitter. After an OTA reboot the ESP is serving HTTP in under a second while
// the module is still starting, so NEITHER pin was toggling yet -- the probe fell back to
// "documented wiring", which is the pinout mach does not have. It then autobauded against a
// pin with nothing on it, settled on 9600, and sat at fix 0 with zero UBX frames for as long
// as it was left. The boot-time answer was wrong and nothing ever revisited it.
//
// The configure() is inside the retry deliberately: re-detecting the pins without re-sending
// CFG-VALSET would leave the module at whatever rate it shipped with, which is the 5 Hz/10 Hz
// asymmetry that cost mach two rejected UTC labellings a second.
// The baud sweep on its own, so /gpspins can re-run it after FORCING a pin order. Extracted
// rather than duplicated: a second copy would drift, and the candidate list is the part that
// matters -- a module configured UBX-binary only shows nothing to a NMEA-only scan.
// Returns whether anything actually DECODED. The caller cannot use nmea_valid/ubx_pvt for
// that: those are incremented by the runtime parser, never by this sweep, so they read 0 after a
// SUCCESSFUL sweep just as they do after a failed one. Using them cost nyquist and rankine their
// fix -- the fallback below fired even when the first sweep had found 230400.
static bool gps_autobaud() {
  // Find the module's baud instead of assuming it. Assuming 9600 produced a stream that a lenient
  // parser happily counted as 22 "sentences" while zero of them were valid NMEA -- a wrong number
  // that looked like a working link. Count VALID lines and let the module tell us.
  {
    // Count UBX frames as well as NMEA lines. A module that came off a flight controller is very
    // often configured UBX-binary only with NMEA disabled -- so a NMEA-only scan sees a live,
    // driven, busy line and reports nothing at every rate, which is exactly what happened.
    static const uint32_t cand[] = {9600, 38400, 115200, 57600, 19200, 230400, 460800, 4800};
    uint32_t best_b = 0; int best_score = 0; bool best_ubx = false;
    for (unsigned k = 0; k < sizeof(cand) / sizeof(cand[0]); k++) {
      Serial1.begin(cand[k], SERIAL_8N1, gps_rx_pin, gps_tx_pin);
      delay(60); while (Serial1.available()) Serial1.read();
      int nm = 0, ub = 0, i = 0; char ln[100]; uint8_t prev = 0; uint32_t t0 = millis();
      while (millis() - t0 < 1200) {
        while (Serial1.available()) {
          uint8_t c = (uint8_t)Serial1.read();
          if (prev == 0xB5 && c == 0x62) ub++;            // UBX sync word
          prev = c;
          if (c == '\n' || i >= 99) {
            ln[i] = 0;
            if (i > 6 && ln[0] == '$' && ln[1] >= 'A' && ln[1] <= 'Z' && ln[2] >= 'A' && ln[2] <= 'Z') nm++;
            i = 0;
          } else if (c != '\r' && c >= 32 && c < 127) ln[i++] = (char)c;
        }
      }
      logf("gps   %6lu baud -> %d NMEA, %d UBX\n", (unsigned long)cand[k], nm, ub);
      int score = nm + ub;
      if (score > best_score) { best_score = score; best_b = cand[k]; best_ubx = (ub > nm); }
      Serial1.end();
    }
    gps_baud = best_b ? best_b : 9600;
    Serial1.begin(gps_baud, SERIAL_8N1, gps_rx_pin, gps_tx_pin);
    logf("gps   using %lu baud (%s)%s\n", (unsigned long)gps_baud,
                  best_ubx ? "UBX binary" : "NMEA",
                  best_b ? "" : " -- nothing decoded at any rate");
    return best_b != 0;
  }
}


static void gps_bringup() {
  // DETACH THE UART FIRST. gps_pick_pins() reads both pins with digitalRead, and on a RETRY the
  // UART peripheral still owns them -- so the probe measured a pin it did not control, saw nothing
  // move, and reported "neither pin toggled" every single time. The sweep that follows then called
  // Serial1.begin() on an already-open port, which does not reinitialise cleanly, so it found
  // nothing at any baud either.
  //
  // The effect was a watchdog that looked like it was working and could never succeed: nyquist ran
  // four attempts over 14 minutes and failed all four, while POST /gpspins -- identical code, but
  // it calls end() first -- found 230400 on the first try. At boot the bug is invisible because no
  // UART is open yet, which is exactly why it survived until the fleet started retrying.
  Serial1.end();
  gps_pick_pins();
  bool decoded = gps_autobaud();

  // IF NOTHING DECODED, TRY THE OTHER PIN ORDER BEFORE GIVING UP.
  //
  // gps_pick_pins() can only decide while the module is TALKING, and a module that is quiet at
  // the moment the probe runs -- which is most of them, for the first few seconds after a reset --
  // sends it to the DOCUMENTED order by default. On a node wired the other way that is a deadlock:
  // the ESP's TX lands on the module's TX, so nothing can be sent to wake it, and the sweep finds
  // nothing at any baud for ever.
  //
  // Measured on mach tonight: it entered that state on EVERY reflash, three times, and each time
  // POST /gpspins?swap=1 found 115200 immediately -- the module had been transmitting the whole
  // time on the pin the 250 ms probe was not watching. A fleet meant to run continuously cannot
  // have a coin-flip at every reboot that costs a node its GPS until someone drives out to it.
  //
  // So: swap and sweep again. It costs ~11 s on a node that has already failed to decode anything,
  // and nothing at all on a node that worked first time.
  if (!decoded) {
    int rx = gps_rx_pin, tx = gps_tx_pin;
    gps_rx_pin = (rx == GPS_RX) ? GPS_TX : GPS_RX;
    gps_tx_pin = (tx == GPS_TX) ? GPS_RX : GPS_TX;
    logf("gps   nothing decoded on RX=GPIO%d -- trying the other order, RX=GPIO%d\n",
         rx, gps_rx_pin);
    if (gps_autobaud()) {
      gps_pin_src = "measured: SWAPPED at the module (found by fallback)";
    } else {
      // Neither order works. Put the pins back AND SWEEP AGAIN, because the failed attempt left
      // the UART open at whatever that sweep settled on -- 9600 -- and restoring only the pin
      // VARIABLES leaves the port wrong. That is what cost nyquist and rankine their fix.
      gps_rx_pin = rx; gps_tx_pin = tx;
      logln("gps   neither pin order decoded -- module is silent or unpowered");
      gps_autobaud();
    }
  }

  delay(300);
  gps_configure();
}

void setup() {
  boot_guard();                    // first statement: a later fault still counts as a failed boot
  Serial.begin(115200);
  delay(1500);
  boot_ms = millis();
  logf("boot  attempt %lu on partition %s\n", (unsigned long)boot_try,
                esp_ota_get_running_partition()->label);
  node_identity();          // before anything logs or joins: the id names the log and the AP
  logf("\n=== dama-hear night node %s (%s) fw %s ===\n", node_id, NODE_CLASS, FW_BUILD);

  // Try each configured network in turn. An outdoor node may only reach one of them, and which
  // one is not knowable from indoors.
  int joined_idx = 0;
  if (WIFI_N > 0) {
    WiFi.mode(WIFI_STA); WiFi.setSleep(false);
    for (int k = 0; k < WIFI_N && !sta_ok; k++) {
      logf("wifi  trying network %d/%d", k + 1, WIFI_N);
      WiFi.begin(WIFI_SSIDS[k], WIFI_PASSES[k]);
      for (int i = 0; i < 24 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
      sta_ok = WiFi.status() == WL_CONNECTED;
      if (sta_ok) joined_idx = k + 1;
      logln(sta_ok ? " joined" : " no");
      if (!sta_ok) WiFi.disconnect();
    }
  }
  if (sta_ok) {
    // Network NAME deliberately not logged: /log is unauthenticated and this node is meant to sit
    // outdoors. The index is enough to tell which of the configured networks answered.
    logf("wifi  STA  network %d/%d  http://%s/\n", joined_idx, WIFI_N,
         WiFi.localIP().toString().c_str());
  }
  else {
    char ap[40]; snprintf(ap, sizeof ap, "%s-%s", AP_SSID, node_id);
    WiFi.mode(WIFI_AP); WiFi.softAP(ap, AP_PASS);
    logf("wifi  AP   ssid \"%s\" pass \"%s\"  http://%s/\n",
                  ap, AP_PASS, WiFi.softAPIP().toString().c_str());
    logln("      (no secrets.h, or the join failed -- see firmware/night_node/README)");
  }
  if (MDNS.begin(node_id)) logf("mdns  http://%s.local/\n", node_id);

  // PULLDOWN, not bare INPUT. An unconnected CMOS input floats and self-oscillates -- measured
  // ~3.4 kHz of phantom edges, which the rate maths happily turned into a plausible +626 ppm.
  pinMode(PPS_PIN, INPUT_PULLDOWN);
  // Same probe as the RX line. Once the module has a fix and has ACKed TP1, it IS pulsing, so
  // silence here can only be the wire or the tap point -- worth stating rather than inferring.
  { int high = 0, edges = 0, last = digitalRead(PPS_PIN); uint32_t t0 = millis();
    while (millis() - t0 < 1500) { int v = digitalRead(PPS_PIN); if (v) high++; if (v != last) { edges++; last = v; } }
    logf("pps   pin (D0/GPIO%d) over 1.5 s: %d edges, %s\n", PPS_PIN, edges,
                  edges ? "something is pulsing it" : "flat -- nothing connected to the tap"); }
  attachInterrupt(digitalPinToInterrupt(PPS_PIN), pps_isr, RISING);
  // D6/D7 are GPIO43/44 -- the ESP32-S3's DEFAULT UART0 pins. With USB-CDC carrying Serial, UART0
  // is still instantiated and can drive GPIO43, contending with Serial1's TX. Release it first.
#ifdef ARDUINO_USB_CDC_ON_BOOT
  Serial0.end();
#endif
  // Which pin is the module actually transmitting on, and is it transmitting at all. This replaces
  // an earlier pulldown probe that asked only "does D7 read high", and answered "DRIVEN high -- a
  // transmitter is connected" on mach for a pin with nothing on it: a pulled-up module input reads
  // exactly like a transmitter idling high, so the test could not tell the two apart. Counting
  // RECURRING run lengths on both pins can, and it also says which way round the pair is wired.
  gps_bringup();
  logln("gps   UBX config sent (TP1 1 Hz locked+unlocked, NAV-PVT, TIM-TP; RAM layer)");

  Wire.begin(I2C_SDA, I2C_SCL, 100000);
  // Bounded, because every I2C call here runs on the loop task that also drains the I2S DMA.
  // A sensor that stops ACKing mid-transfer must cost a failed read and a counter, not a
  // blocked loop and a watchdog reset.
  Wire.setTimeOut(25);
  i2c_scan();
  if (!bmp_begin()) logln("bmp   no BMP280/BME280 -- sound speed reported as null");

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI);
  sd_cs = 0;
  // max_files 8, not the library's default 5 (SD.h:29). The clip writer holds a WAV open across
  // loop iterations, so the long-lived set is now dets.csv + scene.csv + the clip = 3, and /ls
  // holds a directory plus an entry while the 30 s health block opens health.csv -- which is 6,
  // one past the default, and an SD.open past the limit just returns a falsy File. Raising it
  // costs a pointer array; the per-file caches are allocated on open, not here.
  if (SD.begin(21, SPI, 20000000, "/sd", 8)) { sd_ok = true; sd_cs = 21; }
  else if (SD.begin(3, SPI, 20000000, "/sd", 8)) { sd_ok = true; sd_cs = 3; }
  logf("sd    %s%s", sd_ok ? "mounted, CS=" : "no card", sd_ok ? "" : "\n");
  if (sd_ok) logf("%d\n", sd_cs);
  snprintf(clip_boot, sizeof clip_boot, "%08lx", (unsigned long)esp_random());
  if (sd_ok) {
    // A subdirectory, not the root: FAT root directory entries are finite and LFN is on for this
    // FQBN (CONFIG_FATFS_MAX_LFN 255), so each long clip name burns several of them.
    if (!SD.exists(CLIP_DIR)) SD.mkdir(CLIP_DIR);
    sd_free_mb_last = (uint32_t)((SD.totalBytes() - SD.usedBytes()) / 1048576UL);
    gate_floor_load();
    logf("clip  %s/%s-*.wav, %lu B each, budget %lu B (%lu clips), %lu MB free\n",
         CLIP_DIR, clip_boot, (unsigned long)CLIP_BYTES, (unsigned long)CLIP_BUDGET_B,
         (unsigned long)(CLIP_BUDGET_B / CLIP_BYTES), (unsigned long)sd_free_mb_last);
  }
  logf("gate  floor %.0f (%s), min %.0f max %.0f\n", g_floor, g_floor_src, FLOOR_MIN, FLOOR_MAX);

  i2s.setPinsPdmRx(PDM_CLK, PDM_DIN);
  if (!i2s.begin(I2S_MODE_PDM_RX, FS_NOMINAL, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO))
    logln("i2s   FAILED");
  else logf("i2s   PDM %d Hz on CLK=%d DIN=%d\n", FS_NOMINAL, PDM_CLK, PDM_DIN);

  // Raw ring. Ask for 240 s (7.68 MB of the 8.34 MB free) and step down rather than fail: what
  // matters is largest CONTIGUOUS free block, which total-free does not report. Log the span that
  // was actually obtained -- a silent failure here would look identical to a quiet night, which is
  // the failure class env_e_max_win already exists to rule out.
  {
    static const uint32_t want_s[] = {240, 180, 120, 60, 30};
    for (unsigned k = 0; k < sizeof(want_s) / sizeof(want_s[0]) && !praw; k++) {
      size_t want = (size_t)want_s[k] * FS_NOMINAL * sizeof(int16_t);
      // Leave 256 kB of PSRAM behind: WiFi buffers and the web server allocate from it too, and a
      // ring that takes the last byte would trade audio for a node that cannot be reached.
      if (heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM) < want + 262144) continue;
      praw = (int16_t *)ps_malloc(want);
      if (praw) { praw_cap = want_s[k] * FS_NOMINAL; praw_want_s = want_s[k]; }
    }
    if (praw)
      logf("praw  raw ring %lu s = %lu kB PSRAM, %lu kB PSRAM still free\n",
           (unsigned long)praw_want_s, (unsigned long)(praw_cap * 2UL / 1024UL),
           (unsigned long)(ESP.getFreePsram() / 1024UL));
    else
      logln("praw  NO PSRAM ring -- /audio is disabled; capture, gate and logging are unaffected");
  }

  http.on("/", h_root); http.on("/status", h_status); http.on("/detections", h_dets);
  http.on("/update", HTTP_POST,
    []() {
      bool bad = Update.hasError();
      http.send(bad ? 500 : 200, "text/plain", bad ? "FAILED\n" : "OK, rebooting into the new image\n");
      delay(400); ESP.restart();
    },
    []() {
      HTTPUpload &u = http.upload();
      if (u.status == UPLOAD_FILE_START) {
        snprintf(ota_msg, sizeof ota_msg, "receiving %s", u.filename.c_str());
        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
      } else if (u.status == UPLOAD_FILE_WRITE) {
        if (Update.write(u.buf, u.currentSize) != u.currentSize) Update.printError(Serial);
      } else if (u.status == UPLOAD_FILE_END) {
        if (Update.end(true)) snprintf(ota_msg, sizeof ota_msg, "wrote %u B", (unsigned)u.totalSize);
        else { Update.printError(Serial); snprintf(ota_msg, sizeof ota_msg, "write failed"); }
      }
    });
  http.on("/ota", []() {
    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_ota_img_states_t st = ESP_OTA_IMG_UNDEFINED;
    esp_ota_get_state_partition(run, &st);
    char b[320];
    snprintf(b, sizeof b,
      "build     " BUILD_TAG "\nrunning   %s @ 0x%06lx\nboot_try  %lu (reverts after %d)\nhealthy   %s\n"
      "img_state %d\nlast ota  %s\n\npush:  curl -F firmware=@<bin> http://<ip>/update\n",
      run->label, (unsigned long)run->address, (unsigned long)boot_try, BOOT_MAX_TRIES,
      marked_healthy ? "yes" : "not yet", (int)st, ota_msg);
    http.send(200, "text/plain", b);
  });
  http.on("/log", []() {
    String o;
    if (log_wrapped) { o.reserve(LOGBUF + 1); for (size_t i = log_w; i < LOGBUF; i++) o += logbuf[i]; }
    for (size_t i = 0; i < log_w; i++) o += logbuf[i];
    http.send(200, "text/plain", o);
  });
  http.on("/reboot", HTTP_POST, []() {      // POST, so a link prefetcher cannot reboot the node
    http.send(200, "text/plain", "rebooting\n");
    delay(300); ESP.restart();
  });
  http.on("/sd", []() {
    // night.csv was described as the durable record. A record that can only be read by walking
    // outside and pulling the card is not one -- this makes it retrievable over the same link.
    String name = http.hasArg("file") ? http.arg("file") : String("/night.csv");
    if (!name.startsWith("/")) name = "/" + name;
    if (name.indexOf("..") >= 0) { http.send(400, "text/plain", "no\n"); return; }
    if (!sd_ok) { http.send(503, "text/plain", "no card mounted\n"); return; }
    File f = SD.open(name.c_str(), FILE_READ);
    if (!f) { http.send(404, "text/plain", "not found: " + name + "\n"); return; }
    long tail = http.hasArg("tail") ? http.arg("tail").toInt() : 0;   // last N bytes
    size_t remain = f.size();
    if (tail > 0 && remain > (size_t)tail) { f.seek(remain - tail); remain = tail; }
    // streamFile() advertises f.size() regardless of the seek, so a tail request promised the
    // whole file and delivered a fragment -- curl reports "end of response with N bytes missing".
    // Send the length of what we are ACTUALLY sending.
    http.sendHeader("Content-Disposition", "inline; filename=\"" + name.substring(1) + "\"");
    http.setContentLength(remain);
    http.send(200, "text/csv", "");
    uint8_t buf[512];
    while (remain) {
      size_t n = f.read(buf, remain > sizeof buf ? sizeof buf : remain);
      if (!n) break;
      http.client().write(buf, n);
      remain -= n;
    }
    f.close();
  });
  http.on("/ls", []() {
    if (!sd_ok) { http.send(503, "text/plain", "no card mounted\n"); return; }
    String o; File d = SD.open("/");
    for (File e = d.openNextFile(); e; e = d.openNextFile()) {
      o += String(e.isDirectory() ? "d " : "- ") + e.name() + "  " + String((long)e.size()) + " B\n";
      e.close();
    }
    http.send(200, "text/plain", o.length() ? o : "(empty)\n");
  });
  http.on("/perf", []() {            // measured throughput, not a datasheet number
    int mb = http.hasArg("mb") ? http.arg("mb").toInt() : 4;
    if (mb < 1) mb = 1; if (mb > 32) mb = 32;
    static uint8_t chunk[1460];        // one TCP segment
    for (size_t i = 0; i < sizeof chunk; i++) chunk[i] = (uint8_t)i;
    http.setContentLength((size_t)mb * 1024 * 1024);
    http.send(200, "application/octet-stream", "");
    WiFiClient c = http.client();
    size_t sent = 0, total = (size_t)mb * 1024 * 1024;
    while (sent < total && c.connected()) {
      size_t n = total - sent; if (n > sizeof chunk) n = sizeof chunk;
      if (c.write(chunk, n) != n) break;
      sent += n;
    }
  });
  http.on("/pins", []() {          // the compiled-in map, so it can be checked rather than trusted
    // Every row is DERIVED from the pin #defines and the runtime SD chip-select. This table used
    // to be a literal, and it went stale the moment a pin moved -- naming pad D11 for GPIO1, and
    // still calling the mic "disabled" long after it came back. A map that names the wrong pad is
    // worse than no map, because it is the thing you check the wiring against.
    static const struct { const char *pad; int gpio; } PADS[] = {
      {"D0", 1}, {"D1", 2}, {"D2", 3}, {"D3", 4}, {"D4", 5}, {"D5", 6}, {"D6", 43},
      {"D7", 44}, {"D8", 7}, {"D9", 8}, {"D10", 9}, {"D11", 42}, {"D12", 41},
    };
    String out = "pad   GPIO  assignment\n";
    for (auto &e : PADS) {
      const char *role = "free";
      if      (e.gpio == PPS_PIN) role = "GPS PPS in  <-- this build";
      else if (e.gpio == I2C_SDA) role = "I2C SDA  (IST8310 0x0E, BMP280 0x76)";
      else if (e.gpio == I2C_SCL) role = "I2C SCL";
      else if (e.gpio == GPS_TX)  role = "GPS TX -> module RX";
      else if (e.gpio == GPS_RX)  role = "GPS RX <- module TX  (230400 baud, UBX)";
      else if (e.gpio == SD_SCK)  role = "microSD SCK   (driven output -- do not tap)";
      else if (e.gpio == SD_MISO) role = "microSD MISO  (driven output -- do not tap)";
      else if (e.gpio == SD_MOSI) role = "microSD MOSI  (driven output -- do not tap)";
      else if (e.gpio == PDM_CLK) role = "PDM mic CLK   (driven output -- do not tap)";
      else if (e.gpio == PDM_DIN) role = "PDM mic DATA";
      else if (e.gpio == (int)sd_cs) role = "microSD CS";
      char row[96];
      snprintf(row, sizeof row, "%-5s %-5d %s\n", e.pad, e.gpio, role);
      out += row;
    }
    // sd_cs is a runtime discovery, and on this expansion board it is NOT on a numbered pad.
    char tail[128];
    snprintf(tail, sizeof tail, "\nmicroSD CS is on GPIO%d%s\n", (int)sd_cs,
             sd_cs == 21 ? " (expansion-board wiring, not the D2 pad the silkscreen implies)" : "");
    out += tail;
    out += "a pad marked 'free' is safe to tap. anything else is driven by this build.\n";
    http.send(200, "text/plain", out);
  });
  // Renamed from /tp. Two handlers were registered on /tp and WebServer answers with the
  // FIRST match, so the UBX timepulse read-back below has never once been reachable --
  // which is why /status could only ever say "(not read)". A duplicate route is a silent
  // shadow, not an error.
  http.on("/pinsweep", []() {
    // Answers "did you measure it right?" without relying on my two assumptions: that the wire
    // landed on D0, and that a ~45k internal pulldown cannot drag down a weakly-coupled tap.
    // Every free pin, all three pull modes. A 1 Hz / 100 ms pulse = ~2-3 edges and ~10% high.
    static const int pins[] = {1, 2, 4};              // D0, D1, D3 -- the rest are in use
    static const char *nm[] = {"D0/GPIO1", "D1/GPIO2", "D3/GPIO4"};
    static const int modes[] = {INPUT_PULLDOWN, INPUT, INPUT_PULLUP};
    static const char *mn[] = {"pulldown", "float   ", "pullup  "};
    String o = "1 Hz / 100 ms pulse looks like ~2-3 edges and ~10% high.\n"
               "thousands of edges = floating noise, not signal.\n\n";
    bool watching = true;
    detachInterrupt(digitalPinToInterrupt(PPS_PIN));
    for (unsigned q = 0; q < 3; q++) {
      o += String(nm[q]) + "\n";
      for (unsigned m = 0; m < 3; m++) {
        pinMode(pins[q], modes[m]); delay(30);
        int high = 0, n = 0, edges = 0, last = digitalRead(pins[q]);
        uint32_t t0 = millis();
        while (millis() - t0 < 2500) { int v = digitalRead(pins[q]); if (v) high++; n++;
                                       if (v != last) { edges++; last = v; } }
        char b[96];
        snprintf(b, sizeof b, "  %s  edges %6d   high %5.1f%%   %s\n", mn[m], edges,
                 n ? 100.0 * high / n : 0.0,
                 (edges >= 2 && edges <= 12) ? "<-- looks like 1 Hz" : (edges > 200 ? "noise" : ""));
        o += b;
      }
    }
    pinMode(PPS_PIN, INPUT_PULLDOWN);
    pps_resync = true; pps_resyncs++; fs_clean_secs = 0;
    attachInterrupt(digitalPinToInterrupt(PPS_PIN), pps_isr, RISING);
    (void)watching;
    http.send(200, "text/plain", o);
  });
  http.on("/format", HTTP_POST, []() {
    // Both nodes shipped on Raspberry Pi boot cards, so the ESP32 only ever sees the small FAT
    // partition the Pi put there -- 40 MB on nyquist, about half of it kernel images that will
    // never be read again. scene.csv costs 0.8 MB/h, so a card's useful life is hours rather than
    // weeks purely because of how it was partitioned.
    //
    // f_mkfs rewrites the PARTITION TABLE, not just the volume: FM_ANY without FM_SFD lays down a
    // fresh MBR with one FAT partition spanning the whole device, so the Pi boot partition and its
    // ext4 root both go and the card comes back at its real capacity.
    //
    // Guarded by the node's OWN NAME rather than confirm=yes, because there are two of these on
    // similar addresses. A typo should cost you an error, not a night.
    if (http.arg("confirm") != String(node_id)) {
      char b[440];
      snprintf(b, sizeof b,
        "POST /format?confirm=%s\n\n"
        "DESTROYS EVERYTHING ON THE CARD, partition table included. Drain first:\n"
        "  curl 'http://%s.local/sd?file=/scene.csv' -o scene.csv   (and dets, health, *-prev)\n\n"
        "card now: %lu MB free of %lu MB total\n"
        "That total is small because of the Pi partitioning; this is what fixes it.\n",
        node_id, node_id,
        (unsigned long)(sd_ok ? (SD.totalBytes() - SD.usedBytes()) / 1048576UL : 0UL),
        (unsigned long)(sd_ok ? SD.totalBytes() / 1048576UL : 0UL));
      http.send(400, "text/plain", b);
      return;
    }
    if (!sd_ok) { http.send(503, "text/plain", "no card mounted\n"); return; }

    uint64_t before = SD.totalBytes();
    // Every handle must be shut first, or FatFs flushes a dirty FAT onto a volume that is about
    // to stop existing.
    if (detf) { detf.flush(); detf.close(); }
    if (scenef) { scenef.flush(); scenef.close(); }
    logln("sd    FORMAT requested -- closing files and unmounting");
    // f_mkfs blocks loop() for seconds, so PPS edges pass uncounted and the interval spanning the
    // format lands in pps_int_max. Same discard the pin probes needed: mach came back from its
    // format reading a 46 us spread against the 3-4 us it actually holds.
    pps_resync = true; pps_resyncs++; fs_clean_secs = 0;

    BYTE *work = (BYTE *)malloc(FF_MAX_SS);
    if (!work) { http.send(500, "text/plain", "no memory for the mkfs work buffer\n"); return; }
    f_mount(NULL, "0:", 0);                        // drop the volume; the diskio driver stays
    MKFS_PARM opt = {(BYTE)FM_ANY, 0, 0, 0, 0};    // no FM_SFD -> keep an MBR, one full-size part
    FRESULT r = f_mkfs("0:", &opt, work, FF_MAX_SS);
    free(work);

    SD.end();                                      // remount by the same path setup() uses, so
    sd_ok = false; sd_cs = 0;                      // there is only one way a card gets mounted
    if (SD.begin(21, SPI, 20000000, "/sd", 8)) { sd_ok = true; sd_cs = 21; }
    else if (SD.begin(3, SPI, 20000000, "/sd", 8)) { sd_ok = true; sd_cs = 3; }

    det_flushed = det_n;                           // these counted a card that no longer exists
    scene_rows = 0; scene_written = 0;
    det_write_fail = 0; scene_write_fail = 0;
    clip_written = 0; clip_skip_full = 0; clip_budget_left = CLIP_BUDGET_B;

    char b[440];
    snprintf(b, sizeof b,
      "f_mkfs -> %s (%d)\nremount: %s\n\nbefore: %llu MB total\nafter:  %llu MB total, %llu MB free\n\n%s\n",
      r == FR_OK ? "OK" : "FAILED", (int)r, sd_ok ? "mounted" : "FAILED TO MOUNT",
      (unsigned long long)(before / 1048576ULL),
      (unsigned long long)(sd_ok ? SD.totalBytes() / 1048576ULL : 0ULL),
      (unsigned long long)(sd_ok ? (SD.totalBytes() - SD.usedBytes()) / 1048576ULL : 0ULL),
      (r == FR_OK && sd_ok)
        ? "csv_open recreates health.csv, dets.csv and scene.csv with headers on the next write."
        : "If the card did not come back it needs a reader. This is the one operation a reflash "
          "cannot undo.");
    logln(b);
    http.send(r == FR_OK && sd_ok ? 200 : 500, "text/plain", b);
  });
  http.on("/ppsv", []() {
    // Sweep the pull modes as well as reading the voltage, because the obvious way for THIS
    // firmware to be the fault is for its own ~45k pulldown to be flattening a weak or
    // high-impedance source -- in which case the signal is real and I am destroying it before
    // measuring it. A source that can drive the pin against a pulldown will show the same swing
    // in all three modes; one that cannot will show a swing with the pulldown off and none with
    // it on, and that difference is the whole question.
    // ?pin=N selects which pad to look at. Restricted to the four FREE ones: probing a driven
    // output measures this node rather than the world, and repurposing one mid-run would
    // interrupt the SD card or the microphone.
    int pin = http.hasArg("pin") ? http.arg("pin").toInt() : PPS_PIN;
    if (pin != 1 && pin != 2 && pin != 3 && pin != 4) {
      http.send(400, "text/plain",
                "pin must be a free pad: 1 (D0), 2 (D1), 3 (D2) or 4 (D3).\n"
                "Everything else is driven by this build -- see /pins.\n");
      return;
    }
    if (pin == PPS_PIN) detachInterrupt(digitalPinToInterrupt(PPS_PIN));
    analogSetPinAttenuation(pin, ADC_11db);
    String o = "GPIO" + String(pin) + ", 800 ms per mode\n\n";
    o += "mode      n      min V   max V   mean V  swing   digital-high\n";
    const int modes[3] = {INPUT_PULLDOWN, INPUT, INPUT_PULLUP};
    const char *mn[3] = {"pulldown", "float   ", "pullup  "};
    float best_swing = 0;
    for (int m = 0; m < 3; m++) {
      pinMode(pin, modes[m]);
      delay(30);
      uint32_t n = 0, lo = 4095, hi = 0, dh = 0; uint64_t sum = 0;
      uint32_t t0 = millis();
      while (millis() - t0 < 800) {
        uint32_t v = analogRead(pin);
        if (v < lo) lo = v; if (v > hi) hi = v; sum += v; n++;
        if (digitalRead(pin)) dh++;
      }
      double k = 3300.0 / 4095.0 / 1000.0;
      float sw = (float)((hi - lo) * k);
      if (sw > best_swing) best_swing = sw;
      char b[160];
      snprintf(b, sizeof b, "%s  %-6lu %6.2f  %6.2f  %6.2f  %6.2f  %5.1f%%\n",
               mn[m], (unsigned long)n, lo * k, hi * k, (double)sum / n * k, sw,
               100.0 * dh / n);
      o += b;
    }
    pinMode(PPS_PIN, INPUT_PULLDOWN);
    pps_resync = true; pps_resyncs++; fs_clean_secs = 0;
    if (pin == PPS_PIN) attachInterrupt(digitalPinToInterrupt(PPS_PIN), pps_isr, RISING);
    o += "\n";
    o += best_swing < 0.25
       ? "NO SWING IN ANY MODE. Whatever is on this pin is static, and it is static with the\n"
         "pulldown removed as well -- so this firmware is not the thing flattening it.\n"
       : "A SWING APPEARS. Compare the modes above: if it is present with the pulldown off and\n"
         "absent with it on, the source cannot drive against 45k and the pulldown was the fault.\n";
    o += "\nThe firmware still listens for PPS on GPIO" + String(PPS_PIN) + ". Measuring another\n"
         "pin does not move it: that is a rebuild, worth doing once a pin is shown to carry the\n"
         "pulse. Control: nyquist runs this same build and counts real edges on GPIO1.\n";
    http.send(200, "text/plain", o);
  });
  http.on("/tplen", HTTP_POST, []() {
    // POST, not GET: this changes hardware state, and /reboot is POST for the same reason.
    if (!http.hasArg("ms")) {
      http.send(400, "text/plain",
                "POST /tplen?ms=<10..900>   set the timepulse length, RAM layer\n"
                "POST /tplen?ms=100         back to the shipped value\n\n"
                "500 ms is a square wave: a meter reads ~1.65 V on the switched leg against 3.3 V\n"
                "on the supply leg, which tells the two apart without a scope. RAM only, so a\n"
                "power cycle restores 100 ms regardless.\n");
      return;
    }
    long ms = http.arg("ms").toInt();
    // Bounded well inside the 1 s period: a length at or past the period is not a pulse, and a
    // module asked for one can stop toggling altogether -- which would look exactly like the
    // fault being chased.
    if (ms < 10 || ms > 900) { http.send(400, "text/plain", "ms must be 10..900\n"); return; }
    gps_set_pulse_len((uint32_t)ms * 1000UL);
    strcpy(tp_readback, "(no response)");
    uint32_t t0 = millis();
    while (millis() - t0 < 600) { while (Serial1.available()) ubx_feed((uint8_t)Serial1.read()); }
    gps_valget();
    t0 = millis();
    while (millis() - t0 < 600) { while (Serial1.available()) ubx_feed((uint8_t)Serial1.read()); }
    char b[320];
    snprintf(b, sizeof b, "asked for %ld ms\nmodule now reports: %s\n\n"
                          "check the pin with: curl http://%s.local/pps\n",
             ms, tp_readback, node_id);
    http.send(200, "text/plain", b);
  });
  http.on("/tp", []() {
    strcpy(tp_readback, "(no response)");
    gps_valget();
    // The UBX parser lives in loop(), which is blocked while this handler runs -- so pump the
    // port here instead of delay()ing and wondering why nothing arrived.
    uint32_t t0 = millis();
    while (millis() - t0 < 900) { while (Serial1.available()) ubx_feed((uint8_t)Serial1.read()); }
    http.send(200, "text/plain", String(tp_readback) + "\n");
  });
  http.on("/pps", []() {              // live probe: wire the tap, hit this, no reboot needed
    detachInterrupt(digitalPinToInterrupt(PPS_PIN));
    pinMode(PPS_PIN, INPUT_PULLDOWN);
    int high = 0, n = 0, edges = 0, last = digitalRead(PPS_PIN);
    uint32_t t0 = millis();
    while (millis() - t0 < 2500) { int v = digitalRead(PPS_PIN); if (v) high++; n++; if (v != last) { edges++; last = v; } }
    pps_resync = true; pps_resyncs++; fs_clean_secs = 0;
    attachInterrupt(digitalPinToInterrupt(PPS_PIN), pps_isr, RISING);
    char b[300];
    snprintf(b, sizeof b,
      "pin D0/GPIO%d over 2.5 s\n  edges   %d\n  high    %.1f%% of samples\n  verdict %s\n\n"
      "expect ~2-3 edges and ~10%% high for a 1 Hz / 100 ms pulse.\n"
      "flat at 0%%  = nothing driving the pin (tap not connected, or wrong side of the LED)\n"
      "flat at 100%% = tap sits on a rail, not the switched end\n",
      PPS_PIN, edges, n ? 100.0 * high / n : 0.0,
      edges ? "PULSING" : (high > n / 2 ? "STUCK HIGH" : "FLAT LOW"));
    http.send(200, "text/plain", b);
  });
  http.on("/gpsraw", []() {           // what the module is ACTUALLY sending, not what a parser counted
    String o = "bytes=" + String((unsigned long)raw_tot) + " valid_nmea_lines=" + String((unsigned long)nmea_valid) +
               " ubx_ack=" + String((unsigned long)ubx_ack) + " ubx_nak=" + String((unsigned long)ubx_nak) + "\n\n";
    uint16_t st = raw_i;
    // ?hex=1: '.' for every non-printable byte hides exactly the structure worth looking for --
    // a UBX sync pair (b5 62), an NMEA '$' at the wrong framing, or a line stuck at one value.
    bool as_hex = http.hasArg("hex");
    for (uint16_t k = 0; k < sizeof(rawbuf); k++) {
      char c = (char)rawbuf[(st + k) % sizeof(rawbuf)];
      if (as_hex) {
        static const char hx[] = "0123456789abcdef";
        o += hx[(uint8_t)c >> 4]; o += hx[(uint8_t)c & 0xF]; o += ' ';
        if ((k & 31) == 31) o += '\n';
      } else {
        o += (c >= 32 && c < 127) ? c : (c == '\n' ? '\n' : '.');
      }
    }
    http.send(200, "text/plain", o);
  });
  // FORCE the pin order when the module cannot be measured.
  //
  // gps_pick_pins() decides by watching which pin carries a transmitter, so it can only decide
  // while the module is TALKING. That is a deadlock for any node wired against the documented
  // order: mach's pair is reversed, and when its module came up silent the probe fell back to the
  // documented pinout, which put the ESP's TX into the module's TX. Nothing could then be sent to
  // wake it, so the auto-detect could never succeed, and the watchdog retried the same wrong guess
  // every two minutes -- 0 sentences, config_acked false, PPS still ticking, indefinitely.
  //
  // Autodetect is the right default and stays the default. This is the manual override for the
  // case it cannot cover, and it is why a node whose GPS goes quiet is now recoverable over the
  // network instead of needing someone outdoors.
  //
  //   curl -X POST 'http://<node>/gpspins?swap=1'   # module TX on D6, RX on D7 (mach's wiring)
  //   curl -X POST 'http://<node>/gpspins?swap=0'   # documented order
  //   curl -X POST 'http://<node>/gpspins?auto=1'   # hand it back to the probe
  http.on("/gpspins", HTTP_POST, []() {
    if (!http.hasArg("swap") && !http.hasArg("auto")) {
      http.send(400, "text/plain", "POST /gpspins?swap=0|1 | ?auto=1\n");
      return;
    }
    // Re-running bring-up drops the timepulse while it sweeps, and an interval spanning that gap
    // is not a real one. Declare it, exactly as the watchdog path does, or a diagnostic shows up
    // as a multi-second PPS interval that the high-water mark then keeps for the whole run.
    pps_resync = true; pps_resyncs++; fs_clean_secs = 0;
    pps_int_min = 0xFFFFFFFF; pps_int_max = 0;

    if (http.hasArg("auto")) {
      gps_bringup();
    } else {
      bool swap = http.arg("swap").toInt() != 0;
      Serial1.end();
      gps_rx_pin = swap ? GPS_TX : GPS_RX;
      gps_tx_pin = swap ? GPS_RX : GPS_TX;
      gps_pin_src = swap ? "FORCED: swapped at the module" : "FORCED: as documented";
      // Sweep the baud on the pins we were just told to use, then configure. Forcing the pins
      // without re-finding the baud would leave the UART open at whatever the failed autodetect
      // settled on -- 9600, on mach -- and the override would look like it did nothing.
      gps_autobaud();
      gps_configure();
    }
    char b[220];
    snprintf(b, sizeof b, "%s\nRX=GPIO%d TX=GPIO%d at %lu baud\n"
             "watch /status: gps.sentences and gps.config_acked say whether it took.\n",
             gps_pin_src, gps_rx_pin, gps_tx_pin, (unsigned long)gps_baud);
    http.send(200, "text/plain", b);
  });

  http.on("/gpsbaud", []() {
    // Detaching the UART for the measurement and putting it straight back: the pin is shared, and
    // a diagnostic that leaves the GPS silent afterwards would be worse than no diagnostic.
    Serial1.end();
    uint32_t rawmin = 0;
    uint32_t bit_us = gps_bit_time_us(gps_rx_pin, 400, NULL, &rawmin);
    uint32_t other_us = gps_min_pulse_us(gps_rx_pin == GPS_RX ? GPS_TX : GPS_RX, 250);
    // The UART is the authority on whether the link works. This endpoint measures the wire, and
    // where the two disagree the decoded traffic wins and the measurement is reported as suspect.
    bool decoding = nmea_valid > 0 || ubx_pvt > 0;
    float err = 0; uint32_t snap = gps_snap_baud(bit_us, &err);
    Serial1.begin(gps_baud, SERIAL_8N1, gps_rx_pin, gps_tx_pin);
    char b[700];
    snprintf(b, sizeof b,
      "pins     %s\n"
      "listen   GPIO%d   talk GPIO%d\n"
      "other    GPIO%d min run %lu us %s\n"
      "bit time %lu us  (shortest run seen at least %d times in 400 ms)\n"
      "raw min  %lu us  (single shortest run -- a glitch moves this and not the figure above)\n"
      "implies  %.0f baud\n"
      "nearest  %lu baud, %.1f%% away\n"
      "decoding %s\n\n"
      "%s\n"
      "currently open at %lu baud.\n",
      gps_pin_src, gps_rx_pin, gps_tx_pin,
      gps_rx_pin == GPS_RX ? GPS_TX : GPS_RX, (unsigned long)other_us,
      other_us ? "<-- A TRANSMITTER IS ON THE OTHER PIN. The pair is reversed." : "(idle, as expected)",
      (unsigned long)bit_us, GPS_RUN_QUORUM, (unsigned long)rawmin,
      bit_us ? 1e6 / (double)bit_us : 0.0, (unsigned long)snap, err,
      decoding ? "YES -- the UART is producing valid sentences right now" : "no",
      decoding ? "Link is up; treat any mismatch above as a limit of this measurement, not of the\n"
                 "link. digitalRead sampling cannot resolve a bit time reliably much under 10 us."
      : !bit_us ? "NOTHING RECURRED. The line is idle or only glitching: module not powered, not\n"
                  "transmitting, or the wire is on the module's RX rather than its TX."
      : err < 15.0 ? "Plausible rate and nothing decoding, so suspect framing rather than speed --\n"
                     "inverted logic, or not 8N1."
      : "No standard rate fits and nothing is decoding. Check the wiring before the settings.",
      (unsigned long)gps_baud);
    http.send(200, "text/plain", b);
  });
  http.on("/i2c", []() { i2c_scan(); http.send(200, "text/plain", i2c_found); });   // rescan on demand
  // GET reads the floor, POST sets it. Two registrations, two methods -- and NOT two of the same
  // method: this file already registers /tp twice with HTTP_ANY, and core 3.0.5's Parsing.cpp
  // takes the FIRST handler that canHandle()s, so the second one is unreachable dead code with no
  // diagnostic. WebServer's FunctionRequestHandler compares the method, so GET and POST on one
  // URI are two distinct handlers and neither shadows the other.
  http.on("/gate", HTTP_GET,  []() { http.send(200, "application/json", gate_json()); });
  // POST, for the same reason /reboot is POST: a link prefetcher must not be able to deafen the
  // node, and watch.py polls this endpoint's neighbours unattended every 30 s.
  //   curl -X POST 'http://<ip>/gate?floor=300'              # this boot only
  //   curl -X POST 'http://<ip>/gate?floor=300&persist=1'    # and across reboots
  //   curl -X POST 'http://<ip>/gate?reset=1'                # back to the compiled-in default
  http.on("/gate", HTTP_POST, []() {
    if (http.hasArg("reset")) {
      g_floor = FLOOR_DEFAULT; g_floor_src = "default";
      if (sd_ok) SD.remove(GATE_CFG);
      g_floor_saved = NAN;
      logf("gate  floor reset to %.0f\n", g_floor);
    } else if (http.hasArg("floor")) {
      float want = strtof(http.arg("floor").c_str(), NULL);
      g_floor = gate_floor_clamp(want);
      g_floor_src = "http";
      if (http.hasArg("persist") && !gate_floor_persist())
        logln("gate  could NOT write " GATE_CFG " -- the floor is this boot only");
      // Log what was ASKED for as well as what took effect. A request clamped from 20 to 100 that
      // logged only "100" would read as an operator who typed 100.
      logf("gate  floor %.0f (asked %.0f)%s\n", g_floor, want,
           http.hasArg("persist") ? ", persisted" : ", this boot only");
    } else {
      http.send(400, "application/json",
                "{\"error\":\"POST /gate?floor=<n> | ?floor=<n>&persist=1 | ?reset=1\"}");
      return;
    }
    http.send(200, "application/json", gate_json());
  });
  http.on("/audio", []() {
    // Bare /audio answers "what is retrievable?" so a caller never has to guess a window; with
    // ?from=<utc_us>&dur=<s> it returns that window as a playable WAV.
    double   fsu = fs_clean > 1000.0 ? fs_clean : (double)FS_NOMINAL;
    uint32_t newest = g_samples;
    uint32_t lo = praw_oldest();
    // A flat 16 s guard would swallow half of a 30 s fallback ring, so cap it at a third.
    if (praw_cap && newest > praw_cap) {
      uint32_t g = (uint32_t)AUDIO_GUARD_S * FS_NOMINAL;
      if (g > praw_cap / 3) g = praw_cap / 3;
      lo += g;
    }
    int64_t lo_utc = 0, hi_utc = 0;
    bool span_ok = praw && sample_to_utc(lo, &lo_utc) && sample_to_utc(newest, &hi_utc);
    if (!http.hasArg("from")) {
      char b[512];
      snprintf(b, sizeof b,
        // addressable_samples, NOT held_samples: /status reports everything the ring holds, this
        // reports what is safe to ASK for, which is smaller by the overwrite guard. Same quantity
        // under one name in two places would be a label that drifts from what it describes.
        "{\"ring\":%s,\"span_s\":%.1f,\"cap_samples\":%lu,\"addressable_samples\":%lu,"
        "\"fs_hz\":%.4f,\"addressable\":%s,\"from_utc_us\":%lld,\"to_utc_us\":%lld,"
        "\"max_dur_s\":%d,\"usage\":\"/audio?from=<utc_us>&dur=<seconds>\"}",
        praw ? "true" : "false",
        praw_cap ? (double)praw_cap / fsu : 0.0, (unsigned long)praw_cap,
        (unsigned long)(praw_cap && newest > lo ? newest - lo : 0), fsu,
        span_ok ? "true" : "false", (long long)lo_utc, (long long)hi_utc, AUDIO_MAX_S);
      http.send(200, "application/json", b);
      return;
    }
    if (!praw) { http.send(503, "text/plain", "no PSRAM ring: allocation failed at boot\n"); return; }
    if (!span_ok) {
      http.send(503, "text/plain", "no PPS/UTC anchor yet -- the ring cannot be addressed by time\n");
      return;
    }
    int64_t from = strtoll(http.arg("from").c_str(), NULL, 10);
    float dur = http.hasArg("dur") ? http.arg("dur").toFloat() : 5.0f;
    if (!(dur > 0.0f)) dur = 5.0f;
    if (dur > (float)AUDIO_MAX_S) dur = (float)AUDIO_MAX_S;
    uint32_t s0;
    if (!utc_to_sample(from, &s0)) { http.send(400, "text/plain", "cannot map that time\n"); return; }
    // Clamp to what is actually held and SAY SO in a header. Silently returning a shorter file is
    // how a caller ends up believing it has audio either side of an event that it does not have.
    int64_t want0 = (int64_t)s0, want1 = want0 + (int64_t)llrint((double)dur * fsu);
    int64_t got0 = want0 < (int64_t)lo ? (int64_t)lo : want0;
    int64_t got1 = want1 > (int64_t)newest ? (int64_t)newest : want1;
    const char *clip = (got0 > want0) ? (got1 < want1 ? "both" : "head")
                                      : (got1 < want1 ? "tail" : "none");
    if (got1 <= got0) {
      http.sendHeader("X-Audio-Clipped", "all");
      http.send(416, "text/plain", "that window is not in the ring\n");
      return;
    }
    uint32_t nout = (uint32_t)(got1 - got0);
    int64_t out_utc = 0; sample_to_utc((uint32_t)got0, &out_utc);
    char v[48];
    snprintf(v, sizeof v, "%lld", (long long)out_utc); http.sendHeader("X-Audio-From-Utc-Us", v);
    snprintf(v, sizeof v, "%lu", (unsigned long)nout); http.sendHeader("X-Audio-Samples", v);
    snprintf(v, sizeof v, "%.4f", fsu);                http.sendHeader("X-Audio-Fs-Hz", v);
    http.sendHeader("X-Audio-Clipped", clip);
    snprintf(v, sizeof v, "%lld", (long long)lo_utc);  http.sendHeader("X-Audio-Ring-From-Utc-Us", v);
    snprintf(v, sizeof v, "%lld", (long long)hi_utc);  http.sendHeader("X-Audio-Ring-To-Utc-Us", v);
    http.sendHeader("Content-Disposition", "inline; filename=\"audio.wav\"");
    // Content-Length is the header plus exactly the samples being sent. streamFile() advertising
    // f.size() regardless of a seek is what made /sd promise a whole file and deliver a fragment;
    // the same mistake here would be a WAV whose header and body disagree.
    http.setContentLength(44 + (size_t)nout * 2);
    http.send(200, "audio/wav", "");
    WiFiClient c = http.client();
    uint8_t hdr[44];
    wav_header(hdr, nout * 2, (uint32_t)lrint(fsu));
    c.write(hdr, sizeof hdr);
    static uint8_t obuf[AUDIO_CHUNK_B];
    uint64_t t_prev = (uint64_t)esp_timer_get_time();
    uint32_t s = (uint32_t)got0, left = nout;
    while (left && c.connected()) {
      uint32_t nsamp = left > AUDIO_CHUNK_B / 2 ? AUDIO_CHUNK_B / 2 : left;
      uint32_t idx = s % praw_cap;
      uint32_t run = praw_cap - idx; if (run > nsamp) run = nsamp;
      memcpy(obuf, praw + idx, (size_t)run * 2);
      if (run < nsamp) memcpy(obuf + run * 2, praw, (size_t)(nsamp - run) * 2);
      if (c.write(obuf, (size_t)nsamp * 2) != (size_t)nsamp * 2) break;
      s += nsamp; left -= nsamp;
      // The mic does not stop for a download. Drain by ELAPSED TIME rather than one block per
      // chunk: a block is 16 ms of audio, so a fixed one-per-chunk only keeps up above roughly
      // 256 kB/s, and below that the DMA's 90 ms overruns after a few chunks and stays overrun
      // for the rest of the download -- losing more audio than the whole night did. Capped at
      // the DMA depth, because past that the samples are already gone and blocking here to ask
      // for them would only widen the hole.
      uint64_t t_now = (uint64_t)esp_timer_get_time();
      uint32_t due = (uint32_t)((((t_now - t_prev) * 2ULL) / 125ULL) / (uint32_t)BLOCK);
      if (due < 1) due = 1;
      if (due > AUDIO_PUMP_MAX) due = AUDIO_PUMP_MAX;
      for (uint32_t k = 0; k < due; k++) audio_pump();
      t_prev = (uint64_t)esp_timer_get_time();
    }
  });
  fft_init();
  http.begin();
  logln("http  up\n");
}

// ---------------------------------------------------------------- detections -> card
// The ring is 128 deep and the card is the only thing that survives the timer cutting power, so
// the ring is a staging area, not the record. The file is held OPEN and flushed in batches:
// open/write/close per detection costs 20-50 ms inside loop(), which stalls the I2S reader and
// drops the very audio we are here to capture. Whatever that flush still costs now shows up in
// drop_seconds, so the cost is measured rather than assumed.
// clip and clip_why are appended AFTER frame_hex, not inserted: tools/hear_bridge.py checks this
// header as a prefix and documents trailing columns as the supported way to grow it. clip holds
// the path of a WAV that IS on the card, or nothing; clip_why says which of the seven outcomes
// happened, so an empty clip column is never ambiguous between "quiet", "budget spent" and "card
// full". A row is not written until its clip has resolved, so the column never names a file that
// does not exist -- see clip_pump() and the wait in det_flush.
// ⚠️`node_id` AND NOT `node`, WHICH scene.csv USES, DELIBERATELY. The old header said `node`
// and the row never wrote it -- 12 columns declared, 11 written, so every field a consumer read
// by name was shifted one left and the missing one was the column saying WHICH NODE the row came
// from, in the file that exists for multi-node TDoA. csv_open() rolls a file aside only when the
// header STRING changes, so keeping the name would have appended 12-column rows under the same
// header as the 11-column ones already on the card. Renaming forces the roll. Pulled from
// nyquist and mach on 2026-09-08: 147 and 268 rows, every one 11 wide.
// ⚠️`sketch_back` IS IN THE HEADER SO A ROW SAYS WHERE ITS OWN WINDOW STARTED. The frame's
// meaning changed when the window went from [T-46 ms, T-2 ms] to running forward from one hop
// before the trigger, and nothing in a row recorded which convention produced it -- so new-window
// frames would have appended under the same header as old-window ones, indistinguishable. That is
// the ambiguity csv_open's roll exists to prevent, and a changed header string is what triggers
// it. Recording the value rather than bumping a version number also makes the NEXT window change
// visible in the data instead of only in the firmware.
static const char DETS_HDR[] =
  "node_id,utc_us,uptime_s,sample,pps_n,us_since_pps,trigger,flags,fs_hz,sketch_back,frame_hex,"
  "clip,clip_why";

// ⚠️THE SKETCH IS TAKEN HERE, NOT AT THE GATE EDGE, BECAUSE THE AUDIO DOES NOT EXIST YET.
// The window runs forward from one hop before the trigger, so it needs SKETCH_SPAN - SKETCH_BACK
// samples that have not been captured when the gate fires. Same deferral, and the same wait
// idiom, as clip_pump.
//
// In order, and it RETURNS rather than continues on the first not-ready detection: dets are
// flushed in order and det_flush refuses to write a row whose sketch is still pending, so
// skipping ahead would stall the queue behind a row that can never complete.
static void sketch_pump() {
  // Slots recycle at MAXDET. Anything older than that is already gone; walking to it would
  // sketch whatever occupies the slot now -- the same guard det_flush keeps.
  uint32_t k0 = det_flushed;
  if (det_n - det_flushed > MAXDET) k0 = det_n - MAXDET;
  for (uint32_t k = k0; k < det_n; k++) {
    Det &d = dets[k % MAXDET];
    if (d.sk_st) continue;
    if ((int32_t)(g_samples - (d.sample + (SKETCH_SPAN - SKETCH_BACK))) < 0) {
      // not yet. If it never comes -- capture stalled -- the row must still be able to leave.
      if ((millis() - boot_ms) / 1000 - d.uptime_s > CLIP_WAIT_MAX_S) {
        d.flags |= 0x0002; d.sk_st = 1;
        memset(d.frame, 0, sizeof d.frame);
      }
      return;
    }
    uint32_t start = d.sample - SKETCH_BACK;
    // The ring holds ARING samples. If it lapped the window while we waited, the sketch would be
    // of whatever is there now -- say so rather than ship it as a measurement.
    if ((uint32_t)(aring_total - start) > (uint32_t)ARING) d.flags |= 0x0002;
    sketch_frame(d.frame, start, (uint32_t)d.us_since_pps,
                 (uint16_t)abs((int)d.trigger), d.flags);
    d.sk_st = 1;
  }
}

static void det_flush() {
  if (!sd_ok || det_flushed == det_n) return;
  // Peek before opening anything. The oldest unflushed detection blocks the whole batch while its
  // clip resolves, and reaching csv_open only to break out of the write loop would pay a 20-50 ms
  // open once a second for as long as that lasts.
  { uint32_t f0 = det_flushed;
    if (det_n > MAXDET && det_n - MAXDET > f0) f0 = det_n - MAXDET;
    if (dets[f0 % MAXDET].clip_st == CLIP_PENDING || !dets[f0 % MAXDET].sk_st) return; }
  if (!detf) {
    // The old det_hdr_done latch is gone: it was set even when the header had NOT been written,
    // so a card swapped mid-run could never get one.
    detf = csv_open("/dets.csv", "/dets-prev.csv", DETS_HDR);
    if (!detf) return;
  }
  // If more than MAXDET landed since the last flush, the oldest slots have already been
  // overwritten. Count them as lost instead of writing whatever occupies the slot now.
  uint32_t first = det_flushed;
  if (det_n - det_flushed > MAXDET) { first = det_n - MAXDET; det_lost += first - det_flushed; }
  uint32_t wrote = 0;
  static const char hx[] = "0123456789abcdef";
  for (uint32_t k = first; k < det_n && wrote < 16; k++, wrote++) {
    const Det &d = dets[k % MAXDET];
    // A row whose clip has not resolved yet WAITS -- writing it now would either name a file that
    // may never appear or record "no clip" for one that is about to. Bounded by CLIP_WAIT_MAX_S,
    // so the delay a detection can suffer is ~10 s against the 1 s it used to be; the cost is
    // that a power cut inside that window loses the row, and det_n vs det_written in health.csv
    // is where that would show. Break, not continue: the file is append-only and in-order.
    if (d.clip_st == CLIP_PENDING || !d.sk_st) break;
    char line[MEL16_FRAME_BYTES * 2 + 224];
    int m = snprintf(line, sizeof line, "%s,%lld,%lu,%lu,%lu,%ld,%d,%u,%.3f,%lu,",
                     node_id,
                     (long long)d.utc_us, (unsigned long)d.uptime_s, (unsigned long)d.sample,
                     (unsigned long)d.pps_n, (long)d.us_since_pps, d.trigger,
                     (unsigned)d.flags, d.fs_at, (unsigned long)SKETCH_BACK);
    for (int j = 0; j < MEL16_FRAME_BYTES && m < (int)sizeof line - 64; j++) {
      line[m++] = hx[d.frame[j] >> 4]; line[m++] = hx[d.frame[j] & 0xF];
    }
    char cp[48] = "";
    if (d.clip_st == CLIP_OK) clip_name(cp, sizeof cp, d.sample);
    int add = snprintf(line + m, sizeof line - m, ",%s,%s", cp, clip_why(d.clip_st));
    if (add > 0) m += (add < (int)sizeof line - m) ? add : ((int)sizeof line - m - 1);
    line[m++] = '\n';
    if (detf.write((const uint8_t *)line, m) != (size_t)m) {
      // Close, so the next flush reopens. A short write that leaves the handle open turns a
      // transient card error into a permanent, silent stop -- the failure would be counted but
      // never recovered from, and the rest of the night would still be lost.
      det_write_fail++; detf.close(); break;
    }
    det_flushed = k + 1;      // only advance on a write that actually landed
  }
  detf.flush();          // commit: the plug timer can cut power between any two loop iterations
}

static int16_t blk[BLOCK];
// The DC-blocked copy of the same block, kept contiguous: the PSRAM ring then takes one memcpy
// instead of a modulo and an uncached external-RAM store per sample, and the scene FFT has a
// whole frame to read without walking aring's wrap.
static int16_t dcblk[BLOCK];

// One I2S block: DC-block, ring, gate, sketch, raw ring, scene frame. Factored out of loop()
// because /audio has to keep calling it while it streams -- the I2S DMA is 6 x 240 frames = 90 ms
// and a handler that does not drain it loses audio.
static void audio_pump() {
  { size_t got = i2s.readBytes((char *)blk, sizeof blk);
    int n = got / 2;
    // Seed the pedestal from the first block rather than ramping to it from zero, which would
    // otherwise look like a huge transient and fire the gate on every boot.
    if (!dc_ready && n > 0) {
      float m = 0; for (int i = 0; i < n; i++) m += (float)blk[i];
      sig_dc = m / (float)n; dc_ready = true;
    }
    for (int i = 0; i < n; i++) {
      sig_dc += ALPHA_DC * ((float)blk[i] - sig_dc);
      int32_t v = (int32_t)lrintf((float)blk[i] - sig_dc);
      int16_t sac = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
      aring[aring_w] = sac; aring_w = (aring_w + 1) % ARING; aring_total++;
      dcblk[i] = sac;
      int fired = gate(sac);                    // stateful: exactly one call per sample
      if (fired) {
        uint32_t idx = (det_n++) % MAXDET;
        {
          // The block arrives as a unit, so reading the clock here stamps every sample in it
          // with the moment the block FINISHED. At BLOCK=256 that is up to 15.9 ms late -- 87x
          // the 183 us budget, and it throws away the 21 ns the GPS is handing us. Back-date by
          // the samples still to come, at the PPS-disciplined rate rather than the 16 kHz
          // nominal (which is out by ~5600 ppm).
          double   fsu     = fs_clean > 1000.0 ? fs_clean : (double)FS_NOMINAL;
          uint32_t back_us = (uint32_t)((double)(n - 1 - i) * 1e6 / fsu + 0.5);
          uint64_t cap_us  = (uint64_t)esp_timer_get_time() - back_us;
          int64_t  off     = (int64_t)cap_us - (int64_t)pps_us_last;
          uint32_t pn      = pps_count;
          if (!pn) off = 0;    // pre-lock: there is no edge to be offset FROM. utc_us stays 0.
          else if (off < 0 && pn > 1) { pn--; off = (int64_t)cap_us - (int64_t)pps_us_prev; }
          int64_t t = 0; bool tok = local_to_utc(cap_us, &t);
          dets[idx].sample = g_samples + i;
          dets[idx].pps_n = pn;
          dets[idx].us_since_pps = (int32_t)off;
          dets[idx].utc_us = tok ? t : 0;       // 0 = the anchor was not trusted at that instant
          dets[idx].trigger = sac;
          dets[idx].fs_at = fsu;
          dets[idx].uptime_s = (millis() - boot_ms) / 1000;
          // The slot is recycled, so this must be set here and not left over from whatever
          // detection used it 128 events ago -- a stale CLIP_OK would name a file for the wrong
          // event. Nothing is written from audio_pump(); clip_pump() picks it up from loop().
          dets[idx].clip_st = CLIP_PENDING;
          // ⚠️THE SKETCH IS QUEUED, NOT TAKEN HERE. The window has to run FORWARD from the
          // onset, and at this instant the post-onset audio does not exist yet -- the same
          // reason clip_pump is a deferred queue. sketch_pump() takes it once the audio lands.
          dets[idx].sk_st = 0;
          // ⚠️Before the ring has filled, the window behind the trigger is zeros, and a sketch of
          // silence is a legitimate-looking frame of all-equal bands. Flag it rather than ship a
          // number that means nothing. Bit 1 = insufficient context. (Bit 0 is retrigger.)
          //
          // MEL16_FLAG_BITS carries the rate code (bits 8-11) and the fixed-layout bit (12), both
          // generated alongside the filterbank by gen_mel.py so they cannot disagree with it.
          // Without them a frame does not say what band k MEANS: at 16 kHz band 12 is 3072 Hz
          // under the old rescaled bank and 5826 Hz under the shared axis, and a consumer had no
          // way to tell this node's frames from a 48 kHz phone's. Frames pulled from nyquist and
          // mach on 2026-09-08 all decoded as fs=None/layout=nyquist, so the shipped classifier
          // refused every one of them -- correctly, and uselessly.
          // Bit 0 = retrigger: this detection landed inside the previous one's decay, so it is
          // evidence about the site rather than a new event. It was DECLARED in the comment above
          // and never once set -- the flags histogram over 1167 field rows is exactly {0, 2}.
          // hear/node/detect.py has always packed it (RETRIGGER_S = 0.060 s).
          uint16_t fl = MEL16_FLAG_BITS;
          if (det_n >= 2) {
            const Det &prev = dets[(det_n - 2) % MAXDET];
            if ((uint32_t)(dets[idx].sample - prev.sample) < RETRIGGER_SAMPLES) fl |= 0x0001;
          }
          dets[idx].flags = fl;
        }
      }
      float a = fabsf((float)sac);
      if (a > env_peak_seen) env_peak_seen = a;
      if (a > env_peak_win) env_peak_win = a;
    }
    // The DC-BLOCKED samples go into the raw ring, not the raw ones: the pedestal drifted
    // 1093.9 -> 1439.6 over the night, so raw audio carries a moving offset a consumer would only
    // have to remove again, and the ring would not match what the gate and the sketch saw.
    // sig_dc is in health.csv if the pedestal is ever wanted back.
    if (praw && n > 0) {
      uint32_t w = g_samples % praw_cap;               // g_samples is this block's first sample
      uint32_t run = praw_cap - w; if (run > (uint32_t)n) run = (uint32_t)n;
      memcpy(praw + w, dcblk, (size_t)run * 2);
      if ((uint32_t)n > run) memcpy(praw, dcblk + run, (size_t)((uint32_t)n - run) * 2);
    }
    g_samples += n;
    blk_end_samp = g_samples; blk_end_us = (uint64_t)esp_timer_get_time();
    // One block IS one scene frame (BLOCK == MEL16_NFFT), so the 1.024 s descriptor is built
    // 16 ms at a time. A short read cannot be a frame; count it rather than pad it with silence.
    if (n == BLOCK) scene_frame(dcblk); else scene_short_blocks++; }
}

void loop() {
  http.handleClient();
  audio_pump();

  // ---- GPS link watchdog ---------------------------------------------------
  // The boot-time pin/baud detection is a measurement of a module that may not have started
  // transmitting yet, and it was never revisited. On mach -- the node whose TX/RX pair is
  // reversed -- an OTA reboot beat the module to the punch, the probe saw neither pin toggle,
  // fell back to the documented pinout mach does not have, and the node ran for 8 minutes with
  // fix 0 and ubx_pvt 0. It would have run all night.
  //
  // Retry only while NOTHING has ever decoded. Once a single sentence or UBX frame lands, the
  // link is proven and this never fires again -- so a node that is merely waiting for sky is
  // left alone, and a working node never pays the ~11 s the sweep costs.
  { static uint32_t last_try_ms = 0, gps_retries = 0;
    uint32_t up_ms = millis() - boot_ms;
    if (nmea_valid == 0 && ubx_pvt == 0 && up_ms > 45000UL &&
        (last_try_ms == 0 || millis() - last_try_ms > 120000UL)) {
      last_try_ms = millis();
      logf("gps   nothing decoded in %lus -- re-running bring-up (attempt %lu)\n",
           (unsigned long)(up_ms / 1000), (unsigned long)(++gps_retries));
      // The sweep detaches the UART and re-sends CFG-VALSET, which drops the timepulse for
      // ~11 s. Without this flag that gap is averaged in as a real PPS interval: mach came back
      // reporting a 3.95 SECOND spread and two glitches, which is a health metric reading as
      // catastrophic failure because of a diagnostic. Every probe route already declares its own
      // disturbance this way; this path is a probe too.
      pps_resync = true; pps_resyncs++; fs_clean_secs = 0;
      pps_int_min = 0xFFFFFFFF; pps_int_max = 0;
      gps_bringup();
      logf("gps   bring-up retry done: %s, RX=GPIO%d, %lu baud\n",
           gps_pin_src, gps_rx_pin, (unsigned long)gps_baud);
    }
  }
  // After audio_pump(), so the chunk written below is drained against a DMA that was emptied this
  // pass. One CLIP_CHUNK_B per iteration; the pump is the pacing, exactly as in /audio.
  clip_pump();

  // ---- one GPS second of audio, audited ------------------------------------
  // Each PPS edge is exactly one true second apart, so the samples between two edges ARE the
  // acquisition rate -- no reliance on the ESP crystal, whose +9 ppm would otherwise creep in.
  // A second that comes up short lost a block; it is counted and excluded, never averaged in.
  { static uint32_t seen_edge = 0, prev_sm = 0, win_sm0 = 0, win_e0 = 0;
    uint32_t e = pps_count;
    if (e != seen_edge) {
      uint32_t sm = pps_samp_last;
      if (seen_edge) {
        uint32_t d = sm - prev_sm;
        samp_sec_last = d;
        // Anchor the raw ring: a (UTC, sample) pair for the PREVIOUS edge, which local_to_utc
        // will only name once its NAV-PVT has landed -- and if it has not, the pair is skipped
        // rather than guessed. (I had a figure here for how late that report arrives; it was not
        // measured and is gone. The code never depended on it: local_to_utc either names the edge
        // or refuses.) Only when exactly one edge has passed, because a blocking handler can
        // straddle two, and then the timestamp and the sample count describe different seconds.
        if (e - seen_edge == 1) {
          // The ISR writes the timestamp and the sample index as a set. Read them, then re-read
          // the edge counter: if an edge landed between the two reads, the pair is mismatched by
          // a whole second -- 343 m -- so throw it away rather than record it.
          uint64_t mus = pps_us_prev;
          uint32_t msamp = pps_samp_prev_exact;
          if (pps_count == e) {
            int64_t mu;
            if (local_to_utc(mus, &mu)) {
              praw_mark[praw_mark_n % PRAW_MARKS].utc_us = mu;
              praw_mark[praw_mark_n % PRAW_MARKS].sample = msamp;
              praw_mark_n++;
            }
          }
        }
        // ⚠️AGAINST FS_NOMINAL, NOT fs_clean. Testing a second against the very average it
        // updates is a one-way latch: once fs_clean drifts high, every real second looks short,
        // every second takes the drop branch, fs_clean_secs is reset before it can reach the 8
        // it needs to recompute, and the value is stuck for the rest of the run. Measured on
        // mach: fs_clean_hz = 22624.0000 in 1124 of 1126 health rows, fs_win_s pinned at 0,
        // drop_samples 223,352,776 against ~223,209,000 predicted by the inflation itself, and
        // every clip written in that boot carrying a 22624 Hz WAV header over 16 kHz audio.
        if (d < (uint32_t)(0.97 * (double)FS_NOMINAL)) {
          drop_seconds++;
          drop_samples += (uint32_t)((double)FS_NOMINAL - (double)d);
          fs_clean_secs = 0;                      // a broken second cannot be averaged over
        } else {
          if (!fs_clean_secs) { win_sm0 = prev_sm; win_e0 = e - 1; }
          fs_clean_secs = e - win_e0;
          if (fs_clean_secs >= 8) {
            double fsc = (double)(sm - win_sm0) / (double)fs_clean_secs;
            // A crystal is tens of ppm from nominal, not percent. Anything outside 1% is a
            // counting fault, not a measurement, and must not become the divisor that dates
            // every sample, sizes every WAV header and scales every drop count.
            if (fsc > 0.99 * (double)FS_NOMINAL && fsc < 1.01 * (double)FS_NOMINAL) fs_clean = fsc;
            else fs_clean_secs = 0;
          }
        }
      }
      prev_sm = sm; seen_edge = e;
    }
  }

  { static uint32_t last_env = 0;   // the part free-runs at ~1 Hz; 5 s is fresh enough for a
    if (millis() - last_env > 5000) { last_env = millis(); bmp_read(); }   // variable this slow
  }

  sketch_pump();                                  // before the flush: it gates what may be written
  { static uint32_t last_fl = 0;                  // batch, so the stall is once a second not once a hit
    if (det_n != det_flushed && millis() - last_fl > 1000) { last_fl = millis(); det_flush(); }
  }

  while (Serial1.available()) {
    char c = Serial1.read();
    rawbuf[raw_i] = (uint8_t)c; raw_i = (raw_i + 1) % sizeof(rawbuf); raw_tot++;
    ubx_feed((uint8_t)c);                       // UBX and NMEA share the port; parse both
    if (c == '\n' || nmea_i >= (int)sizeof(nmea) - 1) { nmea[nmea_i] = 0; if (nmea_i > 6) nmea_line(nmea); nmea_i = 0; }
    else if (c != '\r') nmea[nmea_i++] = c;
  }


  // The module ACKs or NAKs a VALSET. Silence means nothing reached it -- almost always the
  // node->GPS TX wire, since NMEA arriving proves only the other direction. Retry, then say which.
  static uint32_t cfg_try = 0, cfg_at = 0;
  if (!ubx_ack && !ubx_nak && cfg_try < 6 && millis() - cfg_at > 5000) {
    cfg_at = millis();
    if (cfg_try) gps_configure();
    cfg_try++;
    if (cfg_try == 6)
      logln("gps   no ACK/NAK after 6 tries -- node TX (D6/GPIO43) -> module RX is not "
                     "connected. Running the module's stock config; PPS will appear only on fix.");
  }

  if (sta_ok && WiFi.status() != WL_CONNECTED) {     // AP blipped; an overnight node reconnects
    static uint32_t retry = 0;
    if (millis() - retry > 15000) { retry = millis(); WiFi.reconnect(); }
  }

  mark_healthy_once();

  static uint32_t last = 0;
  if (millis() - last > 30000) {
    last = millis();
    // The clip budget's live floor. Sampled here and nowhere else: SD.usedBytes() is a free-
    // cluster walk on FATFS whose cost on this card is unmeasured, and the health row was already
    // paying for one call. clip_pump() reads the cached value instead of calling it per clip.
    if (sd_ok) sd_free_mb_last = (uint32_t)((SD.totalBytes() - SD.usedBytes()) / 1048576UL);
    double fs = measured_fs();
    uint32_t up = (millis() - boot_ms) / 1000;
    logf("[%6lus] fix %d/%d sats  tAcc %lu ns  pps %lu (%lu bad)  spread %ld us\n",
                  (unsigned long)up, gps_fix, gps_sats, (unsigned long)gps_tacc_ns,
                  (unsigned long)pps_count, (unsigned long)pps_glitch,
                  (long)(pps_count > 1 ? (long)pps_int_max - (long)pps_int_min : 0));
    if (sd_ok) {   // the radio is a convenience; the card is the record
      // health.csv, not night.csv: the schema gained the acquisition audit and the detection
      // counters, and silently changing the column count of an existing file makes every row in
      // it ambiguous. night.csv keeps the earlier bench rows under its own header.
      // The roll's rename is what made the header bug bite: before it, /health.csv always existed
      // at open time so stat() succeeded and size() was real; the rename made the very next open a
      // CREATE, the one path that reaches the uninitialised _stat. csv_open handles all of it, and
      // handles it identically for all three records.
      File f = csv_open("/health.csv", "/health-prev.csv", HEALTH_HDR);
      if (f) {
        int64_t tnow = 0; bool tok = local_to_utc((uint64_t)esp_timer_get_time(), &tnow);
        // An EMPTY field for a sensor that is not there, never a number. "nan" parses as a float
        // in some readers and as a string in others; 0.0 would read as a freezing night. Empty is
        // the one value every CSV reader already agrees means absent.
        char ct[16], cp[16], cc[16];
        #define CSVF(dst, v) do { if ((v) == (v)) snprintf(dst, sizeof dst, "%.2f", (double)(v)); \
                                  else dst[0] = 0; } while (0)
        CSVF(ct, bmp_temp_c); CSVF(cp, bmp_press_hpa); CSVF(cc, sound_speed_mps());
        // Before the first 3D fix there is no mean. Writing 0.0000000 there would put the node
        // in the Gulf of Guinea, and a reader averaging the column would never notice.
        char mlat[20] = "", mlon[20] = "", mhe[16] = "", mhm[16] = "";
        if (pos_n) {
          snprintf(mlat, sizeof mlat, "%.7f", pos_sum_lat  / pos_n * 1e-7);
          snprintf(mlon, sizeof mlon, "%.7f", pos_sum_lon  / pos_n * 1e-7);
          snprintf(mhe,  sizeof mhe,  "%.3f", pos_sum_hell / pos_n / 1000.0);
          snprintf(mhm,  sizeof mhm,  "%.3f", pos_sum_hmsl / pos_n / 1000.0);
        }
        f.printf("%s,%lld,%d,%lu,%d,%d,%lu,%lu,%lu,%ld,%.4f,%lu,%.4f,"
                 "%.4f,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%.1f,%.0f,%lu,%d,%.0f,%.1f,%lu,%.1f,%lu,%lu,"
                 "%s,%s,%s,"
                 "%.0f,%lu,%lu,%lu,%lu,%lu,%lu,%lu,"
                 "%.7f,%.7f,%.3f,%.3f,%.2f,%.2f,%lu,%s,%s,%s,%s\n",
                 node_id, (long long)tnow, tok ? 1 : 0,
                 (unsigned long)up, gps_fix, gps_sats, (unsigned long)gps_tacc_ns,
                 (unsigned long)pps_count, (unsigned long)pps_glitch,
                 (long)(pps_count > 1 ? (long)pps_int_max - (long)pps_int_min : 0),
                 esp_clock_ppm(NULL), (unsigned long)g_samples, fs,
                 fs_clean, (unsigned long)fs_clean_secs, (unsigned long)drop_seconds,
                 (unsigned long)drop_samples, (unsigned long)samp_sec_last,
                 (unsigned long)det_n, (unsigned long)det_flushed, (unsigned long)det_lost,
                 g_amb, env_peak_win, (unsigned long)ESP.getFreeHeap(),
                 armed, gate_thr(), env_e_max_win, (unsigned long)gate_forced, sig_dc,
                 (unsigned long)sd_free_mb_last,
                 (unsigned long)det_write_fail,
                 ct, cp, cc,
                 g_floor, (unsigned long)clip_written, (unsigned long)clip_skip_budget,
                 (unsigned long)clip_skip_full,
                 (unsigned long)clip_skip_dedupe, (unsigned long)clip_skip_ring,
                 (unsigned long)clip_fail, (unsigned long)clip_budget_left,
                 pos_lat_e7 * 1e-7, pos_lon_e7 * 1e-7,
                 pos_hell_mm / 1000.0, pos_hmsl_mm / 1000.0,
                 pos_hacc_mm / 1000.0, pos_vacc_mm / 1000.0,
                 (unsigned long)pos_n, mlat, mlon, mhe, mhm);
        f.close();
      }
      // scene.csv is held open and written every 1.024 s; committing it costs the same 20-50 ms
      // as any other card flush, so it happens on the 30 s health interval rather than per row.
      // The exposure is up to 30 rows (~7 kB) if the plug timer cuts power mid-interval.
      if (scenef) scenef.flush();
      env_peak_win = 0.0f;      // per-row peak, so a single loud event does not flatten the night
      env_e_max_win = 0.0f;
      det_flush();              // never let the card lag the ring by more than a health interval
    }
  }
}

