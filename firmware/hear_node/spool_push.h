#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "hear_push_payload.h"
#include "spool_protocol.h"

#define HEAR_SPOOL_DIR "/spool"
#define HEAR_SPOOL_SEGMENT_GLOB "seg-"
#define HEAR_SPOOL_ACK_A_PATH "/spool/ack-a.state"
#define HEAR_SPOOL_ACK_B_PATH "/spool/ack-b.state"
#define HEAR_SPOOL_BATCH_PATH "/v1/ingest/batches"
#define HEAR_SPOOL_BATCH_MEDIA_TYPE "application/vnd.dama.hear.ingest.batch.v1+json"
#define HEAR_SPOOL_RECEIPT_MEDIA_TYPE "application/vnd.dama.hear.ingest.batch-receipt.v1+json"
#define HEAR_SPOOL_BATCH_MAX_ITEMS 8u
#define HEAR_SPOOL_BATCH_BODY_MAX 8192u
#define HEAR_SPOOL_BATCH_ID_MAX 96u
#define HEAR_SPOOL_IDEMPOTENCY_KEY_MAX 128u
#define HEAR_SPOOL_EVENT_JSON_MAX 1024u

typedef struct {
  uint32_t first_seq;
  uint32_t last_seq;
  uint32_t item_count;
  uint32_t backlog_records;
  uint32_t body_crc32;
  int ack_through_index;
  int retry_after_s;
  int retry_after_is_null;
  uint32_t acked_seq;
  uint32_t oldest_seq;
  uint32_t refused_in_prefix;
  char batch_id[HEAR_SPOOL_BATCH_ID_MAX];
  char idempotency_key[HEAR_SPOOL_IDEMPOTENCY_KEY_MAX];
} hear_spool_batch_plan_t;

static inline int hear_spool_event_record_encode(const hear_push_event_t *ev, uint32_t seq,
                                                 char *json_out, size_t json_cap,
                                                 uint8_t *record_out, size_t record_cap) {
  int json_n = 0;
  uint8_t flags = 0;
  if (!ev || !json_out || !record_out) return 0;
  json_n = hear_push_event_json(ev, json_out, json_cap);
  if (json_n <= 0) return 0;
  if (ev->time_valid) flags |= 0x01u;
  return hear_spool_record_encode(seq, flags, (const uint8_t *)json_out, (uint32_t)json_n,
                                  record_out, record_cap);
}

static inline int hear_spool_build_batch_from_records(const uint8_t *records, size_t records_len,
                                                      const char *node_id, const char *boot_id,
                                                      int64_t boot_epoch_us,
                                                      const char *sent_at_json,
                                                      uint32_t batch_sequence,
                                                      uint32_t backlog_records, char *body_out,
                                                      size_t body_cap, uint32_t *seqs_out,
                                                      size_t seqs_cap,
                                                      hear_spool_batch_plan_t *out) {
  hear_spool_batch_plan_t plan;
  size_t off = 0;
  size_t body_len = 0;
  char boot_epoch[32];
  if (!records || !records_len || !node_id || !node_id[0] || !boot_id || !boot_id[0] ||
      !sent_at_json || !body_out || !seqs_out || !out || !seqs_cap)
    return 0;
  memset(&plan, 0, sizeof plan);
  if (snprintf(plan.batch_id, sizeof plan.batch_id, "%s-%s-%lu", node_id, boot_id,
               (unsigned long)batch_sequence) <= 0)
    return 0;
  if (boot_epoch_us > 0) {
    if (snprintf(boot_epoch, sizeof boot_epoch, "%lld", (long long)boot_epoch_us) <= 0) return 0;
  } else {
    if (snprintf(boot_epoch, sizeof boot_epoch, "null") != 4) return 0;
  }
  {
    int n = snprintf(body_out, body_cap,
                     "{\"batch_schema_version\":1,\"batch_id\":\"%s\",\"device_id\":\"%s\","
                     "\"sent_at\":%s,\"producer\":{\"boot_id\":\"%s\",\"boot_epoch_us\":%s,"
                     "\"batch_sequence\":%lu,\"spool_backlog\":%lu},\"messages\":[",
                     plan.batch_id, node_id, sent_at_json, boot_id, boot_epoch,
                     (unsigned long)batch_sequence, (unsigned long)backlog_records);
    if (n <= 0 || (size_t)n >= body_cap) return 0;
    body_len = (size_t)n;
  }
  while (off < records_len && plan.item_count < HEAR_SPOOL_BATCH_MAX_ITEMS) {
    hear_spool_record_view_t rec;
    size_t rec_bytes = 0;
    int rc = hear_spool_record_parse(records + off, records_len - off, &rec);
    if (rc != HEAR_SPOOL_RECORD_OK) return 0;
    rec_bytes = HEAR_SPOOL_RECORD_HEADER_BYTES + (size_t)rec.len;
    if (plan.item_count >= seqs_cap) return 0;
    if (plan.item_count) {
      if (body_len + 1u >= body_cap) return 0;
      body_out[body_len++] = ',';
    }
    if (body_len + (size_t)rec.len + 3u >= body_cap) return 0;
    memcpy(body_out + body_len, rec.payload, rec.len);
    body_len += rec.len;
    seqs_out[plan.item_count] = rec.seq;
    if (!plan.item_count) plan.first_seq = rec.seq;
    plan.last_seq = rec.seq;
    plan.item_count++;
    off += rec_bytes;
  }
  if (!plan.item_count || off != records_len || body_len + 2u >= body_cap) return 0;
  body_out[body_len++] = ']';
  body_out[body_len++] = '}';
  body_out[body_len] = '\0';
  plan.backlog_records = backlog_records;
  plan.body_crc32 = hear_spool_crc32((const uint8_t *)body_out, body_len);
  if (snprintf(plan.idempotency_key, sizeof plan.idempotency_key, "%s.%s.%lu.%lu.%08lx", node_id,
               boot_id, (unsigned long)plan.first_seq, (unsigned long)plan.item_count,
               (unsigned long)plan.body_crc32) <= 0)
    return 0;
  *out = plan;
  return (int)body_len;
}

static inline int hear_spool_receipt_matches_batch_id(const uint8_t *body, size_t body_len,
                                                      const char *batch_id) {
  size_t pos = 0;
  char got[HEAR_SPOOL_BATCH_ID_MAX];
  if (!body || !body_len || !batch_id || !batch_id[0]) return 0;
  if (!hear_spool_locate_key_value(body, body_len, 0, "\"batch_id\"", &pos)) return 0;
  if (!hear_spool_parse_json_string(body, body_len, &pos, got, sizeof got)) return 0;
  return strcmp(got, batch_id) == 0;
}

static inline int hear_spool_plan_watermark_advance(
    const uint8_t *slot_a, size_t len_a, const uint8_t *slot_b, size_t len_b,
    const char *boot_id, const char *expected_batch_id, const uint8_t *receipt_body,
    size_t receipt_len, const uint32_t *seqs, size_t seq_count, uint8_t *planned_out,
    size_t planned_cap, int *target_slot, hear_spool_batch_plan_t *plan_out) {
  hear_spool_watermark_t current;
  hear_spool_receipt_scan_t receipt;
  hear_spool_batch_plan_t plan;
  int have_current = 0;
  memset(&plan, 0, sizeof plan);
  if (!boot_id || !expected_batch_id || !receipt_body || !seqs || !seq_count || !planned_out)
    return 0;
  have_current = hear_spool_watermark_choose(slot_a, len_a, slot_b, len_b, &current, 0);
  if (!hear_spool_scan_receipt(receipt_body, receipt_len, &receipt) || !receipt.valid) return 0;
  if ((size_t)receipt.results_seen != seq_count) return 0;
  if (!hear_spool_receipt_matches_batch_id(receipt_body, receipt_len, expected_batch_id)) return 0;
  plan.ack_through_index = receipt.ack_through_index;
  plan.retry_after_s = receipt.retry_after_s;
  plan.retry_after_is_null = receipt.retry_after_is_null;
  plan.refused_in_prefix = receipt.refused_in_prefix;
  if (receipt.ack_through_index < 0) {
    if (plan_out) *plan_out = plan;
    return 0;
  }
  plan.acked_seq = seqs[receipt.ack_through_index];
  plan.oldest_seq = (receipt.ack_through_index + 1 < (int)seq_count)
                        ? seqs[receipt.ack_through_index + 1]
                        : (plan.acked_seq + 1u);
  if (have_current && plan.acked_seq <= current.acked_seq) {
    if (plan_out) *plan_out = plan;
    return 0;
  }
  if (!hear_spool_watermark_plan_write(slot_a, len_a, slot_b, len_b, boot_id, plan.acked_seq,
                                       plan.oldest_seq, planned_out, planned_cap, target_slot))
    return 0;
  if (plan_out) *plan_out = plan;
  return 1;
}
