// dama-hear node firmware for BirdWeather PUC hardware.
//
// WHAT THIS IS. A BEACHHEAD, not a finished node. Its only job is to make the PUC reachable and
// updatable over WiFi so that everything else can be developed without a cable -- and to carry the
// instruments needed to work out the rest of the board, since the vendor firmware told us where
// only some of it is.
//
// WHAT IS KNOWN about this hardware, measured rather than assumed (see docs/puc-hardware.md):
//   ESP32-S3 rev v0.1, 8 MB octal PSRAM, 32 MB Macronix OPI flash, unencrypted.
//   Quectel L86 GNSS (MT3333 core, firmware AXN5.1.9) on GPIO44 = module TX, GPIO43 = module RX,
//     9600 baud, PMTK protocol. Confirmed by round-trip: $PMTK605 -> $PMTK705,...,Quectel-L86.
//   Two MEMS microphones at 48 kHz, summed to mono by the vendor firmware. Pins NOT yet known.
//   DS3231 RTC and an environmental suite on I2C. Pins NOT yet known.
//   microSD slot. Pins NOT yet known.
//
// ⚠️1PPS IS NOT ROUTED. The L86 exposes it on pin 11 and it was forced on with $PMTK285,4,100;
// no GPIO saw a 1 Hz edge across repeated whole-bank scans. Until a wire is added, this board
// CANNOT contribute a TDoA arrival -- it is hear/nodeclass.py's `puc-ntp`, not `puc-pps`.
// PPS_PIN below is where that wire should land when it exists.
//
// ⚠️GPIO 19 and 20 ARE USB D-/D+. Reconfiguring them kills the console; it happened once already
// and cost a replug caught inside a four-second window. They are excluded everywhere, alongside
// 26-32 (SPI flash) and 33-37 (octal PSRAM).

#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_mac.h>
#include "soc/gpio_struct.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_N
static const char *WIFI_SSIDS[] = {""};
static const char *WIFI_PASSES[] = {""};
#define WIFI_N 0
#endif
#ifndef NODE_CLASS
#define NODE_CLASS "puc-ntp"        // until 1PPS is wired. hear/nodeclass.py refuses this class
#endif                              // as a TDoA arrival source, and is right to.

#define GPS_RX_PIN 44               // module TX -> here
#define GPS_TX_PIN 43               // here -> module RX
#define GPS_BAUD   9600
#define PPS_PIN    18               // WHERE THE WIRE GOES. Held low, not a strapping pin, clear of
                                    // flash, PSRAM and USB. Nothing drives it today.

static char node_id[24];
static WebServer http(80);
static bool sta_ok = false;

// ---------------------------------------------------------------- boot failback
// Same shape as night_node: count boots in RTC memory, and flip back if a new image never proves
// itself. It does NOT rely on the bootloader's rollback, which this core's prebuilt bootloader may
// not have enabled -- depending on that would be depending on something unverified.
RTC_NOINIT_ATTR static uint32_t boot_magic, boot_try, proven_ok;
#define BOOT_MAGIC 0x50554331
#define BOOT_MAX_TRIES 3

static void boot_guard() {
  if (boot_magic != BOOT_MAGIC) { boot_magic = BOOT_MAGIC; boot_try = 0; proven_ok = 0; }
  boot_try++;
  // Only ever revert an UNPROVEN image. Once a build has reached healthy, being unreachable means
  // the node moved or the AP changed, not that the firmware is bad.
  if (proven_ok) { boot_try = 0; return; }
  if (boot_try > BOOT_MAX_TRIES) {
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    boot_try = 0;
    if (other) { esp_ota_set_boot_partition(other); esp_restart(); }
  }
}
static void mark_healthy_once() {
  static bool done = false;
  if (done || millis() < 30000) return;
  done = true; boot_try = 0; proven_ok = 1;
  esp_ota_mark_app_valid_cancel_rollback();
  Serial.println("boot  marked healthy");
}

static void node_identity() {
#ifdef NODE_ID
  snprintf(node_id, sizeof node_id, "%s", NODE_ID);
#else
  // No fixed default: a default is identical on every node, which is the one bug node identity
  // exists to prevent. MAC-derived is at least unique.
  uint8_t m[6]; esp_efuse_mac_get_default(m);
  snprintf(node_id, sizeof node_id, "puc-%02x%02x%02x", m[3], m[4], m[5]);
#endif
}

// ---------------------------------------------------------------- GPS (PMTK, not UBX)
// The L86 speaks MediaTek's PMTK, so none of night_node's UBX config applies here: no CFG-VALSET,
// no TIM-TP, no NAV-PVT. Fix and satellite count come from NMEA, and the timepulse -- when there
// is a wire for it -- is set with PMTK285 rather than CFG-TP-*.
static uint32_t gps_sentences = 0, gps_valid = 0;
static int gps_fix = 0, gps_sats = 0;
static char gps_utc[16] = "--:--:--";
static char gps_last[96] = "";

static void pmtk(const char *body) {
  uint8_t cs = 0;
  for (const char *c = body; *c; c++) cs ^= (uint8_t)*c;
  Serial1.printf("$%s*%02X\r\n", body, cs);
}

static bool nmea_ok(const char *s) {          // "$....*HH" with a real checksum
  const char *star = strrchr(s, '*');
  if (s[0] != '$' || !star || strlen(star) < 3) return false;
  uint8_t cs = 0;
  for (const char *c = s + 1; c < star; c++) cs ^= (uint8_t)*c;
  return cs == (uint8_t)strtol(star + 1, nullptr, 16);
}

static void gps_line(char *s) {
  gps_sentences++;
  if (!nmea_ok(s)) return;                    // count only what CHECKSUMS. A floating RX line
  gps_valid++;                                // frames noise into things that look like sentences.
  snprintf(gps_last, sizeof gps_last, "%s", s);
  if (!strncmp(s + 3, "GGA", 3)) {
    char *f[16] = {0}; int n = 0;
    for (char *p = strtok(s, ","); p && n < 16; p = strtok(nullptr, ",")) f[n++] = p;
    if (n > 7) {
      if (f[1] && strlen(f[1]) >= 6)
        snprintf(gps_utc, sizeof gps_utc, "%.2s:%.2s:%.2s", f[1], f[1] + 2, f[1] + 4);
      gps_fix = f[6] ? atoi(f[6]) : 0;
      gps_sats = f[7] ? atoi(f[7]) : 0;
    }
  }
}

static void gps_pump() {
  static char buf[128]; static int i = 0;
  while (Serial1.available()) {
    char c = Serial1.read();
    if (c == '\n' || i >= (int)sizeof(buf) - 1) { buf[i] = 0; if (i > 6) gps_line(buf); i = 0; }
    else if (c != '\r') buf[i++] = c;
  }
}

// ---------------------------------------------------------------- PPS
static volatile uint32_t pps_count = 0, pps_int_min = 0xFFFFFFFF, pps_int_max = 0;
static volatile uint64_t pps_us_last = 0;
static void IRAM_ATTR pps_isr() {
  uint64_t now = (uint64_t)esp_timer_get_time();
  if (pps_count) {
    uint32_t d = (uint32_t)(now - pps_us_last);
    if (d < 500000) return;                   // a 1 Hz pulse cannot have edges this close
    if (d < pps_int_min) pps_int_min = d;
    if (d > pps_int_max) pps_int_max = d;
  }
  pps_us_last = now; pps_count++;
}

// ---------------------------------------------------------------- whole-bank pin scan
// The instrument that found the GPS UART. Kept aboard because the mic, SD and I2C pins are still
// unknown and this is how they get found -- by watching every safe pin on one timebase and letting
// signals separate by rate and duty, rather than guessing a pin and probing it.
static const int SAFE[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 21,
                           38, 39, 40, 41, 42, 45, 46, 47, 48};
static const int NSAFE = sizeof(SAFE) / sizeof(SAFE[0]);
#define SCAN_N 120000

// mode: 0 = leave pins as they are, 1 = pulldown, 2 = pullup.
// The pullup is the decisive one for a suspected open joint. An UNCONNECTED input follows whatever
// pull is applied, so it reads HIGH. A pin wired to a push-pull driver that is currently low does
// NOT follow it, and reads LOW. Reset-state and pulldown cannot tell those apart -- both read low
// either way, which is exactly what GPIO18 does with a 1PPS testpoint soldered to it.
static String pin_scan(uint32_t window_ms, int mode) {
  uint32_t *b0 = (uint32_t *)ps_malloc((size_t)SCAN_N * 4);
  uint32_t *b1 = (uint32_t *)ps_malloc((size_t)SCAN_N * 4);
  if (!b0 || !b1) { free(b0); free(b1); return "PSRAM alloc failed\n"; }
  if (mode) for (int k = 0; k < NSAFE; k++) pinMode(SAFE[k], mode == 2 ? INPUT_PULLUP : INPUT_PULLDOWN);
  delay(20);
  // Paced, not free-running: unpaced this fills in 0.2 s, which cannot contain a 1 Hz pulse -- the
  // first version of this reported an empty table and read as "nothing is wired".
  uint32_t n = 0;
  const uint32_t per = (window_ms * 1000UL) / SCAN_N + 1;
  uint64_t t0 = esp_timer_get_time(), next = t0, end = t0 + (uint64_t)window_ms * 1000ULL;
  while (n < SCAN_N && (uint64_t)esp_timer_get_time() < end) {
    while ((uint64_t)esp_timer_get_time() < next) {}
    next += per;
    b0[n] = GPIO.in; b1[n] = GPIO.in1.val; n++;
  }
  double secs = ((uint64_t)esp_timer_get_time() - t0) / 1e6;
  double ups = secs * 1e6 / n;
  String o = "sampled " + String(n) + " x " + String(secs, 2) + " s = " +
             String(n / secs / 1000.0, 0) + " kHz/pin\n\ngpio  edges  high%   min-run  verdict\n";
  for (int k = 0; k < NSAFE; k++) {
    int g = SAFE[k];
    uint32_t mask = 1u << (g < 32 ? g : g - 32);
    const uint32_t *bk = (g < 32) ? b0 : b1;
    int last = (bk[0] & mask) ? 1 : 0; uint32_t hi = 0, ed = 0, lastch = 0, mn = 0xFFFFFFFF;
    for (uint32_t i = 0; i < n; i++) {
      int v = (bk[i] & mask) ? 1 : 0;
      if (v) hi++;
      if (v != last) { uint32_t r = (uint32_t)((i - lastch) * ups);
                       if (r && r < mn) mn = r; ed++; lastch = i; last = v; }
    }
    char row[128];
    if (!ed) { snprintf(row, sizeof row, "%4d  held %s\n", g, hi ? "HIGH" : "low"); o += row; continue; }
    double eps = ed / secs, duty = 100.0 * hi / n;
    const char *verdict =
      (eps > 1.0 && eps < 4.0 && duty > 3 && duty < 30) ? "  <== 1 Hz PULSE ~10% duty -- 1PPS"
      : (eps > 100 && mn > 50 && mn < 200)              ? "  <== ~100 us bit -- 9600 baud UART"
      : (eps > 20000)                                   ? "  <== fast toggle -- a clock"
                                                        : "";
    snprintf(row, sizeof row, "%4d  %5lu  %5.1f%%  %7lu%s\n", g, (unsigned long)ed, duty,
             (unsigned long)(mn == 0xFFFFFFFF ? 0 : mn), verdict);
    o += row;
  }
  free(b0); free(b1);
  o += "\n(19/20 USB, 26-32 flash, 33-37 PSRAM: never touched)\n";
  return o;
}

// ---------------------------------------------------------------- HTTP
static void routes() {
  http.on("/", []() {
    char b[700];
    snprintf(b, sizeof b,
      "<pre>dama-hear PUC node %s (%s)\n\n"
      "uptime  %lus\nheap    %lu   psram %lu\n"
      "gps     fix %d, %d sats, %s   sentences %lu (%lu valid)\n"
      "pps     %lu edges on GPIO%d\n\n"
      "/status /pins /scan /scanpd /scanpu /gps /pmtk?cmd= /pps /ota /update /reboot\n</pre>",
      node_id, NODE_CLASS, (unsigned long)(millis() / 1000),
      (unsigned long)ESP.getFreeHeap(), (unsigned long)ESP.getFreePsram(),
      gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences, (unsigned long)gps_valid,
      (unsigned long)pps_count, PPS_PIN);
    http.send(200, "text/html", b);
  });

  http.on("/status", []() {
    char b[900];
    snprintf(b, sizeof b,
      "{\"node\":\"%s\",\"class\":\"%s\",\"uptime_s\":%lu,\"heap\":%lu,\"psram\":%lu,"
      "\"gps\":{\"fix\":%d,\"sats\":%d,\"utc\":\"%s\",\"sentences\":%lu,\"valid\":%lu,"
      "\"baud\":%d,\"rx_pin\":%d,\"tx_pin\":%d,\"last\":\"%s\"},"
      "\"pps\":{\"pin\":%d,\"edges\":%lu,\"interval_min_us\":%lu,\"interval_max_us\":%lu,"
      "\"wired\":%s},"
      "\"wifi\":{\"sta\":%s,\"rssi\":%d,\"ip\":\"%s\"}}",
      node_id, NODE_CLASS, (unsigned long)(millis() / 1000),
      (unsigned long)ESP.getFreeHeap(), (unsigned long)ESP.getFreePsram(),
      gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences, (unsigned long)gps_valid,
      GPS_BAUD, GPS_RX_PIN, GPS_TX_PIN, gps_last,
      PPS_PIN, (unsigned long)pps_count,
      (unsigned long)(pps_count > 1 ? pps_int_min : 0),
      (unsigned long)(pps_count > 1 ? pps_int_max : 0),
      // Not a configuration flag: it reports whether edges have ACTUALLY arrived. The wire either
      // exists and pulses or it does not, and nothing else should be allowed to claim otherwise.
      pps_count > 2 ? "true" : "false",
      sta_ok ? "true" : "false", WiFi.RSSI(),
      sta_ok ? WiFi.localIP().toString().c_str() : "0.0.0.0");
    http.send(200, "application/json", b);
  });

  http.on("/pins", []() {
    char b[760];
    snprintf(b, sizeof b,
      "gpio  role                                    how we know\n"
      "%4d  GPS RX  <- L86 TX, %d baud PMTK        measured: 9600-baud traffic, PMTK705 reply\n"
      "%4d  GPS TX  -> L86 RX                      measured: the module answers PMTK605\n"
      "%4d  PPS in  <-- THE WIRE GOES HERE         NOT ROUTED on stock hardware; add it\n"
      "  19  USB D-                                 do not touch\n"
      "  20  USB D+                                 do not touch\n"
      "26-32 SPI flash                              do not touch\n"
      "33-37 octal PSRAM                            do not touch\n\n"
      "Two 48 kHz mics, a DS3231, an environmental suite and a microSD slot are all on this board\n"
      "and their pins are NOT yet known. /scan and /scanpd are how they get found.\n",
      GPS_RX_PIN, GPS_BAUD, GPS_TX_PIN, PPS_PIN);
    http.send(200, "text/plain", b);
  });

  http.on("/scan",   []() { http.send(200, "text/plain", pin_scan(4000, 0)); });
  http.on("/scanpd", []() { http.send(200, "text/plain", pin_scan(4000, 1)); });
  http.on("/scanpu", []() { http.send(200, "text/plain", pin_scan(4000, 2)); });

  http.on("/gps", []() {
    // Raw NMEA for a few seconds, pumped here because the parser lives in loop() and a handler
    // that only delay()s would return an empty buffer.
    String o = "";
    uint32_t t0 = millis();
    while (millis() - t0 < 3000 && o.length() < 3000) {
      while (Serial1.available()) { char c = Serial1.read(); if (c != '\r') o += c; }
    }
    http.send(200, "text/plain", o.length() ? o : "(nothing at 9600 on GPIO44)\n");
  });

  http.on("/pmtk", []() {
    // e.g. /pmtk?cmd=PMTK285,4,100  -- forces the timepulse on regardless of fix, which is how
    // you test a newly-added PPS wire without waiting for a sky view.
    if (!http.hasArg("cmd")) {
      http.send(400, "text/plain",
        "/pmtk?cmd=PMTK605           ask the module its version\n"
        "/pmtk?cmd=PMTK285,4,100     timepulse ALWAYS on, 100 ms -- test a new PPS wire with this\n"
        "/pmtk?cmd=PMTK285,1,100     timepulse after first fix (the module default)\n"
        "Checksum is computed here; give the body without $ or *.\n");
      return;
    }
    String c = http.arg("cmd");
    // BOUND PMTK285. A 500 ms width in a 1000 ms period is a 50% duty timepulse, and the MT3333
    // does not accept it -- sending one stopped this module transmitting entirely and it then
    // ignored PMTK101, PMTK104 and even a bare PMTK605. Only a power cycle brought it back.
    // night_node's /tplen has clamped to 10..900 ms since it was written, for exactly this reason;
    // this endpoint was built as an unbounded passthrough and I used it to do the thing that guard
    // exists to prevent. A passthrough that can hang the hardware is not a diagnostic.
    if (c.startsWith("PMTK285")) {
      int comma = c.indexOf(',', 8);
      long w = comma > 0 ? c.substring(comma + 1).toInt() : -1;
      if (w < 10 || w > 400) {
        http.send(400, "text/plain",
          "PMTK285 pulse width must be 10..400 ms.\n\n"
          "500 ms hung this module: it stopped transmitting and ignored every command including a\n"
          "cold start, and needed a power cycle. The period is 1000 ms and a width near it is not a\n"
          "pulse. 100 ms is the module default and what /pps expects; 300 ms is about as wide as is\n"
          "worth going to make it easier to catch on a meter (~1.0 V average against 3.3 V).\n");
        return;
      }
    }
    pmtk(c.c_str());
    String o = "sent $" + c + "\n\nreply:\n";
    uint32_t t0 = millis();
    while (millis() - t0 < 2000) {
      while (Serial1.available()) { char ch = Serial1.read(); if (ch != '\r') o += ch; }
    }
    http.send(200, "text/plain", o);
  });

  http.on("/pps", []() {
    uint32_t e0 = pps_count; delay(2500); uint32_t e1 = pps_count;
    char b[420];
    snprintf(b, sizeof b,
      "GPIO%d over 2.5 s: %lu edges (total %lu)\n\n%s\n",
      PPS_PIN, (unsigned long)(e1 - e0), (unsigned long)e1,
      (e1 - e0) >= 2 ? "PULSING."
        : "No edges. Either the wire from L86 pin 11 is not there yet, or the module has no fix\n"
          "and its timepulse is off -- force it with /pmtk?cmd=PMTK285,4,100 and try again.");
    http.send(200, "text/plain", b);
  });

  http.on("/ota", []() {
    const esp_partition_t *r = esp_ota_get_running_partition();
    char b[300];
    snprintf(b, sizeof b, "running %s @ 0x%06x\nboot_try %lu (reverts after %d)\nhealthy %s\n\n"
                          "push: curl -F firmware=@<bin> http://%s.local/update\n",
             r ? r->label : "?", r ? (unsigned)r->address : 0,
             (unsigned long)boot_try, BOOT_MAX_TRIES, proven_ok ? "yes" : "not yet", node_id);
    http.send(200, "text/plain", b);
  });

  http.on("/reboot", HTTP_POST, []() { http.send(200, "text/plain", "rebooting\n"); delay(200); ESP.restart(); });

  http.on("/update", HTTP_POST, []() {
    http.send(200, "text/plain", Update.hasError() ? "FAILED\n" : "OK, rebooting into the new image\n");
    delay(300); ESP.restart();
  }, []() {
    HTTPUpload &u = http.upload();
    if (u.status == UPLOAD_FILE_START) {
      Serial.printf("ota   %s\n", u.filename.c_str());
      if (!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
    } else if (u.status == UPLOAD_FILE_WRITE) {
      if (Update.write(u.buf, u.currentSize) != u.currentSize) Update.printError(Serial);
    } else if (u.status == UPLOAD_FILE_END) {
      if (Update.end(true)) Serial.printf("ota   %u bytes ok\n", u.totalSize);
      else Update.printError(Serial);
    }
  });
}

void setup() {
  boot_guard();
  Serial.begin(115200);
  delay(400);
  node_identity();
  Serial.printf("\n=== dama-hear PUC node %s (%s) ===\n", node_id, NODE_CLASS);

  Serial1.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.printf("gps   L86 on RX=GPIO%d TX=GPIO%d @ %d\n", GPS_RX_PIN, GPS_TX_PIN, GPS_BAUD);

  pinMode(PPS_PIN, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(PPS_PIN), pps_isr, RISING);
  Serial.printf("pps   watching GPIO%d (nothing is wired to it yet)\n", PPS_PIN);

  for (int k = 0; k < WIFI_N && !sta_ok; k++) {
    WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSIDS[k], WIFI_PASSES[k]);
    Serial.printf("wifi  trying network %d/%d", k + 1, WIFI_N);
    for (int t = 0; t < 40 && WiFi.status() != WL_CONNECTED; t++) { delay(250); Serial.print("."); }
    sta_ok = WiFi.status() == WL_CONNECTED;
    Serial.println(sta_ok ? " joined" : " no");
  }
  if (sta_ok) Serial.printf("wifi  http://%s/\n", WiFi.localIP().toString().c_str());
  else {
    // AP fallback, named per node: two of these must never claim one SSID.
    char ap[40]; snprintf(ap, sizeof ap, "dama-hear-%s", node_id);
    WiFi.mode(WIFI_AP); WiFi.softAP(ap, "damahear");
    Serial.printf("wifi  AP \"%s\" pass damahear  http://%s/\n", ap,
                  WiFi.softAPIP().toString().c_str());
  }
  if (MDNS.begin(node_id)) Serial.printf("mdns  http://%s.local/\n", node_id);

  routes();
  http.begin();
  Serial.println("http  up -- this board never needs the cable again\n");
}

void loop() {
  http.handleClient();
  gps_pump();
  mark_healthy_once();
  static uint32_t last = 0;
  if (millis() - last > 30000) {
    last = millis();
    Serial.printf("[%6lus] fix %d/%d sats  nmea %lu/%lu valid  pps %lu\n",
                  (unsigned long)(millis() / 1000), gps_fix, gps_sats,
                  (unsigned long)gps_valid, (unsigned long)gps_sentences,
                  (unsigned long)pps_count);
  }
}
