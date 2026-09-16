#ifndef HEAR_SPOOL_PROTOCOL_H
#define HEAR_SPOOL_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define HEAR_SPOOL_RECORD_MAGIC0 0x48u
#define HEAR_SPOOL_RECORD_MAGIC1 0x53u
#define HEAR_SPOOL_RECORD_FMT 1u
#define HEAR_SPOOL_RECORD_HEADER_BYTES 16u
#define HEAR_SPOOL_MAX_RECORD_BYTES 4096u
#define HEAR_SPOOL_SEGMENT_MAX_BYTES (64u * 1024u)

#define HEAR_SPOOL_WATERMARK_FMT 1u
#define HEAR_SPOOL_BOOT_ID_HEX_BYTES 16u
#define HEAR_SPOOL_WATERMARK_BYTES 33u

#define HEAR_SPOOL_RECEIPT_MAX_BYTES 4096u
#define HEAR_SPOOL_RECEIPT_MAX_RESULTS 64u

enum {
  HEAR_SPOOL_RECORD_OK = 0,
  HEAR_SPOOL_RECORD_TORN = 1,
  HEAR_SPOOL_RECORD_BAD_MAGIC = 2,
  HEAR_SPOOL_RECORD_BAD_FMT = 3,
  HEAR_SPOOL_RECORD_BAD_LEN = 4,
  HEAR_SPOOL_RECORD_BAD_CRC = 5,
};

enum {
  HEAR_SPOOL_SCAN_OK = 0,
  HEAR_SPOOL_SCAN_TORN_TAIL = 1,
  HEAR_SPOOL_SCAN_UNSUPPORTED_FMT = 2,
};

typedef struct {
  uint8_t fmt;
  uint8_t flags;
  uint32_t seq;
  uint32_t len;
  uint32_t crc32;
  const uint8_t *payload;
} hear_spool_record_view_t;

typedef struct {
  uint32_t records;
  uint32_t crc_drops;
  uint32_t torn_tail;
  uint32_t last_good_seq;
  size_t last_good_end;
  size_t truncate_offset;
  int status;
} hear_spool_scan_result_t;

typedef struct {
  char boot_id[HEAR_SPOOL_BOOT_ID_HEX_BYTES + 1u];
  uint32_t gen;
  uint32_t acked_seq;
  uint32_t oldest_seq;
} hear_spool_watermark_t;

typedef struct {
  int valid;
  int ack_through_index;
  int retry_after_s;
  int retry_after_is_null;
  uint32_t refused_in_prefix;
  uint32_t results_seen;
} hear_spool_receipt_scan_t;

static inline uint32_t hear_spool_u32le(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline void hear_spool_put_u32le(uint8_t *p, uint32_t v) {
  p[0] = (uint8_t)(v & 0xffu);
  p[1] = (uint8_t)((v >> 8) & 0xffu);
  p[2] = (uint8_t)((v >> 16) & 0xffu);
  p[3] = (uint8_t)((v >> 24) & 0xffu);
}

static inline int hear_spool_hex_nybble(unsigned char c) {
  if (c >= '0' && c <= '9') return (int)(c - '0');
  if (c >= 'a' && c <= 'f') return 10 + (int)(c - 'a');
  if (c >= 'A' && c <= 'F') return 10 + (int)(c - 'A');
  return -1;
}

static inline int hear_spool_boot_id_valid(const char *boot_id) {
  size_t i = 0;
  if (!boot_id) return 0;
  for (; i < HEAR_SPOOL_BOOT_ID_HEX_BYTES; ++i)
    if (hear_spool_hex_nybble((unsigned char)boot_id[i]) < 0) return 0;
  return boot_id[HEAR_SPOOL_BOOT_ID_HEX_BYTES] == '\0';
}

static inline uint32_t hear_spool_crc32(const uint8_t *data, size_t len) {
  uint32_t crc = 0xffffffffu;
  size_t i = 0;
  for (; i < len; ++i) {
    uint32_t x = (crc ^ data[i]) & 0xffu;
    unsigned k = 0;
    for (; k < 8u; ++k) x = (x & 1u) ? ((x >> 1) ^ 0xedb88320u) : (x >> 1);
    crc = (crc >> 8) ^ x;
  }
  return crc ^ 0xffffffffu;
}

static inline size_t hear_spool_record_size(uint32_t payload_len) {
  return payload_len > HEAR_SPOOL_MAX_RECORD_BYTES ? 0u
                                                   : (size_t)HEAR_SPOOL_RECORD_HEADER_BYTES + payload_len;
}

static inline int hear_spool_record_encode(uint32_t seq, uint8_t flags, const uint8_t *payload,
                                           uint32_t payload_len, uint8_t *out, size_t out_cap) {
  size_t need = hear_spool_record_size(payload_len);
  if (!out || !payload || !need || out_cap < need) return 0;
  out[0] = HEAR_SPOOL_RECORD_MAGIC0;
  out[1] = HEAR_SPOOL_RECORD_MAGIC1;
  out[2] = HEAR_SPOOL_RECORD_FMT;
  out[3] = flags;
  hear_spool_put_u32le(out + 4u, seq);
  hear_spool_put_u32le(out + 8u, payload_len);
  hear_spool_put_u32le(out + 12u, hear_spool_crc32(payload, payload_len));
  memcpy(out + HEAR_SPOOL_RECORD_HEADER_BYTES, payload, payload_len);
  return (int)need;
}

static inline int hear_spool_record_parse(const uint8_t *data, size_t len, hear_spool_record_view_t *out) {
  uint32_t payload_len = 0;
  uint32_t crc = 0;
  if (!data || len < HEAR_SPOOL_RECORD_HEADER_BYTES) return HEAR_SPOOL_RECORD_TORN;
  if (data[0] != HEAR_SPOOL_RECORD_MAGIC0 || data[1] != HEAR_SPOOL_RECORD_MAGIC1)
    return HEAR_SPOOL_RECORD_BAD_MAGIC;
  if (data[2] != HEAR_SPOOL_RECORD_FMT) return HEAR_SPOOL_RECORD_BAD_FMT;
  payload_len = hear_spool_u32le(data + 8u);
  if (payload_len > HEAR_SPOOL_MAX_RECORD_BYTES) return HEAR_SPOOL_RECORD_BAD_LEN;
  if (len < (size_t)HEAR_SPOOL_RECORD_HEADER_BYTES + payload_len) return HEAR_SPOOL_RECORD_TORN;
  crc = hear_spool_u32le(data + 12u);
  if (hear_spool_crc32(data + HEAR_SPOOL_RECORD_HEADER_BYTES, payload_len) != crc)
    return HEAR_SPOOL_RECORD_BAD_CRC;
  if (out) {
    out->fmt = data[2];
    out->flags = data[3];
    out->seq = hear_spool_u32le(data + 4u);
    out->len = payload_len;
    out->crc32 = crc;
    out->payload = data + HEAR_SPOOL_RECORD_HEADER_BYTES;
  }
  return HEAR_SPOOL_RECORD_OK;
}

static inline size_t hear_spool_find_next_candidate(const uint8_t *data, size_t len, size_t start) {
  size_t i = start;
  if (!data) return len;
  for (; i + 1u < len; ++i)
    if (data[i] == HEAR_SPOOL_RECORD_MAGIC0 && data[i + 1u] == HEAR_SPOOL_RECORD_MAGIC1) return i;
  return len;
}

static inline size_t hear_spool_find_next_valid_record(const uint8_t *data, size_t len, size_t start) {
  size_t off = hear_spool_find_next_candidate(data, len, start);
  for (; off < len; off = hear_spool_find_next_candidate(data, len, off + 1u)) {
    if (hear_spool_record_parse(data + off, len - off, 0) == HEAR_SPOOL_RECORD_OK) return off;
  }
  return len;
}

static inline int hear_spool_scan_segment(const uint8_t *data, size_t len, hear_spool_scan_result_t *out) {
  hear_spool_scan_result_t state;
  size_t off = 0;
  if (!out) return 0;
  memset(&state, 0, sizeof state);
  state.truncate_offset = len;
  state.status = HEAR_SPOOL_SCAN_OK;
  while (off < len) {
    hear_spool_record_view_t rec;
    int rc = hear_spool_record_parse(data + off, len - off, &rec);
    if (rc == HEAR_SPOOL_RECORD_OK) {
      state.records++;
      state.last_good_seq = rec.seq;
      off += HEAR_SPOOL_RECORD_HEADER_BYTES + rec.len;
      state.last_good_end = off;
      continue;
    }
    if (rc == HEAR_SPOOL_RECORD_BAD_FMT && data[off] == HEAR_SPOOL_RECORD_MAGIC0 &&
        off + 1u < len && data[off + 1u] == HEAR_SPOOL_RECORD_MAGIC1) {
      state.truncate_offset = state.last_good_end;
      state.status = HEAR_SPOOL_SCAN_UNSUPPORTED_FMT;
      *out = state;
      return 1;
    }
    {
      size_t next = hear_spool_find_next_valid_record(data, len, off + 1u);
      if (next < len) {
        state.crc_drops++;
        off = next;
        continue;
      }
    }
    state.torn_tail = 1u;
    state.truncate_offset = off;
    state.status = HEAR_SPOOL_SCAN_TORN_TAIL;
    *out = state;
    return 1;
  }
  *out = state;
  return 1;
}

static inline int hear_spool_watermark_encode(const hear_spool_watermark_t *wm, uint8_t *out,
                                              size_t out_cap) {
  uint32_t crc = 0;
  if (!wm || !out || out_cap < HEAR_SPOOL_WATERMARK_BYTES || !hear_spool_boot_id_valid(wm->boot_id))
    return 0;
  out[0] = HEAR_SPOOL_WATERMARK_FMT;
  memcpy(out + 1u, wm->boot_id, HEAR_SPOOL_BOOT_ID_HEX_BYTES);
  hear_spool_put_u32le(out + 17u, wm->gen);
  hear_spool_put_u32le(out + 21u, wm->acked_seq);
  hear_spool_put_u32le(out + 25u, wm->oldest_seq);
  crc = hear_spool_crc32(out, HEAR_SPOOL_WATERMARK_BYTES - 4u);
  hear_spool_put_u32le(out + 29u, crc);
  return (int)HEAR_SPOOL_WATERMARK_BYTES;
}

static inline int hear_spool_watermark_decode(const uint8_t *data, size_t len,
                                              hear_spool_watermark_t *out) {
  char boot_id[HEAR_SPOOL_BOOT_ID_HEX_BYTES + 1u];
  if (!data || len < HEAR_SPOOL_WATERMARK_BYTES) return 0;
  if (data[0] != HEAR_SPOOL_WATERMARK_FMT) return 0;
  if (hear_spool_crc32(data, HEAR_SPOOL_WATERMARK_BYTES - 4u) != hear_spool_u32le(data + 29u))
    return 0;
  memcpy(boot_id, data + 1u, HEAR_SPOOL_BOOT_ID_HEX_BYTES);
  boot_id[HEAR_SPOOL_BOOT_ID_HEX_BYTES] = '\0';
  if (!hear_spool_boot_id_valid(boot_id)) return 0;
  if (out) {
    memcpy(out->boot_id, boot_id, sizeof out->boot_id);
    out->gen = hear_spool_u32le(data + 17u);
    out->acked_seq = hear_spool_u32le(data + 21u);
    out->oldest_seq = hear_spool_u32le(data + 25u);
  }
  return 1;
}

static inline int hear_spool_watermark_choose(const uint8_t *slot_a, size_t len_a, const uint8_t *slot_b,
                                              size_t len_b, hear_spool_watermark_t *out,
                                              int *chosen_slot) {
  hear_spool_watermark_t a, b;
  int a_ok = hear_spool_watermark_decode(slot_a, len_a, &a);
  int b_ok = hear_spool_watermark_decode(slot_b, len_b, &b);
  if (!a_ok && !b_ok) return 0;
  if (!b_ok || (a_ok && a.gen >= b.gen)) {
    if (out) *out = a;
    if (chosen_slot) *chosen_slot = 0;
    return 1;
  }
  if (out) *out = b;
  if (chosen_slot) *chosen_slot = 1;
  return 1;
}

static inline int hear_spool_watermark_plan_write(const uint8_t *slot_a, size_t len_a,
                                                  const uint8_t *slot_b, size_t len_b,
                                                  const char *boot_id, uint32_t acked_seq,
                                                  uint32_t oldest_seq, uint8_t *out,
                                                  size_t out_cap, int *target_slot) {
  hear_spool_watermark_t cur, next;
  int active = -1;
  if (!boot_id || !out || !hear_spool_boot_id_valid(boot_id)) return 0;
  memset(&next, 0, sizeof next);
  memcpy(next.boot_id, boot_id, HEAR_SPOOL_BOOT_ID_HEX_BYTES + 1u);
  next.acked_seq = acked_seq;
  next.oldest_seq = oldest_seq;
  if (hear_spool_watermark_choose(slot_a, len_a, slot_b, len_b, &cur, &active))
    next.gen = cur.gen + 1u;
  else
    next.gen = 1u;
  if (!hear_spool_watermark_encode(&next, out, out_cap)) return 0;
  if (target_slot) *target_slot = (active == 0) ? 1 : 0;
  return 1;
}

static inline int hear_spool_is_ws(unsigned char c) {
  return c == ' ' || c == '\n' || c == '\r' || c == '\t';
}

static inline size_t hear_spool_skip_ws(const uint8_t *buf, size_t len, size_t pos) {
  while (pos < len && hear_spool_is_ws(buf[pos])) ++pos;
  return pos;
}

static inline int hear_spool_match_bytes(const uint8_t *buf, size_t len, size_t pos, const char *lit) {
  size_t n = lit ? strlen(lit) : 0u;
  return buf && lit && pos + n <= len && memcmp(buf + pos, lit, n) == 0;
}

static inline int hear_spool_parse_json_int(const uint8_t *buf, size_t len, size_t *pos, int *out) {
  int sign = 1;
  int value = 0;
  size_t p = hear_spool_skip_ws(buf, len, *pos);
  if (p >= len) return 0;
  if (buf[p] == '-') {
    sign = -1;
    ++p;
  }
  if (p >= len || buf[p] < '0' || buf[p] > '9') return 0;
  for (; p < len && buf[p] >= '0' && buf[p] <= '9'; ++p) {
    if (value > 214748364 || (value == 214748364 && buf[p] > (sign < 0 ? '8' : '7'))) return 0;
    value = value * 10 + (int)(buf[p] - '0');
  }
  *out = value * sign;
  *pos = p;
  return 1;
}

static inline int hear_spool_parse_json_string(const uint8_t *buf, size_t len, size_t *pos,
                                               char *out, size_t out_cap) {
  size_t p = hear_spool_skip_ws(buf, len, *pos);
  size_t n = 0;
  int esc = 0;
  if (!out || !out_cap || p >= len || buf[p] != '"') return 0;
  ++p;
  for (; p < len; ++p) {
    unsigned char c = buf[p];
    if (esc) {
      if (n + 1u >= out_cap) return 0;
      out[n++] = (char)c;
      esc = 0;
      continue;
    }
    if (c == '\\') {
      esc = 1;
      continue;
    }
    if (c == '"') {
      out[n] = '\0';
      *pos = p + 1u;
      return 1;
    }
    if (n + 1u >= out_cap) return 0;
    out[n++] = (char)c;
  }
  return 0;
}

static inline size_t hear_spool_find_key(const uint8_t *buf, size_t len, size_t start,
                                         const char *key) {
  size_t key_len = key ? strlen(key) : 0u;
  size_t i = start;
  int in_string = 0, esc = 0;
  if (!buf || !key || !key_len || start >= len) return len;
  for (; i + key_len <= len; ++i) {
    unsigned char c = buf[i];
    if (in_string) {
      if (esc) {
        esc = 0;
      } else if (c == '\\') {
        esc = 1;
      } else if (c == '"') {
        in_string = 0;
      }
      continue;
    }
    if (c == '"') {
      if (memcmp(buf + i, key, key_len) != 0) {
        in_string = 1;
        continue;
      }
    }
    if (memcmp(buf + i, key, key_len) == 0) {
      size_t after = hear_spool_skip_ws(buf, len, i + key_len);
      if (after < len && buf[after] == ':') return i;
    }
  }
  return len;
}

static inline int hear_spool_locate_key_value(const uint8_t *buf, size_t len, size_t start,
                                              const char *key, size_t *value_pos) {
  size_t at = hear_spool_find_key(buf, len, start, key);
  if (at >= len) return 0;
  at = hear_spool_skip_ws(buf, len, at + strlen(key));
  if (at >= len || buf[at] != ':') return 0;
  *value_pos = hear_spool_skip_ws(buf, len, at + 1u);
  return *value_pos < len;
}

static inline int hear_spool_scan_result_object(const uint8_t *buf, size_t obj_start, size_t obj_end,
                                                int *index_out, char *status_out,
                                                size_t status_cap) {
  size_t pos = 0;
  if (!buf || obj_start >= obj_end) return 0;
  if (!hear_spool_locate_key_value(buf + obj_start, obj_end - obj_start, 0, "\"index\"", &pos))
    return 0;
  if (!hear_spool_parse_json_int(buf + obj_start, obj_end - obj_start, &pos, index_out)) return 0;
  if (!hear_spool_locate_key_value(buf + obj_start, obj_end - obj_start, 0, "\"status\"", &pos))
    return 0;
  if (!hear_spool_parse_json_string(buf + obj_start, obj_end - obj_start, &pos, status_out,
                                    status_cap))
    return 0;
  return 1;
}

static inline int hear_spool_scan_receipt(const uint8_t *buf, size_t len,
                                          hear_spool_receipt_scan_t *out) {
  hear_spool_receipt_scan_t state;
  size_t pos = 0, arr = 0;
  uint64_t seen_mask = 0;
  uint32_t max_index = 0;
  if (!buf || !out || !len || len > HEAR_SPOOL_RECEIPT_MAX_BYTES) return 0;
  memset(&state, 0, sizeof state);
  state.valid = 0;
  state.ack_through_index = -1;
  if (!hear_spool_locate_key_value(buf, len, 0, "\"ack_through_index\"", &pos)) return 0;
  if (!hear_spool_parse_json_int(buf, len, &pos, &state.ack_through_index)) return 0;
  if (state.ack_through_index < -1) return 0;
  if (!hear_spool_locate_key_value(buf, len, 0, "\"retry_after_s\"", &pos)) return 0;
  if (hear_spool_match_bytes(buf, len, pos, "null")) {
    state.retry_after_is_null = 1;
    pos += 4u;
  } else {
    if (!hear_spool_parse_json_int(buf, len, &pos, &state.retry_after_s)) return 0;
    if (state.retry_after_s < 0) return 0;
  }
  if (!hear_spool_locate_key_value(buf, len, 0, "\"results\"", &arr)) return 0;
  arr = hear_spool_skip_ws(buf, len, arr);
  if (arr >= len || buf[arr] != '[') return 0;
  pos = arr + 1u;
  for (;;) {
    size_t obj_start = 0, obj_end = 0;
    size_t depth = 0;
    int in_string = 0, esc = 0;
    int idx = 0;
    char status[16];
    pos = hear_spool_skip_ws(buf, len, pos);
    if (pos >= len) return 0;
    if (buf[pos] == ']') {
      ++pos;
      break;
    }
    if (state.results_seen >= HEAR_SPOOL_RECEIPT_MAX_RESULTS || buf[pos] != '{') return 0;
    obj_start = pos;
    for (; pos < len; ++pos) {
      unsigned char c = buf[pos];
      if (in_string) {
        if (esc) {
          esc = 0;
        } else if (c == '\\') {
          esc = 1;
        } else if (c == '"') {
          in_string = 0;
        }
        continue;
      }
      if (c == '"') {
        in_string = 1;
      } else if (c == '{') {
        ++depth;
      } else if (c == '}') {
        if (!depth) return 0;
        --depth;
        if (!depth) {
          obj_end = pos + 1u;
          break;
        }
      }
    }
    if (!obj_end) return 0;
    if (!hear_spool_scan_result_object(buf, obj_start, obj_end, &idx, status, sizeof status)) return 0;
    if (idx < 0 || idx >= (int)HEAR_SPOOL_RECEIPT_MAX_RESULTS) return 0;
    if (seen_mask & (1ULL << idx)) return 0;
    seen_mask |= (1ULL << idx);
    if ((uint32_t)idx > max_index) max_index = (uint32_t)idx;
    if (strcmp(status, "accepted") && strcmp(status, "duplicate") && strcmp(status, "refused") &&
        strcmp(status, "deferred"))
      return 0;
    if (idx <= state.ack_through_index && strcmp(status, "refused") == 0) state.refused_in_prefix++;
    state.results_seen++;
    pos = hear_spool_skip_ws(buf, len, obj_end);
    if (pos >= len) return 0;
    if (buf[pos] == ',') {
      ++pos;
      continue;
    }
    if (buf[pos] == ']') {
      ++pos;
      break;
    }
    return 0;
  }
  if (state.results_seen == 0u && state.ack_through_index != -1) return 0;
  if (state.results_seen > 0u) {
    uint64_t expected = (state.results_seen == 64u) ? ~0ULL : ((1ULL << state.results_seen) - 1ULL);
    if (max_index + 1u != state.results_seen || seen_mask != expected) return 0;
  }
  if (state.ack_through_index >= (int)state.results_seen) return 0;
  state.valid = 1;
  *out = state;
  return 1;
}

#endif
