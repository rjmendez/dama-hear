// Boot log ring, served over HTTP so a deployed node can be asked what happened at boot.
//
// FIRST module out of hear_node.ino, chosen because everything else calls logf() and because it
// is small enough that if the library mechanism does not work, little is lost proving it.
#pragma once
#include <Arduino.h>

#define HEAR_LOG_CAP 6144

void hear_log_put(const char *s, size_t n);
void hear_logf(const char *fmt, ...);
void hear_logln(const char *s);
void hear_logln(const String &s);
// The ring as one String, oldest first. Copies: the caller is an HTTP handler and the ring keeps
// being written while it serialises.
String hear_log_text();
