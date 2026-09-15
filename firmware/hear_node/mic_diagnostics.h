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
  // How the verdict was reached, not what it is: `attempts` counts probe reads and `settle_ms` is
  // the milliseconds from the first read to the accepted one. A healthy mic on a warm restart
  // reports 1 / 0; a cold ICS-43434 that needed the settle window reports >1 and the time it took.
  uint32_t attempts;
  uint32_t settle_ms;
} mic_diag_t;

#define MIC_DIAG_INIT \
  { MIC_DIAG_CAPTURE_FAILURE, "capture-failure", "silent", "not_run", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0 }

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

// ---------------------------------------------------------------- bounded probe settle
// ⚠️ONE READ TAKEN THE INSTANT i2s.begin() RETURNS IS NOT A MEASUREMENT OF THE MICROPHONE.
// An ICS-43434-class part needs its own power-up plus a number of SCK cycles before it drives the
// data line, and the ESP32's DMA descriptors are handed out already zeroed, so the first 16 ms
// block off a perfectly good mic can be all zeros or a repeating value. That single latched read
// is what reported `capture-failure` on ageev and 98% repeated samples on kasami while both nodes
// were serving healthy 48 kHz audio and detections for the rest of the boot.
//
// The fix is a BOUNDED retry, not a blind delay: read, classify, accept the first healthy verdict,
// and only pay the settle window when the microphone is still not answering. That keeps a warm OTA
// restart (mic still powered, answers on the first read) at zero added boot time, while a cold
// power-on gets the milliseconds it actually needs. It cannot mask an absent, stuck or floating
// microphone either, because the LAST classification is what is reported -- a mic that never wakes
// is classified exactly as it was before, after the window, with `attempts` and `settle_ms` saying
// how hard the node tried.
#define MIC_PROBE_SETTLE_COLD_MS 750u   // cold power-on / brownout: part is coming up from unpowered
#define MIC_PROBE_SETTLE_WARM_MS 250u   // sw / OTA / panic restart: the mic kept its supply
#define MIC_PROBE_RETRY_GAP_MS   20u    // ~one I2S DMA block at FS_ACQ; long enough to be new data
#define MIC_PROBE_MAX_ATTEMPTS   16u

typedef int (*mic_probe_read_fn)(void *ctx, int16_t *dst, size_t max_samples);
typedef uint32_t (*mic_probe_now_ms_fn)(void *ctx);
typedef void (*mic_probe_wait_ms_fn)(void *ctx, uint32_t ms);

typedef struct {
  mic_probe_read_fn read;        // required; returns samples read, <= 0 means nothing came back
  mic_probe_now_ms_fn now_ms;    // optional; without it the probe is a single read, as before
  mic_probe_wait_ms_fn wait_ms;  // optional; services the boot watchdog on the firmware side
  void *ctx;
  int16_t *buf;
  size_t buf_samples;
  uint32_t settle_budget_ms;     // 0 -> single read
  uint32_t retry_gap_ms;         // 0 -> MIC_PROBE_RETRY_GAP_MS
  uint32_t max_attempts;         // 0 -> MIC_PROBE_MAX_ATTEMPTS
} mic_probe_cfg_t;

// The two states a live microphone can legitimately be in at boot. `saturated`, `stuck`,
// `floating` and `capture-failure` are all retried: each of them is a shape a not-yet-awake part
// produces, and a genuinely broken one keeps producing it until the window closes.
static inline int mic_diag_state_is_healthy(mic_diag_state_t state) {
  return state == MIC_DIAG_QUIET || state == MIC_DIAG_NORMAL;
}

static inline uint32_t mic_probe_budget_ms(int cold_boot) {
  return cold_boot ? MIC_PROBE_SETTLE_COLD_MS : MIC_PROBE_SETTLE_WARM_MS;
}

static inline mic_diag_t mic_probe_settle(const mic_probe_cfg_t *cfg) {
  mic_diag_t diag = mic_diag_missing("no_probe");
  if (!cfg || !cfg->read || !cfg->buf || cfg->buf_samples == 0) return diag;

  const uint32_t gap = cfg->retry_gap_ms ? cfg->retry_gap_ms : MIC_PROBE_RETRY_GAP_MS;
  const uint32_t max_attempts = cfg->max_attempts ? cfg->max_attempts : MIC_PROBE_MAX_ATTEMPTS;
  const uint32_t t0 = cfg->now_ms ? cfg->now_ms(cfg->ctx) : 0u;
  uint32_t attempts = 0;

  for (;;) {
    int n = cfg->read(cfg->ctx, cfg->buf, cfg->buf_samples);
    attempts++;
    diag = (n > 0) ? mic_diag_classify(cfg->buf, (size_t)n) : mic_diag_missing("no_samples");
    uint32_t elapsed = cfg->now_ms ? (uint32_t)(cfg->now_ms(cfg->ctx) - t0) : 0u;
    diag.attempts = attempts;
    diag.settle_ms = elapsed;

    if (mic_diag_state_is_healthy(diag.state)) return diag;
    if (!cfg->now_ms) return diag;
    if (attempts >= max_attempts) return diag;
    if (elapsed + gap >= cfg->settle_budget_ms) return diag;
    if (cfg->wait_ms) cfg->wait_ms(cfg->ctx, gap);
  }
}

#ifdef __cplusplus
}
#endif
