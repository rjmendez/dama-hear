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
#include <Wire.h>
#include "driver/gpio.h"
#include <ESP_I2S.h>
#include <WiFiUdp.h>
#include <time.h>
#include <hear_boot.h>   // ⚠️unconditional: nesting it in the secrets guard
                          // builds a sketch with no failback and no error

// Declared up here because the .ino preprocessor inserts function prototypes ahead of the file's
// own declarations; a type named in one of those prototypes must already exist.
struct NtpResult {
  bool ok = false; const char *err = "";
  double offset_s = 0, rtt_best = 0, rtt_min = 0, rtt_max = 0, root_dist_s = 0;
  int n = 0; uint8_t stratum = 0; char refid[5] = {0};
};

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#include <hear_wifi_guard.h>   // no credentials -> compile error, not a silent AP-only node
#ifndef NODE_CLASS
#define NODE_CLASS "puc-ntp"        // until 1PPS is wired. hear/nodeclass.py refuses this class
#endif                              // as a TDoA arrival source, and is right to.

#define GPS_RX_PIN 44               // module TX -> here
#define GPS_TX_PIN 43               // here -> module RX
#define GPS_BAUD   9600
// ⚠️17, NOT 18, AND THIS IS THE SECOND TIME. GPIO18 was the landing pad for two months on the
// strength of "reads low and is not a strapping pin" -- both true, both insufficient. The vendor
// dump configures GPIO18 as an INPUT (gpio_config pin_bit_mask 0x40000), and measured on the live
// board with /scanpu and /scanpd it reads LOW against the ESP32's ~45k internal PULLUP:
//
//     gpio   pullup   pulldown   reading
//       15    HIGH      low      floats -- free
//       16    HIGH      low      floats -- free
//       17    HIGH      low      free, and where the joint is
//       18    low       low      HELD LOW -- something external owns this net
//       39    low       low      HELD LOW -- and puc.h's FREE_PADS lists it, wrongly
//       38    8 edges, 50% duty -- the DS3231 1 Hz, which is what proves the scan can see 1 Hz
//
// A floating pin follows whichever internal resistor is engaged; 18 and 39 do not. The L86's 1PPS
// is a push-pull output (Hardware Design Table 3: VOHmin 2.4 V), so landing it on 18 puts two
// drivers on one net. GPIO17 is physical pin 10 on ESP32-S3-WROOM-1, pad silkscreen IO17.
//
// ⚠️THIS LINE HAS BEEN REVERTED TWICE, BY TWO DIFFERENT ROUTES, AND THAT IS THE REAL HAZARD.
// First a6bbdab wrote the correction into firmware/boards/puc.h, which `grep -rn "puc\.h"` shows
// is included by NOTHING -- so the header held -1 while this file, the one that compiles, said 18.
// Then cfe3ec4 fixed it here and b2d47de ("platform: one failback...") reverted it back to 18 as a
// side effect of a 125-line refactor from a branch carrying an older copy of this file. Neither
// revert was noticed, because nothing compares the compiled pin against the measurement.
// puc.h now points HERE as the file that builds. Do not let a stale branch quietly undo it again.
#define PPS_PIN    17               // L86 pin 6 (1PPS) lands here. Wired 2026-09-09.

static char node_id[24];
static WebServer http(80);
static bool sta_ok = false;

// ---------------------------------------------------------------- boot failback
// Same shape as night_node: count boots in RTC memory, and flip back if a new image never proves
// itself. It does NOT rely on the bootloader's rollback, which this core's prebuilt bootloader may
// not have enabled -- depending on that would be depending on something unverified.
// Failback lives in hear_platform/hear_boot.{h,cpp}. It used to be a SECOND COPY here, and
// this copy was the broken one: mark_healthy_once() marked the image healthy after 30 s
// without ever checking sta_ok, which set proven_ok and switched off the partition revert
// permanently -- on a node that tracks sta_ok in seven other places. The shared version takes
// reachability as an argument so it cannot be left out.
// ⚠️The RTC magic changed with the move, so the FIRST boot on this build resets the counter
// once. That is a one-off, and it fails safe: a fresh counter cannot trigger a spurious revert.


// Whether power was actually removed is not something to infer from "I plugged it back in", and a
// low uptime proves nothing when OTAs reboot this thing several times an hour. The chip knows:
// ESP_RST_POWERON means the supply was genuinely interrupted, SW/EXT/PANIC mean it was not.
static const char *reset_name() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "POWERON";
    case ESP_RST_SW:        return "SW";
    case ESP_RST_PANIC:     return "PANIC";
    case ESP_RST_EXT:       return "EXT";
    case ESP_RST_BROWNOUT:  return "BROWNOUT";
    case ESP_RST_WDT:       return "WDT";
    case ESP_RST_TASK_WDT:  return "TASK_WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    default:                return "OTHER";
  }
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
// ---- I2C discovery ---------------------------------------------------------------------------
// The DS3231, the light sensor and the rest of the environmental suite are all on I2C and NONE of
// their pins are known. /scan cannot find them and never will: an idle I2C bus does not toggle, so
// a passive listener sees two pins sitting high and nothing else. What /scan CAN do is narrow the
// search, and it did -- a line with an external pullup stays high against the ESP32's ~45k internal
// pulldown, and on this board exactly eleven do: 6 7 9 10 11 12 13 14 45 47 48. That set is I2C
// plus the SD card's CMD and D0-D3, which carry pullups for the same reason. Telling the two apart
// needs a START and an address, which is what this is.
//
// SAFETY. I2C is open-drain: this only ever pulls a line LOW, never drives it high. Against a pin
// that turns out to be someone else's output the worst case is a brief contention current, not two
// push-pull drivers fighting -- which is why this is an acceptable thing to do to an unmapped board
// and driving a candidate I2S clock is not. Each pair is released before the next is tried, so a
// pair that wedges the bus is named in the output instead of poisoning every row after it.
//
// GPIO45 and 46 are strapping pins. They are sampled at reset, not at runtime, so using them now is
// harmless -- but a hit on either is reported with that caveat attached rather than as a plain find.
static const int I2C_CAND[] = {6, 7, 9, 10, 11, 12, 13, 14, 45, 47, 48};
static const int NI2C_CAND = sizeof(I2C_CAND) / sizeof(I2C_CAND[0]);

// An address is an address. Several real parts share one, so these are CANDIDATES and the output
// says so; only the ones with a readable ID register below get upgraded from guess to confirmed.
static const char *i2c_hint(uint8_t a) {
  switch (a) {
    case 0x0D: return "QMC5883 magnetometer";
    case 0x10: return "VEML7700/VEML6030/VEML6075 light";
    case 0x18: case 0x19: return "LIS3DH accel";
    case 0x23: return "BH1750 light";
    case 0x29: return "TSL2591/TSL2561 light";
    case 0x38: return "AHT20 humidity";
    case 0x39: return "APDS9960 light+gesture / TSL2561";
    case 0x40: return "HTU21/Si7021 humidity";
    case 0x44: case 0x45: return "SHT3x/SHT4x humidity  OR  OPT3001 light";
    case 0x46: case 0x47: return "OPT3001 light";
    case 0x4A: case 0x4B: return "MAX44009 light";
    case 0x51: return "PCF8563 RTC";
    case 0x53: return "LTR390 UV/ambient light";
    case 0x57: return "AT24C32 EEPROM (ships ON DS3231 breakouts)";
    case 0x58: return "SGP30 VOC";
    case 0x5C: return "BH1750 (ADDR high) OR LPS22 pressure";
    case 0x60: return "SI1145 light/UV";
    case 0x62: return "SCD4x CO2";
    case 0x68: return "DS3231/DS1307 RTC  OR  MPU6050 IMU";
    case 0x76: case 0x77: return "BME280/BMP280/BME680 environmental";
    default:   return "";
  }
}

static bool i2c_begin_default() {
  Wire.end();
  if (!Wire.begin(47, 48, 100000)) return false;
  Wire.setTimeOut(10);
  return true;
}

static bool i2c_rd(uint8_t a, uint8_t reg, uint8_t *v) {
  Wire.beginTransmission(a); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)a, 1) != 1) return false;
  *v = Wire.read(); return true;
}

// Turn a guess into a measurement where the part offers a way. Anything not listed here stays a
// guess and is printed as one -- an address that ACKs proves something answered, not what it is.
static String i2c_confirm(uint8_t a) {
  uint8_t v;
  if ((a == 0x76 || a == 0x77) && i2c_rd(a, 0xD0, &v)) {
    if (v == 0x60) return " CONFIRMED BME280 (id 0x60)";
    if (v == 0x61) return " CONFIRMED BME680 (id 0x61)";
    if (v == 0x58) return " CONFIRMED BMP280 (id 0x58)";
    return " id reg 0xD0 = 0x" + String(v, HEX) + ", not a BME/BMP";
  }
  if (a == 0x29 && i2c_rd(a, 0xB2, &v) && v == 0x50) return " CONFIRMED TSL2591 (id 0x50)";
  if (a == 0x53 && i2c_rd(a, 0x06, &v) && (v & 0xF0) == 0xB0) return " CONFIRMED LTR390 (part 0xB)";
  if (a == 0x68) {
    // DS3231 carries a temperature register pair the DS1307 and MPU6050 do not. A plausible room
    // reading is weak evidence on its own, so the status register's reserved bits are checked too.
    uint8_t t, st;
    if (i2c_rd(a, 0x11, &t) && i2c_rd(a, 0x0F, &st) && !(st & 0x70) && (int8_t)t > -40 && (int8_t)t < 85)
      return " CONFIRMED DS3231 (temp " + String((int8_t)t) + " C, status 0x" + String(st, HEX) + ")";
    return " 0x68 answered but does not look like a DS3231";
  }
  return "";
}

static String i2c_try(int sda, int scl) {
  Wire.end();
  if (!Wire.begin(sda, scl, 100000)) { Wire.end(); return ""; }
  Wire.setTimeOut(10);
  int hits = 0; String found = "";
  for (uint8_t a = 0x08; a <= 0x77; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      hits++;
      if (hits <= 12) {
        found += "      0x" + String(a, HEX) + "  " + i2c_hint(a) + i2c_confirm(a) + "\n";
      }
    }
  }
  String head = "";
  // EVERY address acking is not 112 devices, it is SDA held low -- the classic scanner result that
  // reads as a jackpot. It is reported as the wiring fault it is, and the pair is not a find.
  if (hits > 20) head = "  SDA=" + String(sda) + " SCL=" + String(scl) + ": " + String(hits) +
                        " addresses ACKed -- SDA is stuck low, this is NOT a bus\n";
  else if (hits)  head = "  SDA=" + String(sda) + " SCL=" + String(scl) + ": " + String(hits) +
                        " device(s)\n" + found +
                        ((sda == 45 || scl == 45 || sda == 46 || scl == 46)
                         ? "      (uses a strapping pin -- fine at runtime, note it before soldering)\n" : "");
  Wire.end();
  pinMode(sda, INPUT); pinMode(scl, INPUT);
  return head;
}

// Generic register read. An address that ACKs proves something answered, not what it is, and the
// hint table above is a list of suspects rather than an identification. This is how a suspect gets
// eliminated -- WHO_AM_I on 0x0F, a part ID at 0x92, an EEPROM byte -- without a reflash per guess.
static String i2c_regread(int sda, int scl, uint8_t addr, uint8_t reg, int n, bool raw) {
  Wire.end();
  if (!Wire.begin(sda, scl, 100000)) { Wire.end(); return "bus would not start\n"; }
  Wire.setTimeOut(10);
  String o = "";
  if (n < 1) n = 1;
  if (n > 32) n = 32;
  Wire.beginTransmission(addr);
  if (!raw) Wire.write(reg);
  int tx = Wire.endTransmission(false);
  if (tx != 0) { Wire.end(); return "0x" + String(addr, HEX) + ": no ACK on the register write (" +
                                    String(tx) + ")\n"; }
  int got = Wire.requestFrom((int)addr, n);
  o = "0x" + String(addr, HEX) + " reg 0x" + String(reg, HEX) + " x" + String(n) + " ->";
  for (int i = 0; i < got; i++) { uint8_t v = Wire.read(); o += " 0x" + String(v, HEX); }
  if (!got) o += " (no data)";
  o += "\n";
  Wire.end();
  pinMode(sda, INPUT); pinMode(scl, INPUT);
  return o;
}

// The DS3231 decoded, because "0x68 answered" and "the clock has kept time" are different claims
// and only the second one is worth anything for holdover. OSF is the load-bearing bit: set means
// the oscillator stopped at some point, so whatever the registers say is not a time.
static String rtc_read(int sda, int scl) {
  Wire.end();
  if (!Wire.begin(sda, scl, 100000)) { Wire.end(); return "bus would not start\n"; }
  Wire.setTimeOut(10);
  uint8_t r[19];
  Wire.beginTransmission(0x68); Wire.write((uint8_t)0x00);
  if (Wire.endTransmission(false) != 0) { Wire.end(); return "no DS3231 at 0x68\n"; }
  int got = Wire.requestFrom(0x68, 19);
  for (int i = 0; i < got && i < 19; i++) r[i] = Wire.read();
  Wire.end();
  pinMode(sda, INPUT); pinMode(scl, INPUT);
  if (got < 19) return "short read (" + String(got) + " of 19)\n";
  auto bcd = [](uint8_t v) { return (v >> 4) * 10 + (v & 0x0F); };
  char b[420];
  snprintf(b, sizeof b,
    "DS3231 on SDA=%d SCL=%d\n"
    "  time    20%02d-%02d-%02d %02d:%02d:%02d  (register contents, timezone unknown)\n"
    "  status  0x%02X   OSF=%d %s\n"
    "  ctrl    0x%02X   EN32kHz=%d  INTCN=%d  alarms=%d%d\n"
    "  aging   %d\n"
    "  temp    %.2f C\n",
    sda, scl, bcd(r[6]), bcd(r[5] & 0x1F), bcd(r[4]), bcd(r[2] & 0x3F), bcd(r[1]), bcd(r[0] & 0x7F),
    r[15], (r[15] >> 7) & 1,
    (r[15] >> 7) & 1 ? "-- THE OSCILLATOR STOPPED; this is not a time" : "-- ran continuously",
    r[14], (r[15] >> 3) & 1, r[14] & 4, (r[14] >> 1) & 1, r[14] & 1,
    (int8_t)r[16], (int8_t)r[17] + ((r[18] >> 6) * 0.25));
  return String(b);
}

// ---- PDM microphone discovery ----------------------------------------------------------------
// The two mics are the last unmapped thing on the board and /scan cannot see them: a PDM mic emits
// nothing until it is clocked, so with no clock running its DIN sits static and looks like an
// unconnected pin. The board profile blames "peripherals unpowered" for that; the I2C sweep
// falsified it -- six devices answer on 47/48, so the sensor rail IS live. The mic is silent
// because nobody is clocking it, which is a thing we can change.
//
// SEARCH SHAPE. Not CLK x DIN pairs -- drive ONE candidate clock and watch every other pin at once,
// which turns an N*M sweep into N passes and drives exactly one pin at a time. The sampler runs at
// ~29 kHz/pin and a clocked PDM line toggles near CLK/2, so it is aliased far beyond resolving --
// but resolving it is not the question. "This pin started changing state when I applied a clock,
// and was static before" is the whole measurement, and aliasing preserves that.
//
// WHY THIS IS SAFE TO DO TO AN UNMAPPED BOARD, stated because the I2C sweep's safety argument does
// NOT carry over. I2C is open-drain and only ever pulls low; this drives push-pull, so a pin that
// turns out to be someone else's output means two drivers fighting. The evidence that bounds it:
// under an internal pullup only GPIO 8, 18 and 39 stayed low, so only those three are actively
// driven -- every other SAFE pin followed a ~45k resistor, which nothing was contesting. Those
// three are excluded, 47/48 are excluded because the I2C bus lives there, and the drive strength is
// set to the weakest the part offers so even a wrong guess is current-limited.
#define PDM_PROBE_HZ   2000000
#define PDM_SAMP       12000

static const int PDM_SKIP[] = {8, 18, 39, 47, 48};   // 8/18/39 driven; 47/48 are the I2C bus
static bool pdm_skip(int g) {
  for (unsigned i = 0; i < sizeof(PDM_SKIP) / sizeof(PDM_SKIP[0]); i++) if (PDM_SKIP[i] == g) return true;
  return false;
}

// Edge count per SAFE pin over a short window. Returned by value so a caller can diff two passes.
static void pdm_sample(uint32_t *edges) {
  uint32_t last0 = GPIO.in, last1 = GPIO.in1.val;
  for (int k = 0; k < NSAFE; k++) edges[k] = 0;
  for (uint32_t i = 0; i < PDM_SAMP; i++) {
    uint32_t v0 = GPIO.in, v1 = GPIO.in1.val;
    uint32_t d0 = v0 ^ last0, d1 = v1 ^ last1;
    if (d0 || d1) {
      for (int k = 0; k < NSAFE; k++) {
        int g = SAFE[k];
        uint32_t m = 1u << (g < 32 ? g : g - 32);
        if ((g < 32 ? d0 : d1) & m) edges[k]++;
      }
    }
    last0 = v0; last1 = v1;
  }
}

static String pdm_scan(int only_clk) {
  uint32_t base[NSAFE], live[NSAFE];
  String o = "PDM mic hunt: drive one candidate clock at " + String(PDM_PROBE_HZ / 1000) +
             " kHz, watch every other pin.\n"
             "skipped: 8 18 39 (actively driven) 47 48 (I2C bus). Drive strength: weakest.\n\n";
  pdm_sample(base);
  String basel = "";
  for (int k = 0; k < NSAFE; k++) if (base[k]) basel += " " + String(SAFE[k]) + ":" + String(base[k]);
  o += basel.length() ? "baseline, no clock -- ALREADY TOGGLING:" + basel + "\n\n"
                      : "baseline, no clock -- every pin static, as expected\n\n";
  int hits = 0;
  for (int k = 0; k < NSAFE; k++) {
    int clk = SAFE[k];
    if (pdm_skip(clk)) continue;
    if (only_clk >= 0 && clk != only_clk) continue;
    gpio_set_drive_capability((gpio_num_t)clk, GPIO_DRIVE_CAP_0);
    if (!ledcAttach(clk, PDM_PROBE_HZ, 2)) { o += "  clk " + String(clk) + ": ledc refused\n"; continue; }
    ledcWrite(clk, 2);
    delay(5);
    pdm_sample(live);
    ledcDetach(clk);
    pinMode(clk, INPUT);
    String woke = "";
    for (int j = 0; j < NSAFE; j++) {
      if (SAFE[j] == clk) continue;
      // 8x the quiet-pass count and at least 50 transitions: a pin that merely picked up a little
      // crosstalk from a 2 MHz neighbour is not a microphone answering.
      if (live[j] > 50 && live[j] > base[j] * 8 + 50)
        woke += "  DIN? gpio " + String(SAFE[j]) + " " + String(live[j]) + " edges (was " +
                String(base[j]) + ")\n";
    }
    if (woke.length()) { hits++; o += "clk " + String(clk) + " ->\n" + woke; }
  }
  o += "\n";
  if (!hits) o += "No pin answered any candidate clock. That is a result: either the mics are on a\n"
                  "rail this firmware has not enabled, or their CLK is one of the excluded pins, or\n"
                  "the DIN is outside SAFE. It is NOT evidence that there are no mics.\n";
  return o;
}

// ---- microphone confirmation -----------------------------------------------------------------
// /pdmscan finds a pin that DRIVES when another is clocked. That is not a microphone; it is a pin
// that drives. The Sense bring-up set the bar for this board family -- "16 kHz, 99% non-zero, room
// noise at rms 2573 / peak 7923" -- and it is the right bar, because a stuck line, a floating input
// and a mic all produce edges while only one of them produces a signal with structure.
//
// What is reported: mean, rms about the mean, peak, and the fraction of samples that are non-zero
// and that are rail-pinned. A DC-offset constant reads rms ~0. A line stuck at a rail reads
// pinned ~100%. Only a mic reads a small mean, a non-trivial rms and no pinning.
static I2SClass pdm_i2s;

static String mic_capture(int clk, int din, int fs, bool stereo) {
  static int16_t buf[4096];
  pdm_i2s.end();
  pdm_i2s.setPinsPdmRx(clk, din);
  if (!pdm_i2s.begin(I2S_MODE_PDM_RX, fs, I2S_DATA_BIT_WIDTH_16BIT,
                     stereo ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO))
    return "i2s PDM RX would not start on clk=" + String(clk) + " din=" + String(din) + "\n";
  // A PDM mic has a wake-up: the first blocks after the clock starts are its filter settling, not
  // the room. Read one buffer and throw it away before measuring anything.
  pdm_i2s.readBytes((char *)buf, sizeof buf);
  int got = pdm_i2s.readBytes((char *)buf, sizeof buf);
  pdm_i2s.end();
  pinMode(clk, INPUT); pinMode(din, INPUT);
  int n = got / 2;
  if (n < 64) return "only " + String(n) + " samples came back\n";

  auto stat = [&](int off, int step, String tag) {
    double sum = 0; int cnt = 0;
    for (int i = off; i < n; i += step) { sum += buf[i]; cnt++; }
    double mean = sum / cnt, ss = 0; int nz = 0, pinned = 0, peak = 0;
    for (int i = off; i < n; i += step) {
      double d = buf[i] - mean; ss += d * d;
      if (buf[i]) nz++;
      if (buf[i] >= 32700 || buf[i] <= -32700) pinned++;
      if (abs(buf[i]) > peak) peak = abs(buf[i]);
    }
    char b[160];
    snprintf(b, sizeof b, "  %-5s n=%4d  mean %8.1f  rms %8.1f  peak %6d  non-zero %5.1f%%  pinned %5.1f%%\n",
             tag.c_str(), cnt, mean, sqrt(ss / cnt), peak, 100.0 * nz / cnt, 100.0 * pinned / cnt);
    return String(b);
  };
  String o = "PDM capture clk=" + String(clk) + " din=" + String(din) + " at " + String(fs) +
             " Hz, " + String(stereo ? "stereo" : "mono") + ", " + String(n) + " samples\n";
  if (stereo) { o += stat(0, 2, "left"); o += stat(1, 2, "right"); }
  else o += stat(0, 1, "mono");
  o += "  (a constant reads rms ~0; a line stuck at a rail reads pinned ~100%)\n";
  return o;
}

// ---- time ------------------------------------------------------------------------------------
// This board is called puc-ntp and until now had NO time client of any kind: no NTP, no RTC read,
// no `time` in /status. hear/nodeclass.py credits the class with t_sigma 3 ms = 1.03 m, which was
// describing a capability that did not exist. The L86 is not answering, so GPS PPS is not the
// route; the route is the LAN's own stratum-1 server and the DS3231 for holdover between syncs.
//
// ⚠️WHAT IS MEASURED VERSUS WHAT IS CLAIMED. Arduino's configTime() sets the clock and tells you
// nothing about how well, which is exactly the shape of claim this repo keeps having to retract.
// So this speaks SNTP itself and keeps all four timestamps, because the useful number is not the
// offset -- it is the bound on the offset. NTP's error from path asymmetry is bounded by HALF THE
// ROUND TRIP: if the request and reply take different times, the midpoint is wrong by at most
// (rtt/2). That bound is honest, it needs no assumption about the network being symmetric, and it
// is what /time reports as the node's timestamp uncertainty. rtt_spread across samples says
// whether the link is steady enough for the best-rtt sample to mean anything.
#define NTP_DEFAULT_SERVER "172.16.100.9"      // rtk-base: stratum 1, refid PPS, root distance 23 us
#define NTP_SAMPLES 8


static NtpResult ntp_query(const char *host, int want) {
  NtpResult r;
  WiFiUDP udp;
  if (!udp.begin(0)) { r.err = "no local UDP port"; return r; }
  double best_off = 0, best_rtt = 1e9, mn = 1e9, mx = 0;
  for (int i = 0; i < want; i++) {
    uint8_t pkt[48] = {0}; pkt[0] = 0x1B;               // LI 0, VN 3, mode 3 (client)
    uint64_t t1 = (uint64_t)esp_timer_get_time();
    struct timeval tv1; gettimeofday(&tv1, nullptr);
    udp.beginPacket(host, 123); udp.write(pkt, 48); udp.endPacket();
    uint32_t deadline = millis() + 400;
    int len = 0;
    while (millis() < deadline && !(len = udp.parsePacket())) delay(1);
    if (len < 48) continue;
    udp.read(pkt, 48);
    uint64_t t4 = (uint64_t)esp_timer_get_time();
    double rtt = (t4 - t1) / 1e6;
    auto be32 = [&](int o) { return ((uint32_t)pkt[o] << 24) | ((uint32_t)pkt[o+1] << 16) |
                                    ((uint32_t)pkt[o+2] << 8) | pkt[o+3]; };
    auto ts = [&](int o) { return (double)be32(o) - 2208988800.0 + be32(o + 4) / 4294967296.0; };
    double local1 = tv1.tv_sec + tv1.tv_usec / 1e6;
    double t2 = ts(32), t3 = ts(40), local4 = local1 + rtt;
    double off = ((t2 - local1) + (t3 - local4)) / 2.0;
    if (rtt < best_rtt) { best_rtt = rtt; best_off = off; r.stratum = pkt[1];
                          memcpy(r.refid, pkt + 12, 4);
                          r.root_dist_s = (be32(4) / 65536.0) / 2.0 + be32(8) / 65536.0; }
    if (rtt < mn) mn = rtt;
    if (rtt > mx) mx = rtt;
    r.n++;
    delay(60);
  }
  udp.stop();
  if (!r.n) { r.err = "no reply"; return r; }
  // The BEST round trip is the least-delayed exchange seen, so it carries the tightest asymmetry
  // bound. Taking a median offset instead would average in the queued samples, which is the
  // opposite of what is wanted.
  r.ok = true; r.offset_s = best_off; r.rtt_best = best_rtt; r.rtt_min = mn; r.rtt_max = mx;
  return r;
}

static bool ds3231_write_time(int sda, int scl, time_t utc) {
  struct tm t; gmtime_r(&utc, &t);
  auto b = [](int v) { return (uint8_t)(((v / 10) << 4) | (v % 10)); };
  Wire.end();
  if (!Wire.begin(sda, scl, 100000)) return false;
  Wire.beginTransmission(0x68);
  Wire.write((uint8_t)0x00);
  Wire.write(b(t.tm_sec)); Wire.write(b(t.tm_min)); Wire.write(b(t.tm_hour));
  Wire.write((uint8_t)(t.tm_wday + 1)); Wire.write(b(t.tm_mday));
  Wire.write(b(t.tm_mon + 1)); Wire.write(b(t.tm_year % 100));
  bool ok = Wire.endTransmission() == 0;
  // Clear OSF. It latches on any oscillator stop and stays set until written, so leaving it set
  // after a deliberate set would make every later read say "this is not a time".
  if (ok) { uint8_t st; if (i2c_rd(0x68, 0x0F, &st)) {
      Wire.beginTransmission(0x68); Wire.write((uint8_t)0x0F); Wire.write((uint8_t)(st & 0x7F));
      Wire.endTransmission(); } }
  Wire.end(); pinMode(sda, INPUT); pinMode(scl, INPUT);
  return ok;
}

static bool ds3231_read_time(int sda, int scl, time_t *out, bool *osf) {
  Wire.end();
  if (!Wire.begin(sda, scl, 100000)) return false;
  uint8_t r[19];
  Wire.beginTransmission(0x68); Wire.write((uint8_t)0x00);
  if (Wire.endTransmission(false) != 0) { Wire.end(); return false; }
  int got = Wire.requestFrom(0x68, 19);
  for (int i = 0; i < got && i < 19; i++) r[i] = Wire.read();
  Wire.end(); pinMode(sda, INPUT); pinMode(scl, INPUT);
  if (got < 19) return false;
  auto d = [](uint8_t v) { return (v >> 4) * 10 + (v & 0x0F); };
  struct tm t = {};
  t.tm_sec = d(r[0] & 0x7F); t.tm_min = d(r[1]); t.tm_hour = d(r[2] & 0x3F);
  t.tm_mday = d(r[4]); t.tm_mon = d(r[5] & 0x1F) - 1; t.tm_year = d(r[6]) + 100;
  *osf = (r[15] >> 7) & 1;
  *out = mktime(&t) - _timezone;      // registers are UTC by our own convention; see /timesync
  return true;
}

static double  g_sync_bound_s = -1;   // rtt/2 of the exchange the clock was last set from
static uint32_t g_sync_at_ms  = 0;
static double  g_sync_off_s   = 0;
static int     g_sync_count   = 0;

static String i2c_sweep() {
  String o = "I2C sweep over the 11 externally-pulled-up pins (both orders; SDA/SCL is not symmetric)\n"
             "candidates: 6 7 9 10 11 12 13 14 45 47 48   -- from /scan vs /scanpd\n\n";
  int pairs = 0, found = 0;
  for (int i = 0; i < NI2C_CAND; i++) {
    for (int j = 0; j < NI2C_CAND; j++) {
      if (i == j) continue;
      pairs++;
      String r = i2c_try(I2C_CAND[i], I2C_CAND[j]);
      if (r.length()) { o += r; found++; }
    }
  }
  o += "\n" + String(pairs) + " pairs tried, " + String(found) + " answered.\n";
  if (!found) o += "NOTHING ANSWERED. That is a result, not a failure: it means the bus is not among\n"
                   "these eleven, or its devices are unpowered. Next is a sweep over every SAFE pin.\n";
  return o;
}

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
      "/status /pins /i2c /i2creg /rtc /time /timesync /sqw /pdmscan /mic /scan /scanpd /scanpu /gps /pmtk?cmd= /pps /ota /update /reboot\n</pre>",
      node_id, NODE_CLASS, (unsigned long)(millis() / 1000),
      (unsigned long)ESP.getFreeHeap(), (unsigned long)ESP.getFreePsram(),
      gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences, (unsigned long)gps_valid,
      (unsigned long)pps_count, PPS_PIN);
    http.send(200, "text/html", b);
  });

  http.on("/status", []() {
    char b[900];
    snprintf(b, sizeof b,
      "{\"node\":\"%s\",\"class\":\"%s\",\"uptime_s\":%lu,"
      "\"reset\":\"%s\",\"power_cycled\":%s,\"heap\":%lu,\"psram\":%lu,"
      "\"gps\":{\"fix\":%d,\"sats\":%d,\"utc\":\"%s\",\"sentences\":%lu,\"valid\":%lu,"
      "\"baud\":%d,\"rx_pin\":%d,\"tx_pin\":%d,\"last\":\"%s\"},"
      "\"pps\":{\"pin\":%d,\"edges\":%lu,\"interval_min_us\":%lu,\"interval_max_us\":%lu,"
      "\"wired\":%s},"
      // configured is a BUILD fact (were there credentials at all); sta is the link state.
      "\"wifi\":{\"configured\":%s,\"sta\":%s,\"rssi\":%d,\"ip\":\"%s\"}}",
      node_id, NODE_CLASS, (unsigned long)(millis() / 1000),
      reset_name(), esp_reset_reason() == ESP_RST_POWERON ? "true" : "false",
      (unsigned long)ESP.getFreeHeap(), (unsigned long)ESP.getFreePsram(),
      gps_fix, gps_sats, gps_utc, (unsigned long)gps_sentences, (unsigned long)gps_valid,
      GPS_BAUD, GPS_RX_PIN, GPS_TX_PIN, gps_last,
      PPS_PIN, (unsigned long)pps_count,
      (unsigned long)(pps_count > 1 ? pps_int_min : 0),
      (unsigned long)(pps_count > 1 ? pps_int_max : 0),
      // Not a configuration flag: it reports whether edges have ACTUALLY arrived. The wire either
      // exists and pulses or it does not, and nothing else should be allowed to claim otherwise.
      pps_count > 2 ? "true" : "false",
      HEAR_WIFI_CONFIGURED ? "true" : "false", sta_ok ? "true" : "false", WiFi.RSSI(),
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
      "   6  PDM mic CLK -> both mics             measured: /pdmscan, then real audio on /mic\n"
      "   7  PDM mic DIN <- both mics (stereo)     measured: L rms 46.1, R rms 36.1, they differ\n"
      "  38  DS3231 SQW <- 1 Hz, open-drain        measured: /sqw then /scanpu; off/on is causal\n"
      "  47  I2C SDA                               measured: /i2c, 6 devices answered\n"
      "  48  I2C SCL                               measured: /i2c\n\n"
      "On the bus: 0x18 LIS3DH, 0x1C LIS3MDL, 0x39 AS7341(likely), 0x50 blank EEPROM,\n"
      "0x68 DS3231 (confirmed, OSF=0), 0x76 BME680 (confirmed). The microSD pins are still\n"
      "unknown -- the nine remaining pulled-up pins are where its CMD and D0-D3 should be.\n",
      GPS_RX_PIN, GPS_BAUD, GPS_TX_PIN, PPS_PIN);
    http.send(200, "text/plain", b);
  });

  http.on("/scan",   []() { http.send(200, "text/plain", pin_scan(4000, 0)); });
  http.on("/scanpd", []() { http.send(200, "text/plain", pin_scan(4000, 1)); });
  http.on("/scanpu", []() { http.send(200, "text/plain", pin_scan(4000, 2)); });

  // /i2c sweeps the pullup candidates; /i2c?sda=N&scl=M tries exactly one pair, which is how a
  // find gets re-checked without waiting for all 110 again.
  http.on("/i2c", []() {
    if (http.hasArg("sda") && http.hasArg("scl")) {
      int sda = http.arg("sda").toInt(), scl = http.arg("scl").toInt();
      String r = i2c_try(sda, scl);
      http.send(200, "text/plain", r.length() ? r : "  SDA=" + String(sda) + " SCL=" + String(scl) +
                                                     ": no device answered\n");
      return;
    }
    http.send(200, "text/plain", i2c_sweep());
  });

  // /i2creg?addr=0x39&reg=0x92[&n=1][&sda=47&scl=48][&raw=1] -- raw skips the register write, for
  // parts that answer a bare read. Defaults to the bus /i2c found.
  http.on("/i2creg", []() {
    int sda = http.hasArg("sda") ? http.arg("sda").toInt() : 47;
    int scl = http.hasArg("scl") ? http.arg("scl").toInt() : 48;
    long addr = strtol(http.arg("addr").c_str(), nullptr, 0);
    long reg  = strtol(http.arg("reg").c_str(), nullptr, 0);
    int n = http.hasArg("n") ? http.arg("n").toInt() : 1;
    if (addr < 0x08 || addr > 0x77) { http.send(400, "text/plain", "addr out of range\n"); return; }
    http.send(200, "text/plain",
              i2c_regread(sda, scl, (uint8_t)addr, (uint8_t)reg, n, http.hasArg("raw")));
  });

  http.on("/pdmscan", []() {
    http.send(200, "text/plain", pdm_scan(http.hasArg("clk") ? http.arg("clk").toInt() : -1));
  });

  // /mic?clk=6&din=7[&fs=16000][&mono=1]
  http.on("/mic", []() {
    int clk = http.hasArg("clk") ? http.arg("clk").toInt() : 6;
    int din = http.hasArg("din") ? http.arg("din").toInt() : 7;
    int fs  = http.hasArg("fs")  ? http.arg("fs").toInt()  : 16000;
    http.send(200, "text/plain", mic_capture(clk, din, fs, !http.hasArg("mono")));
  });

  // /timesync -- POST because it WRITES the system clock and the RTC. Reports the bound it
  // achieved, not just that it succeeded.
  http.on("/timesync", HTTP_POST, []() {
    String host = http.hasArg("server") ? http.arg("server") : String(NTP_DEFAULT_SERVER);
    NtpResult r = ntp_query(host.c_str(), NTP_SAMPLES);
    if (!r.ok) { http.send(503, "text/plain", String("ntp ") + host + ": " + r.err + "\n"); return; }
    struct timeval tv; gettimeofday(&tv, nullptr);
    double now = tv.tv_sec + tv.tv_usec / 1e6 + r.offset_s;
    tv.tv_sec = (time_t)now; tv.tv_usec = (suseconds_t)((now - tv.tv_sec) * 1e6);
    settimeofday(&tv, nullptr);
    // ⚠️ROUND, DO NOT TRUNCATE. The DS3231 holds whole seconds, so (time_t)now discards the
    // fraction just computed and lands up to 1 s behind; nearest halves the worst case. The
    // /timesync path avoids this properly by waiting for the second boundary -- this path does
    // not wait, so rounding is the best available here. (Copilot review, PR #19.)
    bool rtc_ok = ds3231_write_time(47, 48, (time_t)llround(now));
    g_sync_bound_s = r.rtt_best / 2.0; g_sync_at_ms = millis();
    g_sync_off_s = r.offset_s; g_sync_count++;
    char b[520];
    snprintf(b, sizeof b,
      "synced to %s\n"
      "  server   stratum %d  refid %.4s  root distance %.3f ms\n"
      "  samples  %d of %d answered, rtt best %.3f ms, min %.3f, max %.3f (spread %.3f)\n"
      "  applied  offset %+.3f ms\n"
      "  BOUND    +/- %.3f ms  = +/- %.3f m of sound  <- rtt/2, the asymmetry bound\n"
      "  rtc      %s\n",
      host.c_str(), r.stratum, r.refid, r.root_dist_s * 1000,
      r.n, NTP_SAMPLES, r.rtt_best * 1000, r.rtt_min * 1000, r.rtt_max * 1000,
      (r.rtt_max - r.rtt_min) * 1000, r.offset_s * 1000,
      r.rtt_best / 2 * 1000, r.rtt_best / 2 * 343.0,
      rtc_ok ? "set from this sync, OSF cleared" : "WRITE FAILED -- holdover is not armed");
    http.send(200, "text/plain", b);
  });

  // /time -- read only. Says what the clock is AND how well it is known, because a timestamp with
  // no stated uncertainty is what put a phone into a TDoA solve at 8.6 m in the first place.
  http.on("/time", []() {
    struct timeval tv; gettimeofday(&tv, nullptr);
    time_t sysu = tv.tv_sec, rtcu = 0; bool osf = true;
    bool have_rtc = ds3231_read_time(47, 48, &rtcu, &osf);
    char sb[32], rb[32];
    struct tm t;
    gmtime_r(&sysu, &t); strftime(sb, sizeof sb, "%Y-%m-%dT%H:%M:%SZ", &t);
    gmtime_r(&rtcu, &t); strftime(rb, sizeof rb, "%Y-%m-%dT%H:%M:%SZ", &t);
    double age_s = g_sync_at_ms ? (millis() - g_sync_at_ms) / 1000.0 : -1;
    char b[700];
    snprintf(b, sizeof b,
      "system   %s  (%s)\n"
      "rtc      %s  %s\n"
      "delta    %+lld s  (rtc - system)\n"
      "sync     %s"
      "%s%s",
      sb, sysu > 1700000000 ? "set" : "NEVER SET -- this is uptime, not a time",
      have_rtc ? rb : "unreadable", osf ? "OSF=1 THE OSCILLATOR STOPPED, not a time" : "OSF=0 ran continuously",
      have_rtc ? (long long)(rtcu - sysu) : 0LL,
      g_sync_at_ms ? "" : "never. There is no NTP client running on a timer; /timesync is manual.\n",
      g_sync_at_ms ? "" : "Until then this node cannot state a timestamp uncertainty and must not\n",
      g_sync_at_ms ? "" : "be admitted as a TDoA arrival at any tier.\n");
    String o = b;
    if (g_sync_at_ms) {
      char c[420];
      snprintf(c, sizeof c,
        "#%d, %.0f s ago, offset applied %+.3f ms\n"
        "BOUND    +/- %.3f ms = +/- %.3f m at sync\n"
        "         the DS3231 is uncalibrated (aging 0) so holdover drift since then is NOT in this\n"
        "         number; two syncs are what measure it, and none of that makes this a PPS tier.\n",
        g_sync_count, age_s, g_sync_off_s * 1000, g_sync_bound_s * 1000, g_sync_bound_s * 343.0);
      o += c;
    }
    http.send(200, "text/plain", o);
  });

  // /sqw?hz=1|1024|4096|8192|off -- the DS3231's square-wave output.
  //
  // WHY THIS IS THE INTERESTING ENDPOINT. The L86's 1PPS is not routed and the module is not
  // answering, so the GPS route to a hardware tick is shut. But the DS3231 has its own SQW pin and
  // can drive 1 Hz from a TCXO-compensated oscillator (+/-2 ppm over temperature, against the bare
  // ESP crystal's tens of ppm). The board has been shipping with INTCN=1 -- interrupt mode, no
  // alarms enabled -- so that pin has been sitting idle since it left the factory and NOBODY HAS
  // EVER LOOKED to see whether it reaches a GPIO. Turn 1 Hz on, then run /scan: its verdict column
  // already flags a 1 Hz pulse, because it was written to look for the L86's.
  //
  // ⚠️A LOCAL TICK IS NOT A PPS. This edge is stable, not correct: it says a second has elapsed,
  // never which second it is, and it is disciplined by a crystal rather than by GPS. That is the
  // same shape as the ESP audioboards' anchor, where the board owns the rate and a host names the
  // second -- which is exactly why it is worth having next to an NTP that can name the second to
  // +/-5 ms but cannot subdivide it.
  http.on("/sqw", []() {
    String hz = http.hasArg("hz") ? http.arg("hz") : "1";
    uint8_t ctrl;
    if (!i2c_begin_default() || !i2c_rd(0x68, 0x0E, &ctrl)) {
      http.send(503, "text/plain", "no DS3231 at 0x68\n"); return; }
    uint8_t rs;
    if      (hz == "1")    rs = 0x00;
    else if (hz == "1024") rs = 0x08;
    else if (hz == "4096") rs = 0x10;
    else if (hz == "8192") rs = 0x18;
    else if (hz == "off")  rs = 0xFF;
    else { http.send(400, "text/plain", "hz must be 1, 1024, 4096, 8192 or off\n"); return; }
    uint8_t nv = (rs == 0xFF) ? (uint8_t)(ctrl | 0x04)              // INTCN=1, SQW off
                              : (uint8_t)((ctrl & ~0x1C) | rs);     // RS2/RS1 set, INTCN=0
    Wire.beginTransmission(0x68); Wire.write((uint8_t)0x0E); Wire.write(nv);
    bool ok = Wire.endTransmission() == 0;
    Wire.end(); pinMode(47, INPUT); pinMode(48, INPUT);
    char b[300];
    snprintf(b, sizeof b, "DS3231 control 0x%02X -> 0x%02X (%s)  %s\n"
             "Now run /scan and look at the verdict column. A 1 Hz pulse there means the SQW pin\n"
             "reaches a GPIO and this board has a hardware tick after all.\n",
             ctrl, nv, rs == 0xFF ? "SQW off" : (hz + " Hz").c_str(), ok ? "written" : "WRITE FAILED");
    http.send(200, "text/plain", b);
  });

  http.on("/rtc", []() {
    int sda = http.hasArg("sda") ? http.arg("sda").toInt() : 47;
    int scl = http.hasArg("scl") ? http.arg("scl").toInt() : 48;
    http.send(200, "text/plain", rtc_read(sda, scl));
  });

  http.on("/gpshold", HTTP_POST, []() {
    // FORCE_ON must be HELD logic high to leave backup mode -- Hardware Design 3.4.3: "FORCE_ON
    // logic high can turn off the switch (backup -> full on)". /gpsreset pulsed each candidate low
    // then high then RELEASED it to floating, which is the right shape for an active-low RESET and
    // the wrong shape for FORCE_ON. If one of the vendor's output pins is wired to FORCE_ON, a
    // pulse would never have woken it.
    //
    // So: drive each candidate HIGH and HOLD it while watching the module's TX for signs of life.
    // If one works, leave it asserted and say which -- that pin is then the wake line.
    static const int CAND[] = {2, 10, 21, 40, 41, 42};
    String o = "holding each vendor output HIGH in turn, watching GPIO44 for the L86 waking\n\n";
    int found = -1;
    for (int pin : CAND) {
      Serial1.end(); delay(10);
      pinMode(pin, OUTPUT);
      digitalWrite(pin, HIGH);
      delay(3000);                       // held, not pulsed
      pinMode(GPS_RX_PIN, INPUT);
      uint32_t ed = 0, n = 0, hi = 0; int last = digitalRead(GPS_RX_PIN);
      uint64_t t0 = esp_timer_get_time();
      while ((uint64_t)esp_timer_get_time() - t0 < 2000000ULL) {
        int v = digitalRead(GPS_RX_PIN); n++; if (v) hi++;
        if (v != last) { ed++; last = v; }
      }
      char b[130];
      snprintf(b, sizeof b, "GPIO%-3d held HIGH 3 s -> RX edges %-7lu high %5.1f%%  %s\n",
               pin, (unsigned long)ed, 100.0 * hi / n, ed > 100 ? "<== AWAKE" : "");
      o += b;
      if (ed > 100) { found = pin; break; }
      digitalWrite(pin, LOW); delay(50); pinMode(pin, INPUT);   // release before the next one
      Serial1.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
    }
    Serial1.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
    if (found >= 0) {
      char b[160];
      snprintf(b, sizeof b, "\nGPIO%d is the wake line and is LEFT ASSERTED HIGH.\n", found);
      o += b;
    } else {
      o += "\nNone of them. Either FORCE_ON is not on any of these pins, or this is not backup mode.\n";
    }
    http.send(200, "text/plain", o);
  });

  http.on("/gpsreset", HTTP_POST, []() {
    // I hung the L86 with PMTK285,4,500 and it will not answer anything, so the only way back is
    // to cycle its power or reset line. The vendor firmware configured GPIO2, 10, 21, 40, 41 and
    // 42 as OUTPUTS at boot and this firmware drives none of them -- so one of them plausibly
    // gates the GPS. Pulse each in turn and watch the module's TX line for it coming back.
    //
    // Bounded and reversible: each pin is driven for 200 ms and then returned to a high-impedance
    // input, so nothing is left asserted. They are pins the vendor firmware drives itself, which
    // is the only reason driving them is reasonable at all.
    static const int CAND[] = {2, 10, 21, 40, 41, 42};
    String o = "pulsing the vendor's output pins, watching GPIO44 for the L86 waking up\n\n";
    for (int pin : CAND) {
      Serial1.end(); delay(10);
      pinMode(pin, OUTPUT);
      digitalWrite(pin, LOW);  delay(200);      // assert (most resets/enables are active low)
      digitalWrite(pin, HIGH); delay(200);
      pinMode(pin, INPUT);                      // leave nothing asserted
      // give it time to boot and start talking, then look at the copper rather than the UART
      delay(1500);
      pinMode(GPS_RX_PIN, INPUT);
      uint32_t ed = 0, n = 0, hi = 0; int last = digitalRead(GPS_RX_PIN);
      uint64_t t0 = esp_timer_get_time();
      while ((uint64_t)esp_timer_get_time() - t0 < 1500000ULL) {
        int v = digitalRead(GPS_RX_PIN); n++; if (v) hi++;
        if (v != last) { ed++; last = v; }
      }
      char b[120];
      snprintf(b, sizeof b, "GPIO%-3d pulsed -> RX edges %-7lu high %5.1f%%  %s\n",
               pin, (unsigned long)ed, 100.0 * hi / n,
               ed > 100 ? "<== THE MODULE IS TALKING AGAIN" : "");
      o += b;
      Serial1.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
      if (ed > 100) { o += "\nstopping here -- that pin gates the GPS.\n"; break; }
    }
    http.send(200, "text/plain", o);
  });

  http.on("/uart", []() {
    // Is the L86 transmitting AT ALL? /gps reading nothing means the UART decoded nothing, which
    // is not the same as the line being quiet -- a wrong baud, a wrong pin or a dead module all
    // look identical through a UART. The whole-bank scan cannot answer it either, because 43/44
    // are excluded from it precisely BECAUSE they are the UART. So drop the driver and look at
    // the copper.
    Serial1.end();
    delay(20);
    String o = "";
    // Three pull modes on the module's TX. Plain INPUT cannot tell a line DRIVEN low from one
    // that is simply not driven at all -- both read 0. Against a pullup they differ: a driving
    // output holds it low, a high-impedance one (module in reset, unpowered, or tri-stated) is
    // pulled high. That distinction is the difference between "hung firmware" and "not running".
    for (int pin : {GPS_RX_PIN, GPS_RX_PIN, GPS_RX_PIN, GPS_TX_PIN}) {
      static int pass = 0;
      int mode = (pin == GPS_RX_PIN && pass < 3) ? (pass == 0 ? INPUT : pass == 1 ? INPUT_PULLUP : INPUT_PULLDOWN) : INPUT;
      const char *pmode = (pin == GPS_TX_PIN) ? "float" : (pass == 0 ? "float" : pass == 1 ? "PULLUP" : "plldn");
      if (pin == GPS_RX_PIN) pass++;
      pinMode(pin, mode);
      delay(30);
      uint32_t hi = 0, n = 0, ed = 0, mn = 0xFFFFFFFF;
      int last = digitalRead(pin);
      uint64_t t0 = esp_timer_get_time(), lastch = t0;
      while ((uint64_t)esp_timer_get_time() - t0 < 2000000ULL) {
        int v = digitalRead(pin); n++;
        if (v) hi++;
        if (v != last) {
          uint64_t now = esp_timer_get_time();
          uint32_t r = (uint32_t)(now - lastch);
          if (r && r < mn) mn = r;
          ed++; lastch = now; last = v;
        }
      }
      char b[200];
      snprintf(b, sizeof b, "GPIO%-3d %-3s %-6s edges %-7lu high %5.1f%%  shortest run %lu us  %s\n",
               pin, pin == GPS_RX_PIN ? "RX" : "TX", pmode, (unsigned long)ed, 100.0 * hi / n,
               (unsigned long)(mn == 0xFFFFFFFF ? 0 : mn),
               ed > 100 ? "<== TRANSMITTING" : ed ? "<== a few edges only" : "<== SILENT");
      o += b;
    }
    Serial1.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
    o += "\nRX float+pullup both LOW  -> the module is DRIVING it low.\n"
         "RX float LOW but pullup HIGH -> the module is NOT driving at all (reset/unpowered/tri-state).\n";
    http.send(200, "text/plain", o);
  });

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
    // Guard PMTK285 against the DOCUMENTED range, which is 2..998 ms (Protocol Spec V1.5 sec
    // 3.19, Type 0/1/2/3/4 and PPSPulseWidth 2~998). An earlier version of this guard clamped to
    // 10..400 and said in its own comment that 500 ms was out of range and had hung the module.
    // That was wrong: 500 is squarely inside the range the datasheet allows. The module did stop
    // talking on that command, but the reason I gave for it was invented, so the bound is now the
    // datasheet's and the comment no longer claims to know why.
    //
    // Still guarded, because a width approaching the 1000 ms period is not a pulse and the module
    // is currently in an unknown state -- but the number comes from the document now.
    if (c.startsWith("PMTK285")) {
      int comma = c.indexOf(',', 8);
      long w = comma > 0 ? c.substring(comma + 1).toInt() : -1;
      if (w < 2 || w > 998) {
        http.send(400, "text/plain",
          "PMTK285 PPSPulseWidth must be 2..998 ms (L86 Protocol Spec V1.5, section 3.19).\n"
          "Type is 0=disable 1=after first fix 2=3D only 3=2D/3D 4=always.\n\n"
          "100 ms is the module default and what /pps expects.\n");
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
             (unsigned long)hear_boot_try(), HEAR_BOOT_MAX_TRIES,
             hear_boot_proven() ? "yes" : "not yet", node_id);
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
  hear_boot_guard();
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
  hear_boot_tick(sta_ok);   // ⚠️the argument this node used to ignore
  static uint32_t last = 0;
  if (millis() - last > 30000) {
    last = millis();
    Serial.printf("[%6lus] fix %d/%d sats  nmea %lu/%lu valid  pps %lu\n",
                  (unsigned long)(millis() / 1000), gps_fix, gps_sats,
                  (unsigned long)gps_valid, (unsigned long)gps_sentences,
                  (unsigned long)pps_count);
  }
}
