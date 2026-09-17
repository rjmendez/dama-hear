#pragma once

// hear.ingest.clip.v1 -- wire constants for chunked WAV upload from a node.
//
// Declarations only: no socket, no SD access, no ring read, no allocation. The contract is
// docs/phase4-push-clip-upload.md and hear/ingest/clipupload.py, and
// tests/test_clip_upload_contract.py fails if the two drift apart.
//
// A no-SD node (gold, ageev, kasami) uploads straight out of the PSRAM raw ring:
// HEAR_CLIP_UPLOAD_SOURCE_RING is the declared source, and the server treats it exactly like
// an SD-backed upload. A chunk is 32 KiB, so one 480,044 B clip is 15 chunks and the largest
// buffer this path needs is HEAR_CLIP_CHUNK_BYTES, not the clip.

#include <stddef.h>
#include <stdint.h>

#define HEAR_CLIP_UPLOAD_SCHEMA_VERSION 1

#define HEAR_CLIP_INIT_PATH "/v1/ingest/clips"
#define HEAR_CLIP_CHUNK_PATH_FMT "/v1/ingest/clips/%s/chunks/%u"
#define HEAR_CLIP_COMPLETE_PATH_FMT "/v1/ingest/clips/%s/complete"
#define HEAR_CLIP_STATUS_PATH_FMT "/v1/ingest/clips/%s"

#define HEAR_CLIP_INIT_MEDIA_TYPE "application/vnd.dama.hear.ingest.clip-init.v1+json"
#define HEAR_CLIP_COMPLETE_MEDIA_TYPE "application/vnd.dama.hear.ingest.clip-complete.v1+json"
#define HEAR_CLIP_STATUS_MEDIA_TYPE "application/vnd.dama.hear.ingest.clip-status.v1+json"
#define HEAR_CLIP_CHUNK_MEDIA_TYPE "application/octet-stream"

#define HEAR_CLIP_CHUNK_DIGEST_HEADER "X-Hear-Chunk-SHA256"

#define HEAR_CLIP_CHUNK_BYTES 32768u
#define HEAR_CLIP_MAX_CHUNK_BYTES 32768u
#define HEAR_CLIP_MAX_CLIP_BYTES 524288u
#define HEAR_CLIP_MAX_CHUNKS 16u
#define HEAR_CLIP_NOMINAL_CLIP_BYTES 480044u
#define HEAR_CLIP_UPLOAD_TTL_S 3600u
#define HEAR_CLIP_MAX_OPEN_UPLOADS 4u

#define HEAR_CLIP_UPLOAD_ID_MAX 33u
#define HEAR_CLIP_IDEMPOTENCY_KEY_MAX 128u

#define HEAR_CLIP_UPLOAD_SOURCE_RING "psram_ring"
#define HEAR_CLIP_UPLOAD_SOURCE_SD "sd"

// How many chunks a clip of clip_bytes is cut into; 0 when the size is not uploadable.
static inline uint32_t hear_clip_chunk_count(uint32_t clip_bytes) {
  if (!clip_bytes || clip_bytes > HEAR_CLIP_MAX_CLIP_BYTES) return 0;
  return (clip_bytes + HEAR_CLIP_CHUNK_BYTES - 1u) / HEAR_CLIP_CHUNK_BYTES;
}

// Exact Content-Length of one chunk; 0 when the index is outside the clip.
static inline uint32_t hear_clip_chunk_len(uint32_t chunk_index, uint32_t clip_bytes) {
  uint32_t total = hear_clip_chunk_count(clip_bytes);
  uint32_t start = 0;
  if (!total || chunk_index >= total) return 0;
  start = chunk_index * HEAR_CLIP_CHUNK_BYTES;
  return (clip_bytes - start < HEAR_CLIP_CHUNK_BYTES) ? (clip_bytes - start)
                                                      : HEAR_CLIP_CHUNK_BYTES;
}
