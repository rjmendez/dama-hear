#include "hear_log.h"
#include <stdarg.h>

// Ring, not a growing buffer: a node that runs for weeks must not have its log be the thing that
// exhausts the heap. Oldest bytes are overwritten, and the read starts at the write head when the
// ring has wrapped.
static char s_buf[HEAR_LOG_CAP];
static size_t s_w = 0;
static bool s_wrapped = false;

void hear_log_put(const char *s, size_t n) {
  for (size_t i = 0; i < n; i++) {
    s_buf[s_w] = s[i];
    s_w = (s_w + 1) % HEAR_LOG_CAP;
    if (s_w == 0) s_wrapped = true;
  }
}

void hear_logf(const char *fmt, ...) {
  char b[256];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(b, sizeof b, fmt, ap);
  va_end(ap);
  if (n < 0) return;
  if (n > (int)sizeof b - 1) n = sizeof b - 1;
  Serial.write((const uint8_t *)b, n);
  hear_log_put(b, n);
}

void hear_logln(const char *s) { hear_logf("%s\n", s); }
void hear_logln(const String &s) { hear_logf("%s\n", s.c_str()); }

String hear_log_text() {
  String o;
  o.reserve(HEAR_LOG_CAP + 1);
  if (s_wrapped) for (size_t i = s_w; i < HEAR_LOG_CAP; i++) o += s_buf[i];
  for (size_t i = 0; i < s_w; i++) o += s_buf[i];
  return o;
}
