#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  MIC_DIAG_CAPTURE_FAILURE = 0,
  MIC_DIAG_STUCK = 1,
  MIC_DIAG_FLOATING = 2,
  MIC_DIAG_QUIET = 3,
  MIC_DIAG_SATURATED = 4,
  MIC_DIAG_NORMAL = 5,
} mic_diag_state_t;

typedef struct {
  mic_diag_state_t state;
  const char *state_name;
  const char *legacy;
  const char *reason;
  uint32_t samples;
  int16_t lo;
  int16_t hi;
  int32_t mean;
  uint32_t span;
  uint32_t mean_abs;
  uint32_t zero_cross_pct;
  uint32_t same_adj_pct;
  uint32_t unique;
  uint32_t sat_pct;
  uint32_t rail_hits;
} mic_diag_t;

#define MIC_DIAG_INIT \
  { MIC_DIAG_CAPTURE_FAILURE, "capture-failure", "silent", "not_run", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0 }

#define MIC_DIAG_QUIET_SPAN_MAX 8u
#define MIC_DIAG_QUIET_MEAN_ABS_MAX 2u
#define MIC_DIAG_STUCK_SPAN_MAX 4u
#define MIC_DIAG_STUCK_SAME_ADJ_PCT_MIN 98u
#define MIC_DIAG_FLOATING_SPAN_MAX 64u
#define MIC_DIAG_FLOATING_MEAN_ABS_MAX 12u
#define MIC_DIAG_FLOATING_ZERO_CROSS_PCT_MIN 20u
#define MIC_DIAG_FLOATING_UNIQUE_MIN 12u
#define MIC_DIAG_FLOATING_TOGGLE_ZERO_CROSS_PCT_MIN 80u
#define MIC_DIAG_FLOATING_TOGGLE_SPAN_MAX 8u
#define MIC_DIAG_SAT_PCT_MIN 90u

static inline const char *mic_diag_state_name(mic_diag_state_t state) {
  switch (state) {
    case MIC_DIAG_CAPTURE_FAILURE: return "capture-failure";
    case MIC_DIAG_STUCK:           return "stuck";
    case MIC_DIAG_FLOATING:        return "floating";
    case MIC_DIAG_QUIET:           return "quiet";
    case MIC_DIAG_SATURATED:       return "saturated";
    case MIC_DIAG_NORMAL:          return "normal";
  }
  return "capture-failure";
}

static inline const char *mic_diag_legacy_state(mic_diag_state_t state) {
  if (state == MIC_DIAG_SATURATED) return "saturated";
  return (state == MIC_DIAG_QUIET || state == MIC_DIAG_NORMAL) ? "ok" : "silent";
}

static inline void mic_diag_set(mic_diag_t *diag, mic_diag_state_t state, const char *reason) {
  if (!diag) return;
  diag->state = state;
  diag->state_name = mic_diag_state_name(state);
  diag->legacy = mic_diag_legacy_state(state);
  diag->reason = reason ? reason : "unspecified";
}

static inline mic_diag_t mic_diag_missing(const char *reason) {
  mic_diag_t diag = MIC_DIAG_INIT;
  mic_diag_set(&diag, MIC_DIAG_CAPTURE_FAILURE, reason ? reason : "no_samples");
  return diag;
}

static inline mic_diag_t mic_diag_classify(const int16_t *samples, size_t n) {
  mic_diag_t diag = MIC_DIAG_INIT;
  mic_diag_set(&diag, MIC_DIAG_CAPTURE_FAILURE, "no_samples");
  if (!samples || n == 0) return diag;

  diag.samples = (uint32_t)n;
  diag.lo = samples[0];
  diag.hi = samples[0];

  int64_t sum = 0;
  uint32_t rail_hits = 0;
  uint32_t same_adj = 0;
  uint32_t zero_cross = 0;
  int last_sign = 0;

  for (size_t i = 0; i < n; i++) {
    int16_t v = samples[i];
    if (v < diag.lo) diag.lo = v;
    if (v > diag.hi) diag.hi = v;
    if (v <= -32000 || v >= 32000) rail_hits++;
    sum += (int64_t)v;
    if (i) {
      if (v == samples[i - 1]) same_adj++;
      int sign = (v > 0) - (v < 0);
      if (sign) {
        if (last_sign && sign != last_sign) zero_cross++;
        last_sign = sign;
      }
    } else {
      last_sign = (v > 0) - (v < 0);
    }
  }

  diag.mean = (int32_t)(sum / (int64_t)n);
  diag.span = (uint32_t)((int32_t)diag.hi - (int32_t)diag.lo);

  int64_t abs_sum = 0;
  for (size_t i = 0; i < n; i++) {
    abs_sum += llabs((long long)samples[i] - (long long)diag.mean);
  }
  diag.mean_abs = (uint32_t)(abs_sum / (int64_t)n);
  diag.same_adj_pct = n > 1 ? (uint32_t)((same_adj * 100u) / (uint32_t)(n - 1)) : 100u;
  diag.zero_cross_pct = n > 1 ? (uint32_t)((zero_cross * 100u) / (uint32_t)(n - 1)) : 0u;
  diag.rail_hits = rail_hits;
  diag.sat_pct = (uint32_t)((rail_hits * 100u) / (uint32_t)n);

  uint32_t unique = 0;
  for (size_t i = 0; i < n; i++) {
    int seen = 0;
    for (size_t j = 0; j < i; j++) {
      if (samples[j] == samples[i]) {
        seen = 1;
        break;
      }
    }
    if (!seen) unique++;
  }
  diag.unique = unique;

  if (diag.sat_pct >= MIC_DIAG_SAT_PCT_MIN) {
    mic_diag_set(&diag, MIC_DIAG_SATURATED, "rail_hits");
    return diag;
  }
  if (diag.unique == 1 && diag.lo == 0 && diag.hi == 0) {
    mic_diag_set(&diag, MIC_DIAG_CAPTURE_FAILURE, "all_zero_samples");
    return diag;
  }
  if (diag.unique == 1) {
    mic_diag_set(&diag, MIC_DIAG_STUCK, "constant_sample");
    return diag;
  }
  if (diag.unique <= 2 &&
      diag.zero_cross_pct >= MIC_DIAG_FLOATING_TOGGLE_ZERO_CROSS_PCT_MIN &&
      diag.span <= MIC_DIAG_FLOATING_TOGGLE_SPAN_MAX) {
    mic_diag_set(&diag, MIC_DIAG_FLOATING, "two_level_toggle");
    return diag;
  }
  if (diag.unique <= 2 &&
      diag.same_adj_pct >= MIC_DIAG_STUCK_SAME_ADJ_PCT_MIN &&
      diag.span <= MIC_DIAG_STUCK_SPAN_MAX) {
    mic_diag_set(&diag, MIC_DIAG_STUCK, "repeating_sample");
    return diag;
  }
  if (diag.span <= MIC_DIAG_QUIET_SPAN_MAX && diag.mean_abs <= MIC_DIAG_QUIET_MEAN_ABS_MAX) {
    mic_diag_set(&diag, MIC_DIAG_QUIET, "low_variation");
    return diag;
  }
  if (diag.span <= MIC_DIAG_FLOATING_SPAN_MAX &&
      diag.mean_abs <= MIC_DIAG_FLOATING_MEAN_ABS_MAX &&
      diag.zero_cross_pct >= MIC_DIAG_FLOATING_ZERO_CROSS_PCT_MIN &&
      diag.unique >= MIC_DIAG_FLOATING_UNIQUE_MIN) {
    mic_diag_set(&diag, MIC_DIAG_FLOATING, "narrow_noisy_span");
    return diag;
  }

  mic_diag_set(&diag, MIC_DIAG_NORMAL, "signal_variation_present");
  return diag;
}

#ifdef __cplusplus
}
#endif
