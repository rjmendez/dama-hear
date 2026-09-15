#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>

typedef struct {
  const char *device_id;
  const char *node_class;
  const char *fw_version;
  unsigned long uptime_s;
  int gps_fix;
  int time_valid;
  int64_t utc_us;
  const char *clock_state;
  uint64_t sync_sigma_ns;
  uint64_t anchor_age_us;
  int64_t boot_epoch_us;
  const char *boot_id;
  unsigned int discontinuity_flags;
  int wifi_has_rssi;
  int wifi_rssi_dbm;
  unsigned long scene_rows_written;
  unsigned long dets_rows_written;
  unsigned long clips_written;
  unsigned long clips_evicted;
} hear_push_heartbeat_t;

typedef struct {
  const char *device_id;
  const char *node_class;
  const char *fw_version;
  unsigned long uptime_s;
  const char *event_type;
  unsigned long event_seq;
  int time_valid;
  int64_t utc_us;
  const char *clock_state;
  uint64_t sync_sigma_ns;
  uint64_t anchor_age_us;
  int64_t boot_epoch_us;
  const char *boot_id;
  unsigned int discontinuity_flags;
  const char *clip_basename;
  unsigned long clips_written;
  unsigned long clips_evicted;
  unsigned long dets_rows_written;
  unsigned long batch_rows;
} hear_push_event_t;

static inline int hear_push_rfc3339(int64_t utc_us, char *out, size_t n) {
  if (!out || n < 21 || utc_us < 0) return 0;
  time_t sec = (time_t)(utc_us / 1000000LL);
  struct tm tmv;
  if (!gmtime_r(&sec, &tmv)) return 0;
  int m = snprintf(out, n, "%04d-%02d-%02dT%02d:%02d:%02dZ",
                   tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
                   tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
  return (m > 0 && (size_t)m < n) ? m : 0;
}

static inline int hear_push_ts_field(int time_valid, int64_t utc_us, char *out, size_t n) {
  if (!out || n < 5) return 0;
  if (!time_valid) {
    int m = snprintf(out, n, "null");
    return (m > 0 && (size_t)m < n) ? m : 0;
  }
  char ts[32];
  if (!hear_push_rfc3339(utc_us, ts, sizeof ts)) return 0;
  int m = snprintf(out, n, "\"%s\"", ts);
  return (m > 0 && (size_t)m < n) ? m : 0;
}

static inline long long hear_push_ts_ms(int time_valid, int64_t utc_us, unsigned long uptime_s) {
  if (time_valid) return (long long)(utc_us / 1000);
  // No wall-clock yet (no GPS fix): AWS's ingest Lambda quarantines any message whose ts_ms/ts/
  // timestamp isn't a positive number, so "ts":null (below) alone would silently drop every
  // heartbeat/event sent before first fix. This monotonic-but-not-epoch fallback only satisfies
  // that gate; consumers must not treat it as wall-clock time -- "time":{"valid":false} says so,
  // and the receiver's own "received_at" is what's trustworthy in that case.
  return (long long)uptime_s * 1000 + 1;
}

static inline int hear_push_time_json(int time_valid, const char *clock_state,
                                      uint64_t sync_sigma_ns, uint64_t anchor_age_us,
                                      int64_t boot_epoch_us, const char *boot_id,
                                      unsigned int discontinuity_flags, char *out, size_t n) {
  if (!out || !n || !clock_state || !clock_state[0] || !boot_id || !boot_id[0]) return 0;
  char sync[32], age[32], boot[32];
  if (time_valid) {
    int m = snprintf(sync, sizeof sync, "%llu", (unsigned long long)sync_sigma_ns);
    if (m <= 0 || (size_t)m >= sizeof sync) return 0;
    m = snprintf(age, sizeof age, "%llu", (unsigned long long)anchor_age_us);
    if (m <= 0 || (size_t)m >= sizeof age) return 0;
    m = snprintf(boot, sizeof boot, "%lld", (long long)boot_epoch_us);
    if (m <= 0 || (size_t)m >= sizeof boot) return 0;
  } else {
    if (snprintf(sync, sizeof sync, "null") != 4) return 0;
    if (snprintf(age, sizeof age, "null") != 4) return 0;
    if (snprintf(boot, sizeof boot, "null") != 4) return 0;
  }
  int m = snprintf(out, n,
                   "{\"valid\":%s,\"state\":\"%s\",\"sync_sigma_ns\":%s,"
                   "\"anchor_age_us\":%s,\"boot_epoch_us\":%s,\"boot_id\":\"%s\","
                   "\"discontinuity_flags\":%u}",
                   time_valid ? "true" : "false", clock_state, sync, age, boot, boot_id,
                   discontinuity_flags);
  return (m > 0 && (size_t)m < n) ? m : 0;
}

static inline int hear_push_heartbeat_json(const hear_push_heartbeat_t *hb, char *out, size_t n) {
  if (!hb || !out || !n || !hb->device_id || !hb->node_class || !hb->fw_version) return 0;
  char ts[40], rssi[16], time_json[224];
  if (!hear_push_ts_field(hb->time_valid, hb->utc_us, ts, sizeof ts)) return 0;
  if (!hear_push_time_json(hb->time_valid, hb->clock_state, hb->sync_sigma_ns,
                           hb->anchor_age_us, hb->boot_epoch_us, hb->boot_id,
                           hb->discontinuity_flags, time_json, sizeof time_json)) return 0;
  if (hb->wifi_has_rssi) {
    int m = snprintf(rssi, sizeof rssi, "%d", hb->wifi_rssi_dbm);
    if (m <= 0 || (size_t)m >= sizeof rssi) return 0;
  } else {
    int m = snprintf(rssi, sizeof rssi, "null");
    if (m <= 0 || (size_t)m >= sizeof rssi) return 0;
  }
  int m = snprintf(
      out, n,
      "{\"telemetry_path\":\"hear/heartbeat\",\"telemetry_schema_version\":1,"
      "\"device_id\":\"%s\",\"ts\":%s,\"ts_ms\":%lld,\"class\":\"%s\",\"fw_version\":\"%s\","
      "\"uptime_s\":%lu,\"gps\":{\"fix\":%d},\"time\":%s,"
      "\"wifi\":{\"rssi_dbm\":%s},\"counters\":{\"scene_rows_written\":%lu,"
      "\"dets_rows_written\":%lu,\"clips_written\":%lu,\"clips_evicted\":%lu}}",
      hb->device_id, ts, hear_push_ts_ms(hb->time_valid, hb->utc_us, hb->uptime_s),
      hb->node_class, hb->fw_version, hb->uptime_s, hb->gps_fix,
      time_json, rssi, hb->scene_rows_written, hb->dets_rows_written,
      hb->clips_written, hb->clips_evicted);
  return (m > 0 && (size_t)m < n) ? m : 0;
}

static inline int hear_push_event_json(const hear_push_event_t *ev, char *out, size_t n) {
  if (!ev || !out || !n || !ev->device_id || !ev->node_class || !ev->fw_version || !ev->event_type)
    return 0;
  char ts[40], time_json[224];
  if (!hear_push_ts_field(ev->time_valid, ev->utc_us, ts, sizeof ts)) return 0;
  if (!hear_push_time_json(ev->time_valid, ev->clock_state, ev->sync_sigma_ns,
                           ev->anchor_age_us, ev->boot_epoch_us, ev->boot_id,
                           ev->discontinuity_flags, time_json, sizeof time_json)) return 0;
  long long ts_ms = hear_push_ts_ms(ev->time_valid, ev->utc_us, ev->uptime_s);
  int m = 0;
  if (ev->clip_basename && ev->clip_basename[0]) {
    m = snprintf(
        out, n,
        "{\"telemetry_path\":\"hear/event\",\"telemetry_schema_version\":1,"
        "\"device_id\":\"%s\",\"ts\":%s,\"ts_ms\":%lld,\"class\":\"%s\",\"fw_version\":\"%s\","
        "\"uptime_s\":%lu,\"time\":%s,\"event_type\":\"%s\",\"event_seq\":%lu,"
        "\"event\":{\"clip_basename\":\"%s\",\"clips_written\":%lu,\"clips_evicted\":%lu}}",
        ev->device_id, ts, ts_ms, ev->node_class, ev->fw_version, ev->uptime_s, time_json,
        ev->event_type,
        ev->event_seq, ev->clip_basename, ev->clips_written, ev->clips_evicted);
  } else {
    m = snprintf(
        out, n,
        "{\"telemetry_path\":\"hear/event\",\"telemetry_schema_version\":1,"
        "\"device_id\":\"%s\",\"ts\":%s,\"ts_ms\":%lld,\"class\":\"%s\",\"fw_version\":\"%s\","
        "\"uptime_s\":%lu,\"time\":%s,\"event_type\":\"%s\",\"event_seq\":%lu,"
        "\"event\":{\"dets_rows_written\":%lu,\"batch_rows\":%lu}}",
        ev->device_id, ts, ts_ms, ev->node_class, ev->fw_version, ev->uptime_s, time_json,
        ev->event_type,
        ev->event_seq, ev->dets_rows_written, ev->batch_rows);
  }
  return (m > 0 && (size_t)m < n) ? m : 0;
}
