// dama-hear node PoC on XIAO ESP32-S3.
//
// Answers the question docs/validation-full-captures.md leaves open in as many words:
// "Nothing about node CPU cost. The wall-clock figure here is Python on a Pi 5 and says nothing
// about an nRF52840." This measures it on real silicon, against vectors the Python produced.
//
// No microphone and no GPS are attached. Nothing here claims to be a working node -- it is the
// compute half, fed from recorded data, so the DSP is provable before a wire is cut.
#include "hear_core.h"

static int8_t  q[GOLD_BANDS * GOLD_FRAMES];
static uint8_t frame[256];

static void verify_sketch() {
  float ref;
  hear_sketch(GOLD_X + GOLD_DET_INDEX, GOLD_N - GOLD_DET_INDEX, q, &ref);
  int len = hear_pack(frame, (uint32_t)(GOLD_DET_INDEX % 1000000), ref,
                      (uint16_t)GOLD_DET_PEAK, q, 0);

  int hdr_ok = 1, body_exact = 0, worst = 0;
  for (int i = 0; i < 12; i++) if (frame[i] != GOLD_FRAME[i]) hdr_ok = 0;
  for (int i = 12; i < len; i++) {
    int d = (int)(int8_t)frame[i] - (int)(int8_t)GOLD_FRAME[i];
    if (d == 0) body_exact++;
    if (d < 0) d = -d;
    if (d > worst) worst = d;
  }
  int body = len - 12;
  Serial.printf("sketch    frame %d B (golden %d)  header %s\n", len, GOLD_FRAME_LEN,
                hdr_ok ? "byte-exact" : "MISMATCH");
  Serial.printf("          ref_db %.4f vs %.4f  (delta %.4f dB)\n", ref, GOLD_REF_DB,
                ref - GOLD_REF_DB);
  Serial.printf("          body %d/%d bytes exact (%.1f%%), worst |delta| %d step(s) = %.1f dB\n",
                body_exact, body, 100.0 * body_exact / body, worst, worst * 0.5);
}

static void bench_sketch() {
  float ref;
  const int N = 200;
  uint32_t t0 = micros();
  for (int i = 0; i < N; i++) hear_sketch(GOLD_X + GOLD_DET_INDEX, GOLD_N - GOLD_DET_INDEX, q, &ref);
  uint32_t dt = micros() - t0;
  float per = (float)dt / N;
  Serial.printf("sketch    %.0f us per detection  (%d frames x %d-pt FFT + %d mel bands)\n",
                per, GOLD_FRAMES, GOLD_NFFT, GOLD_BANDS);
  Serial.printf("          docs/uplink.md predicts ~500 us on a 64 MHz M4F with CMSIS-DSP\n");
}

static void bench_gate() {
  hear_gate_t g;
  const int REPS = 20;
  int32_t idx = -1, first = -1;
  int fired = 0;
  gate_init(&g, GOLD_FS);
  uint32_t t0 = micros();
  for (int r = 0; r < REPS; r++)
    for (int32_t n = 0; n < GOLD_N; n++)
      if (gate_push(&g, GOLD_X[n], n, &idx)) { if (r == 0) { if (first < 0) first = idx; } fired += (r == 0); }
  uint32_t dt = micros() - t0;
  double sps = (double)GOLD_N * REPS / (dt * 1e-6);
  Serial.printf("gate      %.2f M sample/s streaming\n", sps / 1e6);
  Serial.printf("          -> %.2f%% of one core at 16 kHz, %.2f%% at 48 kHz\n",
                100.0 * 16000.0 / sps, 100.0 * 48000.0 / sps);
  Serial.printf("          fired %d time(s) on the golden shot; first at %ld (reference %d, %+ld samples)\n",
                fired, (long)first, GOLD_DET_INDEX, (long)(first - GOLD_DET_INDEX));
  Serial.printf("          causal envelope here vs centred np.convolve in hear/ -- offset expected\n");
}

void setup() {
  Serial.begin(115200);
  delay(2500);
  Serial.println("\n=== dama-hear node PoC : XIAO ESP32-S3 ===");
  Serial.printf("cpu %lu MHz   free heap %lu B   psram %lu B\n",
                (unsigned long)getCpuFrequencyMhz(), (unsigned long)ESP.getFreeHeap(),
                (unsigned long)ESP.getPsramSize());
  Serial.printf("input     %d samples @ %.0f Hz (%.2f s), from tests/test_node.py::_shot\n\n",
                GOLD_N, GOLD_FS, GOLD_N / GOLD_FS);
  verify_sketch();
  Serial.println();
  bench_sketch();
  Serial.println();
  bench_gate();
  Serial.println("\n=== done ===");
}

void loop() { delay(1000); }
