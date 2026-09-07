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

#define PPS_PIN   1                   // D0
#define GPS_RX    44                  // D7  <- module TX
#define GPS_TX    43                  // D6  -> module RX
#define PDM_CLK   42
#define PDM_DIN   41
#define I2C_SDA 5                    // D4
#define I2C_SCL 6                    // D5
#define SD_SCK 7
#define SD_MISO 8
#define SD_MOSI 9

#define FS_NOMINAL 16000
#define BLOCK      256                // finer block -> finer sample-count granularity per PPS
#define MAXDET     64

// ---------------------------------------------------------------- PPS capture
static volatile uint32_t pps_count = 0;
static volatile uint64_t pps_us_last = 0, pps_us_first = 0;
static volatile uint32_t pps_samp_last = 0, pps_samp_first = 0;
static volatile uint32_t pps_int_min = 0xFFFFFFFF, pps_int_max = 0;
static volatile uint32_t pps_glitch = 0;
// A 1 Hz pulse cannot have edges closer than this. Anything faster is noise on the wire, and it
// must be counted rather than averaged in: a floating input self-oscillated at ~3.4 kHz on the
// bench and produced a confident +626 ppm sample-rate figure out of nothing.
#define PPS_MIN_GAP_US 500000
static volatile uint32_t g_samples = 0;      // updated by the audio loop, read in the ISR

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
  pps_us_last = now; pps_samp_last = sm;
  pps_count++;
}

// ---------------------------------------------------------------- GPS (minimal NMEA)
static volatile uint32_t gps_tacc_ns = 0, ubx_pvt = 0, ubx_timtp = 0, ubx_nak = 0, ubx_ack = 0;
static volatile int32_t  gps_qerr_ps = 0;
static char nmea[100]; static int nmea_i = 0;
static volatile int gps_fix = 0, gps_sats = 0;
static char gps_utc[16] = "--:--:--";
static uint32_t gps_sentences = 0;

static void nmea_line(const char *s) {
  gps_sentences++;
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
  Serial.printf("i2c   SDA=%d SCL=%d: %s\n", I2C_SDA, I2C_SCL, i2c_found);
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
    gps_tacc_ns = (uint32_t)ux[12] | ((uint32_t)ux[13] << 8) | ((uint32_t)ux[14] << 16) | ((uint32_t)ux[15] << 24);
    gps_fix = ux[20]; gps_sats = ux[23];
    snprintf(gps_utc, sizeof gps_utc, "%02u:%02u:%02u", ux[8], ux[9], ux[10]);
    ubx_pvt++;
  } else if (ux_cls == 0x0D && ux_id == 0x01 && ux_len >= 16) {   // TIM-TP
    gps_qerr_ps = (int32_t)((uint32_t)ux[8] | ((uint32_t)ux[9] << 8) | ((uint32_t)ux[10] << 16) | ((uint32_t)ux[11] << 24));
    ubx_timtp++;
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

// ---------------------------------------------------------------- gate (as hear/node/detect.py)
static float g_amb = 0, env_sum = 0, env_buf[16]; static int env_i = 0, armed = 1;
static const float ENV_INV = 1.0f / 16.0f, ALPHA = 1.0f / 10000.0f;
static const float RATIO = 8.0f, FLOOR = 800.0f, REARM = 0.35f;
static int gate(int16_t s) {
  float a = fabsf((float)s);
  env_sum += a - env_buf[env_i]; env_buf[env_i] = a;
  if (++env_i >= 16) env_i = 0;
  float e = env_sum * ENV_INV;
  float thr = g_amb * RATIO; if (thr < FLOOR) thr = FLOOR;
  if (!armed) { if (e < thr * REARM) armed = 1; return 0; }
  if (e <= thr) { g_amb = (1.0f - ALPHA) * g_amb + ALPHA * e; return 0; }
  armed = 0; return 1;
}

struct Det { uint32_t sample; uint32_t pps_n; uint32_t us_since_pps; int16_t trigger; uint32_t uptime_s; };
static Det dets[MAXDET]; static volatile uint32_t det_n = 0;

// ---------------------------------------------------------------- state
static I2SClass i2s;
static WebServer http(80);
static bool sd_ok = false, sta_ok = false;
static uint32_t boot_ms = 0;
static float env_peak_seen = 0;

static double measured_fs() {
  if (pps_count < 3) return 0.0;
  double secs = (double)(pps_us_last - pps_us_first) / 1e6;
  if (secs < 1.0) return 0.0;
  return (double)(pps_samp_last - pps_samp_first) / secs;
}

static String status_json() {
  double fs = measured_fs();
  char b[1024];
  snprintf(b, sizeof b,
    "{\"uptime_s\":%lu,\"heap\":%lu,\"psram\":%lu,"
    "\"gps\":{\"fix\":%d,\"sats\":%d,\"utc\":\"%s\",\"sentences\":%lu,"
    "\"tacc_ns\":%lu,\"qerr_ps\":%ld,\"ubx_pvt\":%lu,\"ubx_timtp\":%lu,\"ubx_ack\":%lu,\"ubx_nak\":%lu,\"config_acked\":%s},"
    "\"pps\":{\"edges\":%lu,\"glitches\":%lu,\"interval_min_us\":%lu,\"interval_max_us\":%lu,\"spread_us\":%ld},"
    "\"i2s\":{\"nominal_hz\":%d,\"measured_hz\":%.4f,\"ppm\":%.1f,\"samples\":%lu},"
    "\"audio\":{\"detections\":%lu,\"env_peak\":%.0f},\"sd\":%s,\"i2c\":\"%s\"}",
    (unsigned long)((millis() - boot_ms) / 1000), (unsigned long)ESP.getFreeHeap(),
    (unsigned long)ESP.getFreePsram(),
    gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences,
    (unsigned long)gps_tacc_ns, (long)gps_qerr_ps, (unsigned long)ubx_pvt,
    (unsigned long)ubx_timtp, (unsigned long)ubx_ack, (unsigned long)ubx_nak,
    (ubx_ack ? "true" : "false"),
    (unsigned long)pps_count, (unsigned long)pps_glitch,
    (unsigned long)(pps_count > 1 ? pps_int_min : 0), (unsigned long)(pps_count > 1 ? pps_int_max : 0),
    (long)(pps_count > 1 ? (long)pps_int_max - (long)pps_int_min : 0),
    FS_NOMINAL, fs, fs > 0 ? (fs / FS_NOMINAL - 1.0) * 1e6 : 0.0, (unsigned long)g_samples,
    (unsigned long)det_n, env_peak_seen, sd_ok ? "true" : "false", i2c_found);
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
             "`<tr><td>GPS<td><b>fix ${s.gps.fix}, ${s.gps.sats} sats, ${s.gps.utc} UTC</b>`+`<tr><td>time accuracy<td><b>${s.gps.tacc_ns} ns</b> &middot; pulse qErr ${s.gps.qerr_ps} ps`+`<tr><td>UBX<td>pvt ${s.gps.ubx_pvt}, tim-tp ${s.gps.ubx_timtp}, ack ${s.gps.ubx_ack}, nak ${s.gps.ubx_nak}`+`<tr><td>config<td><b>${s.gps.config_acked?'accepted':'NOT acked - node TX to module RX unwired?'}</b>`+"
             "`<tr><td>PPS edges<td><b>${s.pps.edges}</b> spread ${s.pps.spread_us} us, ${s.pps.glitches} rejected`+"
             "`<tr><td>I2S measured<td><b>${f}</b>`+"
             "`<tr><td>samples<td>${s.i2s.samples}`+"
             "`<tr><td>detections<td><b>${s.audio.detections}</b> (env peak ${s.audio.env_peak})`+"
             "`<tr><td>SD<td>${s.sd}`+`<tr><td>I2C<td>${s.i2c}`+`<tr><td>heap / psram<td>${s.heap} / ${s.psram}`;}"
             "u();setInterval(u,2000);</script>";
  http.send(200, "text/html", p);
}
static void h_status() { http.send(200, "application/json", status_json()); }
static void h_dets() {
  String o = "[";
  uint32_t n = det_n < MAXDET ? det_n : MAXDET;
  for (uint32_t i = 0; i < n; i++) {
    char b[160];
    snprintf(b, sizeof b, "%s{\"uptime_s\":%lu,\"sample\":%lu,\"pps_n\":%lu,\"us_since_pps\":%lu,\"trigger\":%d}",
             i ? "," : "", (unsigned long)dets[i].uptime_s, (unsigned long)dets[i].sample,
             (unsigned long)dets[i].pps_n, (unsigned long)dets[i].us_since_pps, dets[i].trigger);
    o += b;
  }
  o += "]";
  http.send(200, "application/json", o);
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  boot_ms = millis();
  Serial.println("\n=== dama-hear night node ===");

  // Try each configured network in turn. An outdoor node may only reach one of them, and which
  // one is not knowable from indoors.
  if (WIFI_N > 0) {
    WiFi.mode(WIFI_STA); WiFi.setSleep(false);
    for (int k = 0; k < WIFI_N && !sta_ok; k++) {
      Serial.printf("wifi  trying network %d/%d", k + 1, WIFI_N);
      WiFi.begin(WIFI_SSIDS[k], WIFI_PASSES[k]);
      for (int i = 0; i < 24 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
      sta_ok = WiFi.status() == WL_CONNECTED;
      Serial.println(sta_ok ? " joined" : " no");
      if (!sta_ok) WiFi.disconnect();
    }
  }
  if (sta_ok) Serial.printf("wifi  STA  \"%s\"  http://%s/\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  else {
    WiFi.mode(WIFI_AP); WiFi.softAP(AP_SSID, AP_PASS);
    Serial.printf("wifi  AP   ssid \"%s\" pass \"%s\"  http://%s/\n",
                  AP_SSID, AP_PASS, WiFi.softAPIP().toString().c_str());
    Serial.println("      (no secrets.h, or the join failed -- see firmware/night_node/README)");
  }
  if (MDNS.begin("damahear")) Serial.println("mdns  http://damahear.local/");

  // PULLDOWN, not bare INPUT. An unconnected CMOS input floats and self-oscillates -- measured
  // ~3.4 kHz of phantom edges, which the rate maths happily turned into a plausible +626 ppm.
  pinMode(PPS_PIN, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(PPS_PIN), pps_isr, RISING);
  Serial1.begin(9600, SERIAL_8N1, GPS_RX, GPS_TX);
  delay(300);
  gps_configure();
  Serial.println("gps   UBX config sent (TP1 1 Hz locked+unlocked, NAV-PVT, TIM-TP; RAM layer)");

  Wire.begin(I2C_SDA, I2C_SCL, 100000);
  i2c_scan();

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI);
  sd_ok = SD.begin(21, SPI, 20000000) || SD.begin(3, SPI, 20000000);
  Serial.printf("sd    %s\n", sd_ok ? "mounted" : "no card");

  i2s.setPinsPdmRx(PDM_CLK, PDM_DIN);
  if (!i2s.begin(I2S_MODE_PDM_RX, FS_NOMINAL, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO))
    Serial.println("i2s   FAILED");
  else Serial.printf("i2s   PDM %d Hz nominal\n", FS_NOMINAL);

  http.on("/", h_root); http.on("/status", h_status); http.on("/detections", h_dets);
  http.on("/i2c", []() { i2c_scan(); http.send(200, "text/plain", i2c_found); });   // rescan on demand
  http.begin();
  Serial.println("http  up\n");
}

static int16_t blk[BLOCK];

void loop() {
  http.handleClient();

  while (Serial1.available()) {
    char c = Serial1.read();
    ubx_feed((uint8_t)c);                       // UBX and NMEA share the port; parse both
    if (c == '\n' || nmea_i >= (int)sizeof(nmea) - 1) { nmea[nmea_i] = 0; if (nmea_i > 6) nmea_line(nmea); nmea_i = 0; }
    else if (c != '\r') nmea[nmea_i++] = c;
  }

  size_t got = i2s.readBytes((char *)blk, sizeof blk);
  int n = got / 2;
  for (int i = 0; i < n; i++) {
    // gate() is stateful -- exactly one call per sample, or the envelope is fed samples that
    // never existed. An earlier version called it twice on the not-detected path.
    int fired = gate(blk[i]);
    if (fired) {
      uint32_t idx = det_n++;                       // count every detection; store the first MAXDET
      if (idx < MAXDET) {
        dets[idx].sample = g_samples + i;
        dets[idx].pps_n = pps_count;
        dets[idx].us_since_pps = (uint32_t)((uint64_t)esp_timer_get_time() - pps_us_last);
        dets[idx].trigger = blk[i];
        dets[idx].uptime_s = (millis() - boot_ms) / 1000;
      }
    }
    float a = fabsf((float)blk[i]);
    if (a > env_peak_seen) env_peak_seen = a;
  }
  g_samples += n;

  // The module ACKs or NAKs a VALSET. Silence means nothing reached it -- almost always the
  // node->GPS TX wire, since NMEA arriving proves only the other direction. Retry, then say which.
  static uint32_t cfg_try = 0, cfg_at = 0;
  if (!ubx_ack && !ubx_nak && cfg_try < 6 && millis() - cfg_at > 5000) {
    cfg_at = millis();
    if (cfg_try) gps_configure();
    cfg_try++;
    if (cfg_try == 6)
      Serial.println("gps   no ACK/NAK after 6 tries -- node TX (D6/GPIO43) -> module RX is not "
                     "connected. Running the module's stock config; PPS will appear only on fix.");
  }

  if (sta_ok && WiFi.status() != WL_CONNECTED) {     // AP blipped; an overnight node reconnects
    static uint32_t retry = 0;
    if (millis() - retry > 15000) { retry = millis(); WiFi.reconnect(); }
  }

  static uint32_t last = 0;
  if (millis() - last > 30000) {
    last = millis();
    double fs = measured_fs();
    uint32_t up = (millis() - boot_ms) / 1000;
    Serial.printf("[%6lus] fix %d/%d sats  tAcc %lu ns  qErr %ld ps  pps %lu (%lu bad)  fs %.3f Hz (%+.1f ppm)  dets %lu\n",
                  (unsigned long)up, gps_fix, gps_sats, (unsigned long)gps_tacc_ns, (long)gps_qerr_ps,
                  (unsigned long)pps_count, (unsigned long)pps_glitch, fs,
                  fs > 0 ? (fs / FS_NOMINAL - 1.0) * 1e6 : 0.0, (unsigned long)det_n);
    if (sd_ok) {   // the radio is a convenience; the card is the record
      File f = SD.open("/night.csv", FILE_APPEND);
      if (f) {
        if (f.size() == 0) f.println("uptime_s,fix,sats,utc,pps,pps_bad,samples,fs_hz,ppm,dets");
        f.printf("%lu,%d,%d,%s,%lu,%lu,%lu,%.4f,%.2f,%lu\n", (unsigned long)up, gps_fix, gps_sats,
                 gps_utc, (unsigned long)pps_count, (unsigned long)pps_glitch,
                 (unsigned long)g_samples, fs, fs > 0 ? (fs / FS_NOMINAL - 1.0) * 1e6 : 0.0,
                 (unsigned long)det_n);
        f.close();
      }
    }
  }
}
