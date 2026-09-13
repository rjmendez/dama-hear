// Clip names and the order clips are evicted in. Pure C so tests/test_clip_eviction.py can compile
// and run it on the host; hear/clips.py:eviction_key mirrors it for the drain's fetch order.
//
// A clip this firmware writes is <node>-<boot>-<sample>.wav, where <boot> is CLIP_SEQ_HEX hex of
// sequence followed by CLIP_RAND_HEX hex of esp_random(), and <sample> is %010lu. The sequence is
// one past the highest on the card at boot and advances again whenever the sample counter wraps,
// so (sequence, sample) is chronological. Any other name on the card was written by an older
// firmware and is older than every name of this shape.
#pragma once
#include <stdint.h>
#include <string.h>

#define CLIP_SEQ_HEX       6
#define CLIP_RAND_HEX      6
#define CLIP_BOOT_HEX      (CLIP_SEQ_HEX + CLIP_RAND_HEX)
#define CLIP_SAMPLE_DIGITS 10
#define CLIP_SEQ_MASK      0xFFFFFFu

typedef struct { uint8_t legacy; uint32_t seq; uint32_t sample; } clip_key_t;

static inline int clip_hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// 1 when nm is a basename of this firmware's shape; otherwise 0 and k->legacy = 1.
static inline int clip_parse(const char *nm, clip_key_t *k) {
  const size_t tail = 1 + CLIP_BOOT_HEX + 1 + CLIP_SAMPLE_DIGITS + 4;
  size_t n = strlen(nm);
  k->legacy = 1; k->seq = 0; k->sample = 0;
  if (n <= tail) return 0;
  const char *t = nm + n - tail;
  if (t[0] != '-' || t[1 + CLIP_BOOT_HEX] != '-' || strcmp(t + tail - 4, ".wav") != 0) return 0;
  uint32_t seq = 0;
  for (int i = 0; i < CLIP_BOOT_HEX; i++) {
    int v = clip_hexval(t[1 + i]);
    if (v < 0) return 0;
    if (i < CLIP_SEQ_HEX) seq = seq * 16u + (uint32_t)v;
  }
  uint64_t s = 0;
  for (int i = 0; i < CLIP_SAMPLE_DIGITS; i++) {
    char c = t[2 + CLIP_BOOT_HEX + i];
    if (c < '0' || c > '9') return 0;
    s = s * 10u + (uint64_t)(c - '0');
  }
  if (s > 0xFFFFFFFFull) return 0;
  k->legacy = 0; k->seq = seq; k->sample = (uint32_t)s;
  return 1;
}

// Negative when a is older than b, i.e. evicted first.
static inline int clip_cmp(const char *an, const clip_key_t *a, const char *bn, const clip_key_t *b) {
  if (a->legacy != b->legacy) return a->legacy ? -1 : 1;
  if (!a->legacy) {
    if (a->seq != b->seq) return a->seq < b->seq ? -1 : 1;
    if (a->sample != b->sample) return a->sample < b->sample ? -1 : 1;
  }
  return strcmp(an, bn);
}
