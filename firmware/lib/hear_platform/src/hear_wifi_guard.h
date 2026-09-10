// A build with no credentials must FAIL, not quietly produce a node nobody can reach.
//
// ⚠️INCLUDE THIS AFTER secrets.h. It decides on WIFI_N, so included before it the guard would fire
// on every build, credentials or not. Anywhere after is fine -- tests/test_secretless_build_fails.py
// asserts the ORDER, which is the constraint that actually matters, not adjacency.
//
// WHAT THIS COSTS WHEN IT IS MISSING, measured 2026-09-10: rankine was flashed with an image built
// without secrets.h. `#ifndef WIFI_N` supplied WIFI_N 0 and two empty string arrays, the compile
// reported success, and the node came up in AP mode broadcasting its own SSID on 192.168.4.1 --
// invisible from the LAN. It sat there awake for hours with a GPS fix, writing scene rows to its
// card, while it was diagnosed as bricked and physically retrieved. The one place that said what
// had happened was the serial console, which is exactly the thing a deployed node does not have.
//
// The fallback itself is legitimate -- a bench build, a compile check, a node deliberately brought
// up as its own AP -- so it is kept, behind a flag the builder has to type:
//
//     arduino-cli compile --build-property "compiler.cpp.extra_flags=-DHEAR_ALLOW_NO_WIFI" ...
//
// ⚠️AND AN AP-ONLY IMAGE SAYS SO AT RUNTIME. HEAR_WIFI_CONFIGURED reaches /status, because the
// second-worst outcome after "unreachable and silent" is "reachable and lying about why".
#pragma once

// ⚠️`WIFI_N 0` IS NOT CONFIGURED, IT IS A HOLE. Testing only `defined(WIFI_N)` let a secrets.h
// that declares zero networks sail through the guard AND report configured:true -- strictly worse
// than having no secrets.h at all, because it looks deliberate. That file is generated from a
// credentials store by a parser; the one written on 2026-09-10 would have emitted exactly
// `#define WIFI_N 0` had its regex matched nothing. Count, do not merely detect.
#if defined(WIFI_N)
#  if (WIFI_N) <= 0
     // secrets.h exists but declares no networks. Its arrays are already defined, so nothing is
     // redeclared here -- only the verdict changes.
#    if !defined(HEAR_ALLOW_NO_WIFI)
#      error "secrets.h declares WIFI_N <= 0: this build would come up as its own AP and be \
unreachable from the LAN. Fix the credentials, or pass -DHEAR_ALLOW_NO_WIFI if an AP-only image \
is what you actually want."
#    endif
#    define HEAR_WIFI_CONFIGURED 0
#  else
#    define HEAR_WIFI_CONFIGURED 1
#  endif
#else
#  if defined(HEAR_ALLOW_NO_WIFI)
#    define WIFI_N 0
static const char *WIFI_SSIDS[]  = {""};
static const char *WIFI_PASSES[] = {""};
#    define HEAR_WIFI_CONFIGURED 0
#  else
#    error "No secrets.h: this build would come up as its own AP and be unreachable from the LAN. \
Generate firmware/<sketch>/secrets.h defining WIFI_N, WIFI_SSIDS[] and WIFI_PASSES[] (it is \
gitignored), or pass -DHEAR_ALLOW_NO_WIFI if an AP-only image is what you actually want."
#  endif
#endif
