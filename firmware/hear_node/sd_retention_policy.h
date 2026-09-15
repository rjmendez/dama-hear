#ifndef HEAR_SD_RETENTION_POLICY_H
#define HEAR_SD_RETENTION_POLICY_H

#include <stdint.h>
#include <string.h>

#define SD_CACHE_FREE_TARGET_PCT 10u
#define SD_CACHE_FREE_TARGET_CAP_PCT 25u
#define SD_CACHE_FREE_FLOOR_BYTES (8ULL * 1024ULL * 1024ULL)

enum {
  SD_CACHE_PROTECTED = 0,
  SD_CACHE_ROLLING = 1,
  SD_CACHE_UNKNOWN_ROLLING = 2,
};

typedef struct { char path[80]; char key[96]; uint32_t size; } sd_cache_ent_t;

static inline int sd_cache_has_suffix(const char *s, const char *suf) {
  size_t n = strlen(s), m = strlen(suf);
  return n >= m && strcmp(s + n - m, suf) == 0;
}

static inline int sd_cache_has_prefix(const char *s, const char *pre) {
  return strncmp(s, pre, strlen(pre)) == 0;
}

static inline const char *sd_cache_base(const char *path) {
  const char *slash = strrchr(path, '/');
  return slash ? slash + 1 : path;
}

static inline uint64_t sd_cache_target_free_bytes(uint64_t total_bytes) {
  uint64_t pct = total_bytes * (uint64_t)SD_CACHE_FREE_TARGET_PCT / 100ULL;
  uint64_t cap = total_bytes * (uint64_t)SD_CACHE_FREE_TARGET_CAP_PCT / 100ULL;
  uint64_t target = pct > SD_CACHE_FREE_FLOOR_BYTES ? pct : SD_CACHE_FREE_FLOOR_BYTES;
  if (cap && target > cap) target = cap;
  return target;
}

static inline int sd_cache_classify_path(const char *path, int is_dir) {
  const char *base = sd_cache_base(path);
  if (!path || !path[0] || strcmp(path, "/") == 0) return SD_CACHE_PROTECTED;
  if (is_dir) return strcmp(path, "/clips") == 0 ? SD_CACHE_ROLLING : SD_CACHE_PROTECTED;

  if (strcmp(path, "/gate.cfg") == 0 || strcmp(path, "/gps.cfg") == 0 ||
      sd_cache_has_suffix(base, ".cfg") || sd_cache_has_suffix(base, ".json") ||
      sd_cache_has_suffix(base, ".state") || sd_cache_has_suffix(base, ".key") ||
      sd_cache_has_suffix(base, ".pem") || strstr(base, "secret") || strstr(base, "config") ||
      strstr(base, "prov") || strstr(base, "wifi") || strstr(base, "state")) {
    return SD_CACHE_PROTECTED;
  }

  if (sd_cache_has_prefix(path, "/clips/") && sd_cache_has_suffix(base, ".wav"))
    return SD_CACHE_ROLLING;
  if ((sd_cache_has_prefix(base, "scene-") && sd_cache_has_suffix(base, ".csv")) ||
      strcmp(base, "scene.csv") == 0 || strcmp(base, "scene-prev.csv") == 0 ||
      strcmp(base, "dets.csv") == 0 || strcmp(base, "dets-prev.csv") == 0 ||
      strcmp(base, "health.csv") == 0 || strcmp(base, "health-prev.csv") == 0 ||
      sd_cache_has_suffix(base, ".log")) {
    return SD_CACHE_ROLLING;
  }

  return SD_CACHE_UNKNOWN_ROLLING;
}

#endif
