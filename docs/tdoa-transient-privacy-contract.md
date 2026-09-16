# Transient TDoA privacy contract

**Status:** implemented library boundary; not yet wired into `hear-drain` or node firmware.

## Purpose

TDoA needs waveform correlation. Classification embeddings, tags, and scene descriptors cannot
recover phase or arrival delay. Human speech therefore may exist only in a bounded volatile
window while correlation and VAD run. It must not be written to the pool merely to make TDoA
possible.

`hear/privacy/transient_tdoa.py` owns that boundary:

1. It copies synchronized mono waveforms into process RAM.
2. It refuses windows longer than 10 seconds, mismatched lengths, non-finite samples, and fewer
   than two receivers.
3. It computes target-minus-reference delays with GCC-PHAT and reports correlation peak and
   peak-width timing sigma.
4. It runs VAD after correlation. A VAD exception is a fail-closed speech decision.
5. It zeroes and releases every owned waveform in a `finally` block, including when correlation
   or validation fails.
6. It emits only `hear.tdoa.transient.v1`: node identifiers, arrival timestamps when a reference
   onset timestamp is supplied, relative delays, correlation quality, timing resolution, optional
   local ENU position, and the collapsed speech/fail-closed flags.

The API deliberately owns copies rather than caller buffers. It guarantees destruction of its
own transient working set without corrupting a capture buffer that another real-time consumer
still owns. The caller remains responsible for dropping its source buffers immediately after the
one-shot call.

## Claims and limits

- `zero_audio_retained: true` means this component returns and writes no audio. It does not prove
  that an upstream camera, microphone driver, node SD cache, swap device, crash dump, or caller
  retained no copy.
- Fractional-lag interpolation can produce a delay value with sub-sample precision. Accuracy is
  not implied by decimal precision. `timing_resolution_s` and `delay_sigma_s` remain in every
  record and are never allowed below the sample-quantisation floor.
- ENU coordinates are derived spatial evidence and may be retained under the spatial-event
  policy. Geographic coordinates, transcripts, speaker identity, voiceprints, embeddings,
  spectra, and waveforms are outside this schema.
- The current integration point is a library seam only. Enabling clip retention before
  `hear-drain` calls this boundary would violate the contract.

## Deployment gate

Do not enable persistent clip capture for TDoA. A production caller must prove that its capture
path passes bytes directly into `TransientTdoaWindow`, disables swap/core dumps for that worker,
does not log arguments or exceptions containing arrays, and drops the caller-owned buffers after
the call. Automated speech purge of already-persisted clips remains a remediation path, not the
normal TDoA path.
