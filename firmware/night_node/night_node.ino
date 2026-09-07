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
#include <SPI.h>
#include "driver/gpio.h"
#include <Wire.h>
#include <Update.h>
#include "esp_ota_ops.h"
#include "mel16.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_N                        // no secrets.h -- fall back to the node's own AP
#define WIFI_N 0
static const char *WIFI_SSIDS[] = {""};
static const char *WIFI_PASSES[] = {""};
#endif

#define AP_SSID   "dama-hear-node"
#define AP_PASS   "damahear"          // >=8 chars or the AP silently refuses to start

#define PPS_PIN   1                   // D0. PPS must NOT sit on D11: that is the PDM mic's CLK,
                                      // an output, and two push-pull drivers on one pin is not a
                                      // configuration. D0 keeps mic and PPS coexisting.
#define PDM_CLK   42                  // D11
#define PDM_DIN   41                  // D12
#define GPS_RX    44                  // D7  <- module TX
#define GPS_TX    43                  // D6  -> module RX
#define I2C_SDA 5                    // D4
#define I2C_SCL 6                    // D5
#define SD_SCK 7
#define SD_MISO 8
#define SD_MOSI 9

#define FS_NOMINAL 16000
#define BLOCK      256                // finer block -> finer sample-count granularity per PPS
#define MAXDET     128               // ring, not a cap: the 65th detection used to vanish

// ---------------------------------------------------------------- PPS capture
static volatile uint32_t pps_count = 0;
static volatile uint64_t pps_us_last = 0, pps_us_first = 0, pps_us_prev = 0;
static volatile uint32_t pps_samp_last = 0, pps_samp_first = 0;
static volatile uint32_t pps_int_min = 0xFFFFFFFF, pps_int_max = 0;
static volatile uint32_t pps_glitch = 0;
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
  "utc_us,time_valid,uptime_s,fix,sats,tacc_ns,pps,pps_bad,spread_us,esp_ppm,samples,fs_cum_hz,"
  "fs_clean_hz,fs_win_s,drop_s,drop_samples,samp_last_s,det_n,det_written,det_lost,ambient,"
  "env_peak_win,heap,gate_armed,gate_thr,gate_e_max,gate_forced,sig_dc,sd_free_mb,write_fail";
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
  if (pps_count) {
    uint32_t d = (uint32_t)(now - pps_us_last);
    if (d < pps_int_min) pps_int_min = d;
    if (d > pps_int_max) pps_int_max = d;
  } else {
    pps_us_first = now; pps_samp_first = sm;
  }
  pps_us_prev = pps_us_last;
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
static char tp_readback[160] = "(not read)";
static char nmea[100]; static int nmea_i = 0;
static uint8_t rawbuf[512]; static volatile uint16_t raw_i = 0; static volatile uint32_t raw_tot = 0;
static volatile uint32_t nmea_valid = 0;      // lines that actually start '$' and carry a talker id
static uint32_t gps_baud = 0;
static volatile int gps_fix = 0, gps_sats = 0;
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
  int w = ((key >> 28) & 0x7) == 4 ? 4 : 1;                 // 0x4... is U4, everything used here is 1 B
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
                           K_LEN_TP1, K_LEN_LOCK, K_USE_LOCKED_TP1};
  for (unsigned k = 0; k < sizeof(keys) / sizeof(keys[0]); k++)
    for (int i = 0; i < 4; i++) ubx_buf[vs_i++] = (keys[k] >> (8 * i)) & 0xFF;
  ubx_send(0x06, 0x8B, ubx_buf, vs_i);
}

static void gps_configure() {
  vs_begin();
  vs_add(K_PULSE_DEF, 0); vs_add(K_PULSE_LEN_DEF, 1);
  vs_add(K_PERIOD_TP1, 1000000); vs_add(K_PERIOD_LOCK, 1000000);   // 1 Hz locked AND unlocked
  vs_add(K_LEN_TP1, 100000); vs_add(K_LEN_LOCK, 100000);           // 100 ms, visible on the LED
  vs_add(K_TP1_ENA, 1); vs_add(K_USE_LOCKED_TP1, 1);
  vs_add(K_ALIGN_TOW_TP1, 1); vs_add(K_POL_TP1, 1);
  vs_add(K_SYNC_GNSS_TP1, 1);
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
    snprintf(gps_utc, sizeof gps_utc, "%02u:%02u:%02u", ux[8], ux[9], ux[10]);
    ubx_pvt++;
  } else if (ux_cls == 0x0D && ux_id == 0x01 && ux_len >= 16) {   // TIM-TP
    gps_qerr_ps = (int32_t)((uint32_t)ux[8] | ((uint32_t)ux[9] << 8) | ((uint32_t)ux[10] << 16) | ((uint32_t)ux[11] << 24));
    timtp_flags = ux[14];          // bit0 timeBase, bit1 utc, bit4 qErrInvalid
    ubx_timtp++;
  } else if (ux_cls == 0x06 && ux_id == 0x8B && ux_len > 4) {     // CFG-VALGET response
    char o[160]; int n = 0; uint16_t i = 4;
    while (i + 4 <= ux_len && n < (int)sizeof(o) - 24) {
      uint32_t key = (uint32_t)ux[i] | ((uint32_t)ux[i+1] << 8) | ((uint32_t)ux[i+2] << 16) | ((uint32_t)ux[i+3] << 24);
      int w = (((key >> 28) & 0x7) == 4) ? 4 : 1;
      uint32_t v = 0;
      for (int k = 0; k < w && i + 4 + k < ux_len; k++) v |= (uint32_t)ux[i + 4 + k] << (8 * k);
      const char *nm = key == K_TP1_ENA ? "TP1_ENA" : key == K_PERIOD_TP1 ? "PERIOD"
                     : key == K_PERIOD_LOCK ? "PERIOD_LOCK" : key == K_LEN_TP1 ? "LEN"
                     : key == K_LEN_LOCK ? "LEN_LOCK" : key == K_PULSE_DEF ? "PULSE_DEF"
                     : key == K_USE_LOCKED_TP1 ? "USE_LOCKED" : "?";
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
static int sketch_frame(uint8_t *out, uint32_t back, uint32_t node_us, uint16_t peak, uint16_t flags) {
  static float db[MEL16_BANDS * MEL16_FRAMES];
  uint32_t start = (aring_w + ARING - back) % ARING;
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
static const float RATIO = 8.0f, FLOOR = 800.0f, REARM = 0.35f;
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
static float gate_thr() { float t = g_amb * RATIO; return t < FLOOR ? FLOOR : t; }
static int gate(int16_t s) {
  float a = fabsf((float)s);
  env_sum += a - env_buf[env_i]; env_buf[env_i] = a;
  if (++env_i >= 16) env_i = 0;
  float e = env_sum * ENV_INV;
  if (e > env_e_max_win) env_e_max_win = e;
  float thr = g_amb * RATIO; if (thr < FLOOR) thr = FLOOR;
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

struct Det { uint32_t sample; uint32_t pps_n; int32_t us_since_pps; int64_t utc_us;
              int16_t trigger; uint16_t flags; uint32_t uptime_s; double fs_at;
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
  char b[1536];
  snprintf(b, sizeof b,
    "{\"uptime_s\":%lu,\"heap\":%lu,\"psram\":%lu,"
    "\"gps\":{\"fix\":%d,\"sats\":%d,\"utc\":\"%s\",\"sentences\":%lu,\"valid_nmea\":%lu,\"baud\":%lu,"
    "\"tacc_ns\":%lu,\"qerr_ps\":%ld,\"ubx_pvt\":%lu,\"ubx_timtp\":%lu,\"ubx_ack\":%lu,\"ubx_nak\":%lu,\"config_acked\":%s,\"timtp_flags\":%u,\"qerr_valid\":%s},"
    "\"pps\":{\"edges\":%lu,\"glitches\":%lu,\"interval_min_us\":%lu,\"interval_max_us\":%lu,\"spread_us\":%ld},"
    "\"i2s\":{\"nominal_hz\":%d,\"measured_hz\":%.4f,\"ppm\":%.1f,\"samples\":%lu},"
    // measured_hz above is cumulative and stays poisoned by any stall. acq is the one to trust.
    "\"acq\":{\"fs_clean_hz\":%.4f,\"win_s\":%lu,\"drop_s\":%lu,\"drop_samples\":%lu,\"last_s\":%lu},"
    "\"esp_clock\":{\"ppm_vs_gps\":%.3f,\"pps_intervals\":%lu},"
    "\"time\":{\"valid\":%s,\"utc_us\":%lld,\"since_edge_us\":%llu,\"navpvt_nano\":%ld,\"label_rejects\":%lu},"
    "\"audio\":{\"enabled\":true,\"detections\":%lu,\"written\":%lu,\"lost\":%lu,"
    "\"ambient\":%.1f,\"env_peak\":%.0f},"
    "\"gate\":{\"armed\":%d,\"thr\":%.0f,\"e_max_win\":%.1f,\"headroom\":%.2f,"
    "\"forced_rearms\":%lu,\"dc\":%.1f},\"write_fail\":%lu,"
    "\"sd\":%s,\"sd_free_mb\":%lu,\"sd_total_mb\":%lu,\"i2c\":\"%s\"}",
    (unsigned long)((millis() - boot_ms) / 1000), (unsigned long)ESP.getFreeHeap(),
    (unsigned long)ESP.getFreePsram(),
    gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences,
    (unsigned long)nmea_valid, (unsigned long)gps_baud,
    (unsigned long)gps_tacc_ns, (long)gps_qerr_ps, (unsigned long)ubx_pvt,
    (unsigned long)ubx_timtp, (unsigned long)ubx_ack, (unsigned long)ubx_nak,
    (ubx_ack ? "true" : "false"), (unsigned)timtp_flags,
    ((timtp_flags != 0xFF && !(timtp_flags & 0x10)) ? "true" : "false"),
    (unsigned long)pps_count, (unsigned long)pps_glitch,
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
    (unsigned long)gate_forced, sig_dc, (unsigned long)det_write_fail,
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
  o.reserve(n * (MEL16_FRAME_BYTES * 2 + 200) + 64);
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
    o += "\"}";
  }
  o += "]";
  http.send(200, "application/json", o);
}

void setup() {
  boot_guard();                    // first statement: a later fault still counts as a failed boot
  Serial.begin(115200);
  delay(1500);
  boot_ms = millis();
  logf("boot  attempt %lu on partition %s\n", (unsigned long)boot_try,
                esp_ota_get_running_partition()->label);
  logln("\n=== dama-hear night node ===");

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
    WiFi.mode(WIFI_AP); WiFi.softAP(AP_SSID, AP_PASS);
    logf("wifi  AP   ssid \"%s\" pass \"%s\"  http://%s/\n",
                  AP_SSID, AP_PASS, WiFi.softAPIP().toString().c_str());
    logln("      (no secrets.h, or the join failed -- see firmware/night_node/README)");
  }
  if (MDNS.begin("damahear")) logln("mdns  http://damahear.local/");

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
  // Is anything DRIVING the RX line? A floating UART input frames noise into bytes that look like
  // data, which is how "22 sentences" and 1236 received bytes coexisted with zero valid NMEA. A
  // real transmitter idles HIGH and holds it high against a pulldown; a floating pin follows the
  // pulldown to 0. Same trick that settled the phantom PPS edges.
  {
    pinMode(GPS_RX, INPUT_PULLDOWN); delay(20);
    int high = 0, edges = 0, last = digitalRead(GPS_RX);
    uint32_t t0 = millis();
    while (millis() - t0 < 300) { int v = digitalRead(GPS_RX); if (v) high++; if (v != last) { edges++; last = v; } }
    logf("gps   RX pin (D7/GPIO%d) with pulldown: %s (%d edges)\n", GPS_RX,
                  high > 20 ? "DRIVEN high -- a transmitter is connected"
                            : "follows the pulldown -- NOTHING is driving it", edges);
  }

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
      Serial1.begin(cand[k], SERIAL_8N1, GPS_RX, GPS_TX);
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
    Serial1.begin(gps_baud, SERIAL_8N1, GPS_RX, GPS_TX);
    logf("gps   using %lu baud (%s)%s\n", (unsigned long)gps_baud,
                  best_ubx ? "UBX binary" : "NMEA",
                  best_b ? "" : " -- nothing decoded at any rate");
  }
  delay(300);
  gps_configure();
  logln("gps   UBX config sent (TP1 1 Hz locked+unlocked, NAV-PVT, TIM-TP; RAM layer)");

  Wire.begin(I2C_SDA, I2C_SCL, 100000);
  i2c_scan();

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI);
  sd_cs = 0;
  if (SD.begin(21, SPI, 20000000)) { sd_ok = true; sd_cs = 21; }
  else if (SD.begin(3, SPI, 20000000)) { sd_ok = true; sd_cs = 3; }
  logf("sd    %s%s", sd_ok ? "mounted, CS=" : "no card", sd_ok ? "" : "\n");
  if (sd_ok) logf("%d\n", sd_cs);

  i2s.setPinsPdmRx(PDM_CLK, PDM_DIN);
  if (!i2s.begin(I2S_MODE_PDM_RX, FS_NOMINAL, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO))
    logln("i2s   FAILED");
  else logf("i2s   PDM %d Hz on CLK=%d DIN=%d\n", FS_NOMINAL, PDM_CLK, PDM_DIN);

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
  http.on("/tp", []() {
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
    attachInterrupt(digitalPinToInterrupt(PPS_PIN), pps_isr, RISING);
    (void)watching;
    http.send(200, "text/plain", o);
  });
  http.on("/tp", []() {
    strcpy(tp_readback, "(no response)");
    gps_valget();
    // The UBX parser lives in loop(), which is blocked while this handler runs -- so pump the
    // port here instead of delay()ing and wondering why nothing arrived.
    uint32_t t0 = millis();
    while (millis() - t0 < 900) { while (Serial1.available()) ubx_feed((uint8_t)Serial1.read()); }
    http.send(200, "text/plain", tp_readback);
  });
  http.on("/pps", []() {              // live probe: wire the tap, hit this, no reboot needed
    detachInterrupt(digitalPinToInterrupt(PPS_PIN));
    pinMode(PPS_PIN, INPUT_PULLDOWN);
    int high = 0, n = 0, edges = 0, last = digitalRead(PPS_PIN);
    uint32_t t0 = millis();
    while (millis() - t0 < 2500) { int v = digitalRead(PPS_PIN); if (v) high++; n++; if (v != last) { edges++; last = v; } }
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
    for (uint16_t k = 0; k < sizeof(rawbuf); k++) {
      char c = (char)rawbuf[(st + k) % sizeof(rawbuf)];
      o += (c >= 32 && c < 127) ? c : (c == '\n' ? '\n' : '.');
    }
    http.send(200, "text/plain", o);
  });
  http.on("/i2c", []() { i2c_scan(); http.send(200, "text/plain", i2c_found); });   // rescan on demand
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
static File detf;
static bool det_hdr_done = false;

static void det_flush() {
  if (!sd_ok || det_flushed == det_n) return;
  if (!detf) {
    detf = SD.open("/dets.csv", FILE_APPEND);
    if (!detf) return;
    if (!det_hdr_done && detf.size() == 0)
      detf.println("utc_us,uptime_s,sample,pps_n,us_since_pps,trigger,flags,fs_hz,frame_hex");
    det_hdr_done = true;
  }
  // If more than MAXDET landed since the last flush, the oldest slots have already been
  // overwritten. Count them as lost instead of writing whatever occupies the slot now.
  uint32_t first = det_flushed;
  if (det_n - det_flushed > MAXDET) { first = det_n - MAXDET; det_lost += first - det_flushed; }
  uint32_t wrote = 0;
  static const char hx[] = "0123456789abcdef";
  for (uint32_t k = first; k < det_n && wrote < 16; k++, wrote++) {
    const Det &d = dets[k % MAXDET];
    char line[MEL16_FRAME_BYTES * 2 + 160];
    int m = snprintf(line, sizeof line, "%lld,%lu,%lu,%lu,%ld,%d,%u,%.3f,",
                     (long long)d.utc_us, (unsigned long)d.uptime_s, (unsigned long)d.sample,
                     (unsigned long)d.pps_n, (long)d.us_since_pps, d.trigger,
                     (unsigned)d.flags, d.fs_at);
    for (int j = 0; j < MEL16_FRAME_BYTES && m < (int)sizeof line - 3; j++) {
      line[m++] = hx[d.frame[j] >> 4]; line[m++] = hx[d.frame[j] & 0xF];
    }
    line[m++] = '\n';
    if (detf.write((const uint8_t *)line, m) != (size_t)m) { det_write_fail++; break; }
    det_flushed = k + 1;      // only advance on a write that actually landed
  }
  detf.flush();          // commit: the plug timer can cut power between any two loop iterations
}

static int16_t blk[BLOCK];

void loop() {
  http.handleClient();

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
          // Sketch from a little BEFORE the trigger, so the rise the classifier needs is inside
          // the window rather than clipped off its front.
          uint32_t back = MEL16_NFFT + (MEL16_FRAMES - 1) * MEL16_HOP + 32;
          // ⚠️Before the ring has filled, the window behind the trigger is zeros, and a sketch of
          // silence is a legitimate-looking frame of all-equal bands. Flag it rather than ship a
          // number that means nothing. Bit 1 = insufficient context. (Bit 0 is retrigger.)
          uint16_t fl = (aring_total < back) ? 0x0002 : 0x0000;
          dets[idx].flags = fl;
          sketch_frame(dets[idx].frame, back,
                       (uint32_t)(dets[idx].us_since_pps),
                       (uint16_t)abs((int)sac), fl);
        }
      }
      float a = fabsf((float)sac);
      if (a > env_peak_seen) env_peak_seen = a;
      if (a > env_peak_win) env_peak_win = a;
    }
    g_samples += n; }

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
        if (d < (uint32_t)(0.97 * fs_clean)) {
          drop_seconds++;
          drop_samples += (uint32_t)(fs_clean - (double)d);
          fs_clean_secs = 0;                      // a broken second cannot be averaged over
        } else {
          if (!fs_clean_secs) { win_sm0 = prev_sm; win_e0 = e - 1; }
          fs_clean_secs = e - win_e0;
          if (fs_clean_secs >= 8) fs_clean = (double)(sm - win_sm0) / (double)fs_clean_secs;
        }
      }
      prev_sm = sm; seen_edge = e;
    }
  }

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
      // If the schema has changed since the file was started, every row in it becomes ambiguous
      // -- appending wider rows under a narrower header is worse than starting a new file. Roll
      // the old one aside once per boot rather than quietly corrupting it.
      static bool hdr_checked = false;
      if (!hdr_checked) {
        hdr_checked = true;
        File r = SD.open("/health.csv", FILE_READ);
        if (r) {
          String first = r.readStringUntil('\n'); r.close();
          first.trim();
          if (first.length() && first != String(HEALTH_HDR)) {
            SD.remove("/health-prev.csv");
            SD.rename("/health.csv", "/health-prev.csv");
          }
        }
      }
      File f = SD.open("/health.csv", FILE_APPEND);
      if (f) {
        if (f.size() == 0) f.println(HEALTH_HDR);
        int64_t tnow = 0; bool tok = local_to_utc((uint64_t)esp_timer_get_time(), &tnow);
        f.printf("%lld,%d,%lu,%d,%d,%lu,%lu,%lu,%ld,%.4f,%lu,%.4f,"
                 "%.4f,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%.1f,%.0f,%lu,%d,%.0f,%.1f,%lu,%.1f,%lu,%lu\n",
                 (long long)tnow, tok ? 1 : 0,
                 (unsigned long)up, gps_fix, gps_sats, (unsigned long)gps_tacc_ns,
                 (unsigned long)pps_count, (unsigned long)pps_glitch,
                 (long)(pps_count > 1 ? (long)pps_int_max - (long)pps_int_min : 0),
                 esp_clock_ppm(NULL), (unsigned long)g_samples, fs,
                 fs_clean, (unsigned long)fs_clean_secs, (unsigned long)drop_seconds,
                 (unsigned long)drop_samples, (unsigned long)samp_sec_last,
                 (unsigned long)det_n, (unsigned long)det_flushed, (unsigned long)det_lost,
                 g_amb, env_peak_win, (unsigned long)ESP.getFreeHeap(),
                 armed, gate_thr(), env_e_max_win, (unsigned long)gate_forced, sig_dc,
                 (unsigned long)((SD.totalBytes() - SD.usedBytes()) / 1048576UL),
                 (unsigned long)det_write_fail);
        f.close();
      }
      env_peak_win = 0.0f;      // per-row peak, so a single loud event does not flatten the night
      env_e_max_win = 0.0f;
      det_flush();              // never let the card lag the ring by more than a health interval
    }
  }
}
