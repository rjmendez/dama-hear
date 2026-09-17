#pragma once

// hear.ingest.clip.v1 -- the LOGIC the no-SD push path is built from, shared by hear_node.ino
// and tests/test_firmware_clip_push.py.
//
// clip_upload.h is declarations only: wire constants, no algorithm. This file is the algorithm,
// and it is deliberately self-contained rather than `#include <mbedtls/sha256.h>`, for one
// reason: this repo's host tests compile firmware headers with a bare `cc` (see
// tests/test_firmware_spool_push.py), and this development host has no mbedtls dev headers
// installed and no privilege to install them. A SHA-256 the test harness cannot compile is a
// SHA-256 nobody has actually run before it reaches a node, so the hash lives here, in plain C,
// with no dependency past <stdint.h>. tests/test_firmware_clip_push.py checks it against the
// FIPS 180-4 test vectors AND against hear.ingest.clipupload.upload_id, so the firmware's
// upload_id and the Python receiver's upload_id are proven equal, not just believed to be.
//
// Everything below is pure: no socket, no SD, no ring, no PSRAM, no allocation past a caller-
// supplied buffer. hear_node.ino is the only place that reads the ring, opens a client, or holds
// a multi-chunk session; this header only ever sees the bytes it is handed.

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "clip_upload.h"

// ---------------------------------------------------------------- SHA-256 (FIPS 180-4)
// Textbook, unaccelerated, one block at a time. A clip is 480 044 B = ~7 500 blocks; measured
// cost is not the concern here (see hear_node.ino, where the chunk hash is folded into the same
// pass that already copies the chunk out of the ring) -- correctness against the two received
// digests (per-chunk X-Hear-Chunk-SHA256 and the whole-clip `sha256` at complete) is.

typedef struct {
  uint32_t h[8];
  uint64_t bitlen;
  uint8_t  buf[64];
  uint32_t buf_len;
} hear_sha256_ctx_t;

static const uint32_t HEAR_SHA256_K[64] = {
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

#define HEAR_SHA256_ROTR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static inline void hear_sha256_transform(hear_sha256_ctx_t *c, const uint8_t *block) {
  uint32_t w[64];
  for (int i = 0; i < 16; i++) {
    w[i] = ((uint32_t)block[i * 4] << 24) | ((uint32_t)block[i * 4 + 1] << 16) |
           ((uint32_t)block[i * 4 + 2] << 8) | (uint32_t)block[i * 4 + 3];
  }
  for (int i = 16; i < 64; i++) {
    uint32_t s0 = HEAR_SHA256_ROTR(w[i - 15], 7) ^ HEAR_SHA256_ROTR(w[i - 15], 18) ^ (w[i - 15] >> 3);
    uint32_t s1 = HEAR_SHA256_ROTR(w[i - 2], 17) ^ HEAR_SHA256_ROTR(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3];
  uint32_t e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
  for (int i = 0; i < 64; i++) {
    uint32_t S1 = HEAR_SHA256_ROTR(e, 6) ^ HEAR_SHA256_ROTR(e, 11) ^ HEAR_SHA256_ROTR(e, 25);
    uint32_t ch = (e & f) ^ (~e & g);
    uint32_t t1 = h + S1 + ch + HEAR_SHA256_K[i] + w[i];
    uint32_t S0 = HEAR_SHA256_ROTR(a, 2) ^ HEAR_SHA256_ROTR(a, 13) ^ HEAR_SHA256_ROTR(a, 22);
    uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
    uint32_t t2 = S0 + maj;
    h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
  }
  c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
  c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static inline void hear_sha256_init(hear_sha256_ctx_t *c) {
  static const uint32_t iv[8] = {
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
  };
  memcpy(c->h, iv, sizeof iv);
  c->bitlen = 0;
  c->buf_len = 0;
}

static inline void hear_sha256_update(hear_sha256_ctx_t *c, const uint8_t *data, size_t len) {
  c->bitlen += (uint64_t)len * 8u;
  while (len) {
    size_t take = 64 - c->buf_len;
    if (take > len) take = len;
    memcpy(c->buf + c->buf_len, data, take);
    c->buf_len += (uint32_t)take;
    data += take; len -= take;
    if (c->buf_len == 64) { hear_sha256_transform(c, c->buf); c->buf_len = 0; }
  }
}

static inline void hear_sha256_final(hear_sha256_ctx_t *c, uint8_t out[32]) {
  uint64_t bitlen = c->bitlen;
  uint8_t pad = 0x80;
  hear_sha256_update(c, &pad, 1);
  uint8_t zero = 0;
  while (c->buf_len != 56) hear_sha256_update(c, &zero, 1);
  for (int i = 0; i < 8; i++) c->buf[56 + i] = (uint8_t)(bitlen >> (56 - 8 * i));
  hear_sha256_transform(c, c->buf);   // length appended directly: hear_sha256_update must not
                                      // re-count these 8 bytes into bitlen
  for (int i = 0; i < 8; i++) {
    out[i * 4]     = (uint8_t)(c->h[i] >> 24);
    out[i * 4 + 1] = (uint8_t)(c->h[i] >> 16);
    out[i * 4 + 2] = (uint8_t)(c->h[i] >> 8);
    out[i * 4 + 3] = (uint8_t)(c->h[i]);
  }
}

// Lowercase hex, always 65 bytes (64 hex + NUL) -- the shape every digest in the contract wants.
static inline void hear_sha256_hex(const uint8_t digest[32], char out[65]) {
  static const char hexd[] = "0123456789abcdef";
  for (int i = 0; i < 32; i++) {
    out[i * 2]     = hexd[digest[i] >> 4];
    out[i * 2 + 1] = hexd[digest[i] & 0xF];
  }
  out[64] = 0;
}

// One-shot convenience for a chunk-sized buffer already in hand (the per-chunk digest never
// streams: the whole chunk is staged before it is sent, see hear_node.ino's clip_push_stage).
static inline void hear_sha256_hex_of(const uint8_t *data, size_t len, char out[65]) {
  hear_sha256_ctx_t c; hear_sha256_init(&c);
  hear_sha256_update(&c, data, len);
  uint8_t d[32]; hear_sha256_final(&c, d);
  hear_sha256_hex(d, out);
}

// ---------------------------------------------------------------- identity: clip_key/upload_id
// Byte-identical to hear.clips.clip_key(node, boot, sample) and hear.ingest.clipupload.upload_id,
// which tests/test_firmware_clip_push.py asserts directly against the real Python module -- this
// is the one piece of arithmetic that MUST agree with a receiver this firmware cannot import.
static inline void hear_clip_upload_id(const char *node, const char *boot, uint32_t sample,
                                       char out[HEAR_CLIP_UPLOAD_ID_MAX]) {
  hear_sha256_ctx_t c; hear_sha256_init(&c);
  char sample_dec[16];
  snprintf(sample_dec, sizeof sample_dec, "%lu", (unsigned long)sample);
  const char *parts[4] = {"clip", node, boot, sample_dec};
  static const uint8_t unit_sep = 0x1f;
  for (int i = 0; i < 4; i++) {
    hear_sha256_update(&c, (const uint8_t *)parts[i], strlen(parts[i]));
    hear_sha256_update(&c, &unit_sep, 1);
  }
  uint8_t digest[32]; hear_sha256_final(&c, digest);
  char hex[65]; hear_sha256_hex(digest, hex);
  snprintf(out, HEAR_CLIP_UPLOAD_ID_MAX, "%.32s", hex);   // clip_key truncates to 32 hex chars
}

// device_id.clipinit.<upload_id> / device_id.clipdone.<upload_id>.<sha256[:16]>, mirroring
// hear.ingest.clipupload.init_idempotency_key / complete_idempotency_key exactly.
static inline int hear_clip_init_idempotency_key(const char *device_id, const char *upload_id,
                                                 char *out, size_t cap) {
  int n = snprintf(out, cap, "%s.clipinit.%s", device_id, upload_id);
  return (n > 0 && (size_t)n < cap) ? n : -1;
}

static inline int hear_clip_complete_idempotency_key(const char *device_id, const char *upload_id,
                                                     const char *sha256_hex, char *out, size_t cap) {
  int n = snprintf(out, cap, "%s.clipdone.%s.%.16s", device_id, upload_id, sha256_hex);
  return (n > 0 && (size_t)n < cap) ? n : -1;
}

// ---------------------------------------------------------------- init/complete JSON frames
// Bounded, single snprintf, no allocation. Field order matches docs/phase4-push-clip-upload.md's
// example so a byte capture of the wire is easy to compare against the doc by eye; the server
// parses a JSON object and does not require a field order, so this is a readability choice, not
// a protocol one.
static inline int hear_clip_init_body(const char *device_id, const char *node, const char *boot,
                                      uint32_t sample, const char *clip_basename,
                                      uint32_t clip_bytes, uint32_t chunk_bytes,
                                      const char *upload_source, const char *upload_id,
                                      char *out, size_t cap) {
  int n = snprintf(out, cap,
      "{\"upload_schema_version\":%d,\"device_id\":\"%s\",\"node\":\"%s\",\"boot\":\"%s\","
      "\"sample\":%lu,\"clip_basename\":\"%s\",\"clip_bytes\":%lu,\"chunk_bytes\":%lu,"
      "\"upload_source\":\"%s\",\"upload_id\":\"%s\"}",
      HEAR_CLIP_UPLOAD_SCHEMA_VERSION, device_id, node, boot, (unsigned long)sample,
      clip_basename, (unsigned long)clip_bytes, (unsigned long)chunk_bytes, upload_source,
      upload_id);
  return (n > 0 && (size_t)n < cap) ? n : -1;
}

static inline int hear_clip_complete_body(uint32_t clip_bytes, const char *sha256_hex,
                                          char *out, size_t cap) {
  int n = snprintf(out, cap, "{\"upload_schema_version\":%d,\"clip_bytes\":%lu,\"sha256\":\"%s\"}",
                   HEAR_CLIP_UPLOAD_SCHEMA_VERSION, (unsigned long)clip_bytes, sha256_hex);
  return (n > 0 && (size_t)n < cap) ? n : -1;
}

// ---------------------------------------------------------------- request paths
static inline int hear_clip_chunk_path(const char *upload_id, uint32_t chunk_index,
                                       char *out, size_t cap) {
  int n = snprintf(out, cap, HEAR_CLIP_CHUNK_PATH_FMT, upload_id, (unsigned)chunk_index);
  return (n > 0 && (size_t)n < cap) ? n : -1;
}

static inline int hear_clip_complete_path(const char *upload_id, char *out, size_t cap) {
  int n = snprintf(out, cap, HEAR_CLIP_COMPLETE_PATH_FMT, upload_id);
  return (n > 0 && (size_t)n < cap) ? n : -1;
}

static inline int hear_clip_status_path(const char *upload_id, char *out, size_t cap) {
  int n = snprintf(out, cap, HEAR_CLIP_STATUS_PATH_FMT, upload_id);
  return (n > 0 && (size_t)n < cap) ? n : -1;
}
