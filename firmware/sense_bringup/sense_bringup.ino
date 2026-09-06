// XIAO ESP32-S3 Sense bring-up. Proves the four subsystems a mains-powered recorder node needs,
// and says which ones actually work rather than assuming.
//
//   arduino-cli compile -u -p /dev/ttyACM0 \
//     --fqbn esp32:esp32:XIAO_ESP32S3:PSRAM=opi firmware/sense_bringup
//
// PSRAM=opi matters: the default board option leaves PSRAM DISABLED and the camera then fails to
// allocate at anything above QVGA.
#include "esp_camera.h"
#include <SD.h>
#include <SPI.h>
#include <ESP_I2S.h>
#include <WiFi.h>

#define CAMERA_MODEL_XIAO_ESP32S3
#include "camera_pins.h"

// Sense expansion board
#define PDM_CLK 42
#define PDM_DIN 41
#define SD_SCK   7
#define SD_MISO  8
#define SD_MOSI  9
static const int SD_CS_CANDIDATES[] = {21, 3};   // docs disagree; ask the hardware

static int pass = 0, fail = 0;
static void ok(const char *what, const char *detail) {
  Serial.printf("  [ OK ] %-10s %s\n", what, detail); pass++;
}
static void bad(const char *what, const char *detail) {
  Serial.printf("  [FAIL] %-10s %s\n", what, detail); fail++;
}

static void t_psram() {
  size_t n = ESP.getPsramSize();
  char b[96];
  snprintf(b, sizeof b, "%u B (%.1f MB) free %u", (unsigned)n, n / 1048576.0,
           (unsigned)ESP.getFreePsram());
  n ? ok("psram", b) : bad("psram", "0 B -- rebuild with PSRAM=opi");
}

static void t_camera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0; c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM; c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000; c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_UXGA; c.jpeg_quality = 12; c.fb_count = 2;
  c.fb_location = CAMERA_FB_IN_PSRAM; c.grab_mode = CAMERA_GRAB_LATEST;

  esp_err_t e = esp_camera_init(&c);
  if (e != ESP_OK) { char b[64]; snprintf(b, sizeof b, "init failed 0x%x", e); bad("camera", b); return; }

  sensor_t *s = esp_camera_sensor_get();
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) { bad("camera", "init ok but no frame captured"); return; }
  size_t first = fb->len; uint32_t w = fb->width, h = fb->height;
  esp_camera_fb_return(fb);

  // one frame can be luck; time ten and report the rate a recorder would actually get
  uint32_t t0 = millis(); int got = 0;
  for (int i = 0; i < 10; i++) {
    camera_fb_t *f = esp_camera_fb_get();
    if (f) { got++; esp_camera_fb_return(f); }
  }
  uint32_t dt = millis() - t0;
  char b[128];
  snprintf(b, sizeof b, "PID 0x%04x  %ux%u JPEG %u B  %d/10 frames, %.1f fps",
           s ? s->id.PID : 0, (unsigned)w, (unsigned)h, (unsigned)first, got, got * 1000.0 / dt);
  got == 10 ? ok("camera", b) : bad("camera", b);
}

static void t_sd() {
  SPI.begin(SD_SCK, SD_MISO, SD_MOSI);
  for (unsigned i = 0; i < sizeof(SD_CS_CANDIDATES) / sizeof(int); i++) {
    int cs = SD_CS_CANDIDATES[i];
    if (!SD.begin(cs, SPI, 20000000)) continue;
    uint64_t sz = SD.cardSize();
    File f = SD.open("/dama_hear_bringup.txt", FILE_WRITE);
    if (!f) { SD.end(); continue; }
    f.print("dama-hear bring-up"); f.close();
    f = SD.open("/dama_hear_bringup.txt");
    String back = f ? f.readString() : String();
    if (f) f.close();
    SD.remove("/dama_hear_bringup.txt");
    char b[128];
    snprintf(b, sizeof b, "CS=%d  %llu MB  write+readback %s", cs, sz / 1048576ULL,
             back == "dama-hear bring-up" ? "verified" : "MISMATCH");
    back == "dama-hear bring-up" ? ok("sd", b) : bad("sd", b);
    return;
  }
  bad("sd", "no card mounted on CS 21 or 3 -- card inserted?");
}

static void t_mic() {
  I2SClass i2s;
  i2s.setPinsPdmRx(PDM_CLK, PDM_DIN);
  if (!i2s.begin(I2S_MODE_PDM_RX, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    bad("pdm mic", "i2s begin failed"); return;
  }
  const int N = 4096;
  static int16_t buf[N];
  size_t got = i2s.readBytes((char *)buf, sizeof buf);
  int n = got / 2;
  if (n < 64) { bad("pdm mic", "no samples"); i2s.end(); return; }
  double sum = 0; int32_t pk = 0; int nz = 0;
  for (int i = 0; i < n; i++) {
    int32_t a = buf[i] < 0 ? -buf[i] : buf[i];
    sum += (double)buf[i] * buf[i];
    if (a > pk) pk = a;
    if (buf[i] != 0) nz++;
  }
  double rms = sqrt(sum / n);
  char b[128];
  snprintf(b, sizeof b, "%d samples @16k  rms %.0f  peak %ld  nonzero %d%%",
           n, rms, (long)pk, 100 * nz / n);
  (pk > 0 && nz > n / 10) ? ok("pdm mic", b) : bad("pdm mic", b);
  i2s.end();
}

static void t_wifi() {
  WiFi.mode(WIFI_STA); WiFi.disconnect();
  int n = WiFi.scanNetworks();
  if (n <= 0) { bad("wifi", "scan found 0 networks"); return; }
  int best = -127; for (int i = 0; i < n; i++) if (WiFi.RSSI(i) > best) best = WiFi.RSSI(i);
  char b[96];
  snprintf(b, sizeof b, "radio up, %d networks, strongest %d dBm (not joined -- no creds)", n, best);
  ok("wifi", b);
}

void setup() {
  Serial.begin(115200);
  delay(2500);
  Serial.println("\n=== XIAO ESP32-S3 Sense bring-up ===");
  Serial.printf("cpu %lu MHz  flash %lu MB  heap %lu B\n\n",
                (unsigned long)getCpuFrequencyMhz(), (unsigned long)(ESP.getFlashChipSize() / 1048576),
                (unsigned long)ESP.getFreeHeap());
  t_psram();
  t_camera();
  t_sd();
  t_mic();
  t_wifi();
  Serial.printf("\n%d passed, %d failed\n=== done ===\n", pass, fail);
}

void loop() { delay(1000); }
