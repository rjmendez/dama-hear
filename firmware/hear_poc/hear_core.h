// Node-side DSP in plain C: gate -> log-mel sketch -> 172 B frame.
//
// Checked against golden.h, which is emitted by the Python in hear/. If this file and hear/
// disagree, this file is wrong -- the Python is the reference and the field data was produced
// with it.
//
// Two deliberate departures from hear/, both stated so nobody has to guess:
//
//  1. The envelope here is CAUSAL. hear/node/detect.py:32 uses np.convolve(..., mode='same'),
//     which centres the window and therefore reads samples that have not arrived yet. A node
//     cannot do that. So the streaming gate's firing index is NOT expected to equal the
//     reference's, and the harness reports the difference rather than hiding it.
//  2. Single precision. hear/ is float64. The sketch comparison reports max |delta| in int8
//     steps rather than asserting byte equality, because claiming bit-exactness across a
//     precision change would be a claim about luck.
#pragma once
#include <math.h>
#include <stdint.h>
#include <string.h>

#include "golden.h"

// ---------------------------------------------------------------- FFT (radix-2, in-place)
// Twiddles are tabulated once. The first cut recomputed them with cosf/sinf inside the butterfly
// loop and cost 2660 us per sketch on this part -- so that measurement was of the transcendentals,
// not of the platform. ESP-DSP's dsps_fft2r_fc32 would be faster again and is the next step if
// this ever matters; it does not yet, because detections are rare.
static float fft_re[GOLD_NFFT], fft_im[GOLD_NFFT];
static float tw_re[GOLD_NFFT / 2], tw_im[GOLD_NFFT / 2];
static int   tw_ready = 0;

static void fft_init(void) {
  for (int k = 0; k < GOLD_NFFT / 2; k++) {
    float a = -2.0f * (float)M_PI * (float)k / (float)GOLD_NFFT;
    tw_re[k] = cosf(a); tw_im[k] = sinf(a);
  }
  tw_ready = 1;
}

static void fft256(void) {
  const int N = GOLD_NFFT;
  if (!tw_ready) fft_init();
  for (int i = 1, j = 0; i < N; i++) {                 // bit-reversal permutation
    int bit = N >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      float t = fft_re[i]; fft_re[i] = fft_re[j]; fft_re[j] = t;
      t = fft_im[i]; fft_im[i] = fft_im[j]; fft_im[j] = t;
    }
  }
  for (int len = 2; len <= N; len <<= 1) {
    int step = N / len;
    for (int i = 0; i < N; i += len) {
      for (int k = 0; k < len / 2; k++) {
        int a = i + k, b = a + len / 2;
        float cr = tw_re[k * step], ci = tw_im[k * step];
        float xr = fft_re[b] * cr - fft_im[b] * ci;
        float xi = fft_re[b] * ci + fft_im[b] * cr;
        fft_re[b] = fft_re[a] - xr; fft_im[b] = fft_im[a] - xi;
        fft_re[a] += xr;            fft_im[a] += xi;
      }
    }
  }
}

// ---------------------------------------------------------------- log-mel sketch
// q is [bands][frames] in the Python's row-major order; ref_db is the per-event reference that
// MUST travel with it (hear/sketch.py: amplitude alone measured AUC 0.90).
static void hear_sketch(const int16_t *x, int n, int8_t *q, float *ref_db) {
  static float db[GOLD_BANDS * GOLD_FRAMES];
  const int NB = GOLD_BANDS, NF = GOLD_FRAMES, NFFT = GOLD_NFFT;

  for (int t = 0; t < NF; t++) {
    int s = t * GOLD_HOP;
    for (int i = 0; i < NFFT; i++) {
      int k = s + i;
      float v = (k < n) ? (float)x[k] : 0.0f;
      fft_re[i] = v * GOLD_WIN[i];
      fft_im[i] = 0.0f;
    }
    fft256();
    const float *w = GOLD_FB_W;
    for (int b = 0; b < NB; b++) {
      int lo = GOLD_FB_LO[b], cnt = GOLD_FB_N[b];
      float acc = 0.0f;
      for (int i = 0; i < cnt; i++) {
        int bin = lo + i;
        float p = fft_re[bin] * fft_re[bin] + fft_im[bin] * fft_im[bin];
        acc += w[i] * p;
      }
      w += cnt;
      db[b * NF + t] = 10.0f * log10f(acc + 1e-12f);
    }
  }
  float ref = db[0];
  for (int i = 1; i < NB * NF; i++) if (db[i] > ref) ref = db[i];
  *ref_db = ref;
  for (int i = 0; i < NB * NF; i++) {
    float v = roundf((db[i] - ref) * 2.0f);
    if (v < -128.0f) v = -128.0f;
    if (v > 127.0f) v = 127.0f;
    q[i] = (int8_t)v;
  }
}

// ---------------------------------------------------------------- wire format (hear/sketch.py pack)
static int hear_pack(uint8_t *out, uint32_t node_us, float ref_db, uint16_t peak,
                     const int8_t *q, uint16_t flags) {
  int16_t ref4 = (int16_t)lrintf(ref_db * 4.0f);
  out[0] = node_us & 0xFF;       out[1] = (node_us >> 8) & 0xFF;
  out[2] = (node_us >> 16) & 0xFF; out[3] = (node_us >> 24) & 0xFF;
  out[4] = ref4 & 0xFF;          out[5] = (ref4 >> 8) & 0xFF;
  out[6] = peak & 0xFF;          out[7] = (peak >> 8) & 0xFF;
  out[8] = GOLD_BANDS;           out[9] = GOLD_FRAMES;
  out[10] = flags & 0xFF;        out[11] = (flags >> 8) & 0xFF;
  memcpy(out + 12, q, GOLD_BANDS * GOLD_FRAMES);
  return 12 + GOLD_BANDS * GOLD_FRAMES;
}

// ---------------------------------------------------------------- streaming gate (causal)
typedef struct {
  float ambient, alpha, ratio, floor_, rearm_frac, env_inv;
  int   guard, env_n, env_i, armed;
  float env_sum;
  float env_buf[64];
  int32_t last_idx;
} hear_gate_t;

static void gate_init(hear_gate_t *g, float fs) {
  memset(g, 0, sizeof(*g));
  g->ratio = 8.0f; g->floor_ = 800.0f; g->rearm_frac = 0.35f;
  g->guard = (int)(0.025f * fs);
  g->env_n = (int)(0.001f * fs); if (g->env_n < 1) g->env_n = 1;
  if (g->env_n > 64) g->env_n = 64;
  g->env_inv = 1.0f / (float)g->env_n;
  // Numerically matches hear/node/detect.py -- alpha is 1e-4 at 48 kHz either way -- but the
  // Python now derives it honestly as 1/(AMBIENT_TAU_S*fs) with AMBIENT_TAU_S documented as
  // 0.2083 s. This expression should follow.
  //
  // ⚠️AND THIS GATE IS NOW A THIRD BEHAVIOUR. gate_push returns the THRESHOLD-CROSSING index --
  // not the envelope peak the Python used to return, and not the constant-fraction onset it
  // returns now. A threshold crossing is the amplitude-dependent timestamp that constant-fraction
  // exists to remove, so this PoC no longer reproduces the reference it was written to check.
  // Porting the back-walk needs a longer env history than the 64 samples here: 1.3 ms at 48 kHz
  // against rise times measured out to 20 ms.
  g->alpha = 1.0f / (10.0f * fs / (float)g->env_n);
  g->armed = 1; g->last_idx = -1;
}

// Returns 1 and sets *idx when a detection fires at absolute sample index `n`.
static int gate_push(hear_gate_t *g, int16_t s, int32_t n, int32_t *idx) {
  float a = (float)(s < 0 ? -s : s);
  g->env_sum += a - g->env_buf[g->env_i];
  g->env_buf[g->env_i] = a;
  if (++g->env_i >= g->env_n) g->env_i = 0;   // was `% env_n` -- a divide in the hot loop
  float e = g->env_sum * g->env_inv;          // was `/ env_n` -- likewise

  float thr = g->ambient * g->ratio;
  if (thr < g->floor_) thr = g->floor_;

  if (!g->armed) {
    if (e < thr * g->rearm_frac) g->armed = 1;
    return 0;
  }
  if (e <= thr) {
    g->ambient = (1.0f - g->alpha) * g->ambient + g->alpha * e;
    return 0;
  }
  *idx = n;
  g->last_idx = n;
  g->armed = 0;
  return 1;
}
