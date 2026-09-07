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

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#define WIFI_PASS ""
#endif

#define AP_SSID   "dama-hear-node"
#define AP_PASS   "damahear"          // >=8 chars or the AP silently refuses to start

#define PPS_PIN   1                   // D0
#define GPS_RX    44                  // D7  <- module TX
#define GPS_TX    43                  // D6  -> module RX
#define PDM_CLK   42
#define PDM_DIN   41
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
static char nmea[100]; static int nmea_i = 0;
static volatile int gps_fix = 0, gps_sats = 0;
static char gps_utc[16] = "--:--:--";
static uint32_t gps_sentences = 0;

static void nmea_line(const char *s) {
  gps_sentences++;
  // $xxGGA,hhmmss.ss,lat,N,lon,E,fix,sats,...
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
    "\"gps\":{\"fix\":%d,\"sats\":%d,\"utc\":\"%s\",\"sentences\":%lu},"
    "\"pps\":{\"edges\":%lu,\"glitches\":%lu,\"interval_min_us\":%lu,\"interval_max_us\":%lu,\"spread_us\":%ld},"
    "\"i2s\":{\"nominal_hz\":%d,\"measured_hz\":%.4f,\"ppm\":%.1f,\"samples\":%lu},"
    "\"audio\":{\"detections\":%lu,\"env_peak\":%.0f},\"sd\":%s}",
    (unsigned long)((millis() - boot_ms) / 1000), (unsigned long)ESP.getFreeHeap(),
    (unsigned long)ESP.getFreePsram(),
    gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences,
    (unsigned long)pps_count, (unsigned long)pps_glitch,
    (unsigned long)(pps_count > 1 ? pps_int_min : 0), (unsigned long)(pps_count > 1 ? pps_int_max : 0),
    (long)(pps_count > 1 ? (long)pps_int_max - (long)pps_int_min : 0),
    FS_NOMINAL, fs, fs > 0 ? (fs / FS_NOMINAL - 1.0) * 1e6 : 0.0, (unsigned long)g_samples,
    (unsigned long)det_n, env_peak_seen, sd_ok ? "true" : "false");
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
             "`<tr><td>GPS<td><b>fix ${s.gps.fix}, ${s.gps.sats} sats, ${s.gps.utc} UTC</b> (${s.gps.sentences} sentences)`+"
             "`<tr><td>PPS edges<td><b>${s.pps.edges}</b> spread ${s.pps.spread_us} us, ${s.pps.glitches} rejected`+"
             "`<tr><td>I2S measured<td><b>${f}</b>`+"
             "`<tr><td>samples<td>${s.i2s.samples}`+"
             "`<tr><td>detections<td><b>${s.audio.detections}</b> (env peak ${s.audio.env_peak})`+"
             "`<tr><td>SD<td>${s.sd}`+`<tr><td>heap / psram<td>${s.heap} / ${s.psram}`;}"
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

  if (strlen(WIFI_SSID)) {
    WiFi.mode(WIFI_STA); WiFi.setSleep(false); WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("wifi joining \"%s\"", WIFI_SSID);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
    sta_ok = WiFi.status() == WL_CONNECTED;
    Serial.println();
  }
  if (sta_ok) Serial.printf("wifi  STA  http://%s/\n", WiFi.localIP().toString().c_str());
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

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI);
  sd_ok = SD.begin(21, SPI, 20000000) || SD.begin(3, SPI, 20000000);
  Serial.printf("sd    %s\n", sd_ok ? "mounted" : "no card");

  i2s.setPinsPdmRx(PDM_CLK, PDM_DIN);
  if (!i2s.begin(I2S_MODE_PDM_RX, FS_NOMINAL, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO))
    Serial.println("i2s   FAILED");
  else Serial.printf("i2s   PDM %d Hz nominal\n", FS_NOMINAL);

  http.on("/", h_root); http.on("/status", h_status); http.on("/detections", h_dets);
  http.begin();
  Serial.println("http  up\n");
}

static int16_t blk[BLOCK];

void loop() {
  http.handleClient();

  while (Serial1.available()) {
    char c = Serial1.read();
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

  static uint32_t last = 0;
  if (millis() - last > 30000) {
    last = millis();
    double fs = measured_fs();
    uint32_t up = (millis() - boot_ms) / 1000;
    Serial.printf("[%6lus] fix %d/%d sats  pps %lu (%lu bad)  fs %.3f Hz (%+.1f ppm)  dets %lu\n",
                  (unsigned long)up, gps_fix, gps_sats, (unsigned long)pps_count,
                  (unsigned long)pps_glitch, fs, fs > 0 ? (fs / FS_NOMINAL - 1.0) * 1e6 : 0.0,
                  (unsigned long)det_n);
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
