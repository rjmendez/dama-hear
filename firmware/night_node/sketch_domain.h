#pragma once
#include <stdint.h>
// The sketch path's domain arithmetic, pure and host-compilable, so tests/test_firmware_sketch_domain.py
// can EXECUTE it rather than pattern-match the .ino. night_node.ino must not open-code any of it.
//
// Argument names carry the domain and the kind: _dec/_acq is the rate, _at is an INSTANT (the FIR
// group delay already subtracted, so it names when a sound reached the microphone), _pos is a
// BUFFER POSITION (a count of samples written -- no group delay ever belongs on one), _len is a
// length. An instant and a position are interchangeable ONLY against a buffer that stores the
// undelayed acquisition stream, which aring does and g_acq counts; that is why the delay comes off
// the detection index and must never come off the write head.

// How far before the onset the window starts, from TIME. hear/node/detect.py's SKETCH_BACK_S and
// hear/sketch.py's HOP_S are two independent reference constants that both happen to be 0.004 s;
// taking the hop instead read as 4.000 ms at 16 kHz and 1.333 ms at 48 kHz off the same line.
static inline uint32_t sk_back_acq_len(double back_s, double fs_acq) {
  uint32_t n = (uint32_t)(back_s * fs_acq);
  return n ? n : 1u;
}

// The onset instant of a detection at decimated offset i_dec in a block whose first acquisition
// sample is acq_base_pos. decimate() forms output j from inputs up to j*decim + (decim-1), and the
// FIR is linear phase, so the sound reached the mic decim_delay_len acquisition samples before
// that newest input. SATURATES AT 0 -- before the first decim_delay_len samples of a boot the
// unsigned subtraction would wrap to ~4e9 and every caller compares the result against a bound.
static inline uint32_t sk_onset_acq_at(uint32_t acq_base_pos, uint32_t i_dec,
                                       uint32_t decim, uint32_t decim_delay_len) {
  uint64_t newest = (uint64_t)acq_base_pos + (uint64_t)i_dec * decim + (decim - 1u);
  return newest > (uint64_t)decim_delay_len ? (uint32_t)(newest - decim_delay_len) : 0u;
}

// An instant minus a length is an instant. Saturates; the caller flags a saturated start, because
// the samples in front of position 0 are zeros and a sketch of zeros is a legitimate-looking frame.
static inline uint32_t sk_window_start_acq_at(uint32_t onset_acq_at, uint32_t back_acq_len) {
  return onset_acq_at > back_acq_len ? onset_acq_at - back_acq_len : 0u;
}

// Has the whole window landed? head_acq_pos is a WRITE HEAD, not an instant.
static inline int sk_window_landed(uint32_t head_acq_pos, uint32_t start_acq_at,
                                   uint32_t span_acq_len) {
  return (int32_t)(head_acq_pos - (start_acq_at + span_acq_len)) >= 0;
}

// Is any part of the window outside what the ring still holds? Tests BOTH ends: testing only the
// start was safe only because the readiness test guaranteed the tail, an invariant that lived in a
// different function with nothing linking the two.
static inline int sk_window_lapped(uint32_t head_acq_pos, uint32_t start_acq_at,
                                   uint32_t span_acq_len, uint32_t ring_acq_len) {
  if ((uint32_t)(head_acq_pos - start_acq_at) > ring_acq_len) return 1;
  return !sk_window_landed(head_acq_pos, start_acq_at, span_acq_len);
}
