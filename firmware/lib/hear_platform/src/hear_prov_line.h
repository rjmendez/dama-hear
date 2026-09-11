// A node's enrolled identity and Wi-Fi, and the one-line USB command that sets it. Plain C, no
// Arduino, so tests/test_prov_line.py compiles it with cc and drives it with what enroll.py sends.
//
//   PROV v=1 node=<id> [class=<id>] net=<ssid hex>:<psk hex> [net=...] crc=<8 hex>
//
// Hex because an SSID may contain spaces, '=' or ':'. crc is CRC-32 (zlib's) of everything before
// " crc=", and must be the last token: the USB CDC receive path drops the rest of a packet when
// its queue is full, and a line with a hole in a PSK can still parse.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define HEAR_PROV_MAX_NETS 8
#define HEAR_PROV_ID_MAX   23
#define HEAR_PROV_LINE_MAX 1024

typedef struct {
  char node[HEAR_PROV_ID_MAX + 1];
  char cls[HEAR_PROV_ID_MAX + 1];
  int  n;
  char ssid[HEAR_PROV_MAX_NETS][33];
  char psk[HEAR_PROV_MAX_NETS][65];
} hear_prov_t;

// The node-id grammar gen_secrets.py enforces: [a-z0-9][a-z0-9-]{0,22}.
static inline int hear_prov_id_ok(const char *s) {
  size_t n = strlen(s);
  if (n < 1 || n > HEAR_PROV_ID_MAX) return 0;
  for (size_t i = 0; i < n; i++) {
    char c = s[i];
    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || (i > 0 && c == '-'))) return 0;
  }
  return 1;
}

static inline int hear_prov_nib(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Hex to a NUL-terminated string. Returns its length, or -1 for odd length, a bad digit, an
// embedded NUL, or a result that does not fit `cap` including the terminator.
static inline int hear_prov_unhex(const char *h, size_t hn, char *out, size_t cap) {
  if (hn % 2 || hn / 2 + 1 > cap) return -1;
  for (size_t i = 0; i < hn; i += 2) {
    int a = hear_prov_nib(h[i]), b = hear_prov_nib(h[i + 1]);
    if (a < 0 || b < 0 || (a | b) == 0) return -1;
    out[i / 2] = (char)(a << 4 | b);
  }
  out[hn / 2] = 0;
  return (int)(hn / 2);
}

// CRC-32 exactly as zlib.crc32 computes it: reflected 0xEDB88320, init and final xor 0xFFFFFFFF.
static inline uint32_t hear_prov_crc32(const char *s, size_t n) {
  uint32_t c = 0xFFFFFFFFu;
  for (size_t i = 0; i < n; i++) {
    c ^= (uint8_t)s[i];
    for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
  }
  return c ^ 0xFFFFFFFFu;
}

// The line must END in " crc=" and 8 hex digits matching everything before them. Returns where the
// signed part ends, or NULL with *why set.
static inline const char *hear_prov_signed_end(const char *line, const char **why) {
  size_t len = strlen(line);
  if (len < 13 || strncmp(line + len - 13, " crc=", 5) != 0) { *why = "missing crc"; return NULL; }
  uint32_t want = 0;
  for (size_t i = len - 8; i < len; i++) {
    int d = hear_prov_nib(line[i]);
    if (d < 0) { *why = "missing crc"; return NULL; }
    want = want << 4 | (uint32_t)d;
  }
  if (hear_prov_crc32(line, len - 13) != want) { *why = "bad crc"; return NULL; }
  return line + len - 13;
}

static inline const char *hear_prov_parse_(const char *line, hear_prov_t *p) {
  if (strncmp(line, "PROV ", 5) != 0) return "not a PROV line";
  if (strlen(line) >= HEAR_PROV_LINE_MAX) return "line too long";
  const char *why = NULL;
  const char *end = hear_prov_signed_end(line, &why);
  if (!end) return why;
  int have_v = 0;
  const char *s = line + 5;
  while (s < end) {
    while (s < end && *s == ' ') s++;
    if (s >= end) break;
    const char *e = s;
    while (e < end && *e != ' ') e++;
    const char *eq = (const char *)memchr(s, '=', (size_t)(e - s));
    if (!eq) return "token without '='";
    size_t kn = (size_t)(eq - s), vn = (size_t)(e - eq - 1);
    const char *v = eq + 1;
    if (kn == 1 && s[0] == 'v') {
      if (vn != 1 || v[0] != '1') return "unsupported version";
      have_v = 1;
    } else if ((kn == 4 && !strncmp(s, "node", 4)) || (kn == 5 && !strncmp(s, "class", 5))) {
      char *dst = kn == 4 ? p->node : p->cls;
      const char *bad = kn == 4 ? "bad node" : "bad class";
      if (vn > HEAR_PROV_ID_MAX) return bad;
      memcpy(dst, v, vn);
      dst[vn] = 0;
      if (!hear_prov_id_ok(dst)) return bad;
    } else if (kn == 3 && !strncmp(s, "net", 3)) {
      if (p->n >= HEAR_PROV_MAX_NETS) return "too many networks";
      const char *c = (const char *)memchr(v, ':', vn);
      if (!c) return "net without ':'";
      int sl = hear_prov_unhex(v, (size_t)(c - v), p->ssid[p->n], sizeof p->ssid[0]);
      int pl = hear_prov_unhex(c + 1, (size_t)(e - c - 1), p->psk[p->n], sizeof p->psk[0]);
      if (sl < 1 || sl > 32) return "bad ssid";
      if (pl < 8 || pl > 64) return "bad psk";
      p->n++;
    } else {
      return "unknown key";
    }
    s = e;
  }
  if (!have_v) return "missing v=1";
  if (!p->node[0]) return "missing node";
  if (p->n < 1) return "no networks";
  return NULL;
}

// NULL on success. On failure the reason, and *p is all zero: a half-parsed record is never used.
static inline const char *hear_prov_parse(const char *line, hear_prov_t *p) {
  memset(p, 0, sizeof *p);
  const char *err = hear_prov_parse_(line, p);
  if (err) memset(p, 0, sizeof *p);
  return err;
}

static inline int hear_prov_same(const hear_prov_t *a, const hear_prov_t *b) {
  if (a->n != b->n || strcmp(a->node, b->node) || strcmp(a->cls, b->cls)) return 0;
  for (int k = 0; k < a->n; k++)
    if (strcmp(a->ssid[k], b->ssid[k]) || strcmp(a->psk[k], b->psk[k])) return 0;
  return 1;
}
