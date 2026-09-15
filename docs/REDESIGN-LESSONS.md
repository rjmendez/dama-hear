# Postmortem: dama-hear Acoustic Fleet — Systems-Design Lessons for a Generalized Independent Acoustic Monitoring System

Scope: synthesized from session artifacts only (gold-validation/*, hardware-validation/*,
ageev-discovery/*, gold-audio-evidence-analysis/REPORT.md, fleet-enrollment-rollout/ROLLOUT.md,
48khz-build-matrix-benchmark.md, dama-hear-clean / dama-hear-local / dama-hear-pr67 source trees).
Every claim below is tagged **[EVIDENCE]** (directly observed in an artifact) or **[INFERENCE]**
(reasoned conclusion not itself present verbatim in an artifact). No code was changed to produce
this document.

## 1. Observed failure modes (direct evidence)

1. **False-positive health signal from a boolean flag.** [EVIDENCE] `audio_active: true` was
   reported on 34 samples across two early captures where `audio_peak_left/right` sat at the exact
   full-scale rail (2^23 / 2^23−1) on 100% of samples — a saturated/mis-decoded channel, not a
   working microphone — yet the existing canary (`verify_gold_canary.py`) passed the audio check
   on this data (REPORT.md §2–§4).
2. **A quantity-only gate cannot distinguish "broken" from "healthy."** [EVIDENCE] The same canary
   passed `gold-mic-stimulus-20260912.jsonl` in full despite 100% rail-pinned peaks, because its
   only audio check was presence of the `audio_active` flag, not a statistical bound on
   peak/RMS/variation (REPORT.md §4).
3. **Two structurally different failures were superficially similar ("still broken").**
   [EVIDENCE] Saturation-at-boot (uptime <22s) and exact-zero-at-stimulus (uptime 595–607s) are
   non-overlapping symptoms, not a continuation of one defect (REPORT.md §3–§4). [INFERENCE]
   Treating "still no audio" as one bug class instead of two would have sent debugging down the
   wrong path (e.g. re-checking gain/format when the real fault was elsewhere in-boot).
4. **Root cause was a pin-mapping transposition, and it was reported wrong twice before being
   confirmed.** [EVIDENCE] BCLK/DIN were swapped (41↔42) across three successive statements: an
   audit recommendation, an "operator correction," and a "post-rework report," each contradicting
   the last (REPORT.md §7–§9). The final resolution (§9) shows the swap plus a missing WS
   connection was the entire root cause — the mic hardware itself was fine.
5. **A confound (PPS stall) sat directly adjacent to the audio failure window and was not
   separated out before attributing failure to the microphone.** [EVIDENCE] A 4s PPS stall
   immediately preceded the failed stimulus window in the 2026-09-12 receipt (hardware-validation
   JSON; REPORT.md §5). [INFERENCE] Any single-variable "audio failed" conclusion drawn from that
   window, without re-testing over a clean PPS baseline, risked misattributing a timing/DMA fault
   to the transducer.
6. **Provenance/traceability gap: the firmware that produced the evidence could not be tied to any
   committed source.** [EVIDENCE] `git grep` for the "gold_validation" sketch across all 358
   commits on every branch returned zero hits; the only integrity anchor was a SHA-256 flash
   backup on a remote host not reachable read-only (REPORT.md §1). This made a provable
   binary/config diff between the saturated and zero captures impossible from available artifacts.
7. **Uncommitted, concurrently-edited working tree made benchmark numbers a moving target.**
   [EVIDENCE] `hear_node.ino` and three board headers changed size across five read timestamps in
   ~25 minutes during the 48kHz build-matrix benchmark; the benchmark explicitly flags its own
   numbers as a snapshot, not a release measurement (48khz-build-matrix-benchmark.md, "Concurrent
   edits observed").
8. **A "same chip family" assumption was already proven false once, and process design had to
   explicitly guard against repeating it.** [EVIDENCE] Ageev (16MB flash / 8MB octal PSRAM) and
   Gold (8MB flash / 2MB quad PSRAM) share the same ESP32-S3 QFN56 chip but are incompatible boot
   images; PSRAM bus mode (quad vs octal) is a compile-time, not runtime-detected, setting, so a
   mismatched binary fails at PSRAM init before anything else runs (ROLLOUT.md §1).
9. **A silent partition-table default nearly consumed available OTA headroom.** [EVIDENCE]
   Gold/Kasami/Ageev's FQBN strings set `FlashSize=` but never `PartitionScheme=`, so arduino-esp32
   silently fell back to a 4MB scheme's 1.25MiB OTA app slot regardless of the board's real 8MB/16MB
   flash — leaving Gold/Kasami at 85% of that slot used (~14% OTA growth budget left) while 4–12MB
   of physical flash sat permanently unaddressed (48khz-build-matrix-benchmark.md, "Flash" +
   "Rollout blockers #1").
10. **An unconditional library include cost ~8% flash and ~7% DRAM on boards that never use the
    feature.** [EVIDENCE] `SD.h`/`Wire.h`/FatFs were compiled into Gold/Kasami/Ageev firmware even
    though their board profile sets `HAS_SD 0`/`HAS_I2C 0`; only runtime calls were guarded, not
    the includes (48khz-build-matrix-benchmark.md, "What changed in-flight").
11. **A sample-rate migration silently tripled a background processing lane's CPU cadence, and
    that cost was left unquantified because no hardware was booted to read the instrumentation
    that already existed for it.** [EVIDENCE] `SCENE_FRAMES`×`MELS_NFFT` unchanged, but each frame
    covers 256/48000s instead of 256/16000s → scene FFT runs 3× more often; `scene_fft_us_last/max`
    counters exist in code but were never read because "no board was booted" was a stated
    constraint of that benchmarking pass (48khz-build-matrix-benchmark.md, "CPU headroom").
12. **No local storage means total data loss on brownout/reboot for the newer board class.**
    [EVIDENCE] Gold/Kasami/Ageev have no SD card; the entire record of raw/detection audio is a
    PSRAM ring (~15s on Gold/Kasami, ~80s on Ageev) that is gone on any reboot/brownout not
    preceded by a successful pull (48khz-build-matrix-benchmark.md, "Storage/network differences").
13. **Staged rollout was repeatedly and correctly blocked by a chain of independent gates, but the
    chain grew organically and its "done" states were prone to being over-read.** [EVIDENCE]
    ROLLOUT.md §5 explicitly warns that "a closed todo is not the same fact as a device that has
    booted it" — `multi-board-ota-safety` closing (a process/compile fact) does not supply Gold's
    live canary, Ageev's live canary, or Kasami's hardware-class scan (three independent
    measurement facts). This distinction had to be stated explicitly because the natural reading of
    a "done" status elsewhere in the tracker would otherwise have been treated as rollout-readiness.
14. **A device that is "assembled" was, at one point, at risk of being treated as "classified."**
    [EVIDENCE] ROLLOUT.md §1 explicitly separates these two gates for Kasami and states that
    conflating them ("Treating it as pre-classified") would repeat the Ageev-class mistake.

## 2. Recurring operational pain (direct evidence)

- Every hardware claim required tracing back to a specific artifact/file/counter because narrative
  claims (an "operator statement," a "post-rework report") were shown to conflict with each other
  and with the eventually-confirmed physical wiring (REPORT.md §7–§9). Plain-language status
  reports were not reliable audit trail on their own.
- Verification logic and the thing it verifies evolved out of sync: the canary script existed
  before the statistical audio checks it needed did (REPORT.md §4→§5, ROLLOUT.md Stage 2's later
  "Audio is judged on statistics, not flags (2026-09-12 hardening)" entry, added *after* the gap
  was found).
- Read-only forensic sessions repeatedly hit a wall at "the log/image that would resolve this
  question lives on a remote host (`mrpink`) not reachable from here" (REPORT.md §4, disposition
  block of hardware-validation JSON).
- Firmware and its build/flash tooling were being edited live while a benchmark tried to measure a
  stable baseline of it (48khz-build-matrix-benchmark.md).

## 3. Incorrect assumptions surfaced and corrected during the session (direct evidence)

| Assumption | Why wrong | Where corrected |
|---|---|---|
| `audio_active == true` means the microphone works | It only reflects the flag-setting logic, which itself had a bug (rail-pinned data reads as "active") | REPORT.md §2–§5 |
| Same silicon family (ESP32-S3) ⇒ same boot image | PSRAM bus mode (quad/octal) is compile-time; mismatched binary fails PSRAM init, not a graceful degrade | ROLLOUT.md §1 |
| BCLK=42/DIN=41 (an intermediate operator statement) | Contradicted by the final confirmed physical mapping (BCLK=41/DIN=42) | REPORT.md §7–§9 |
| "16kHz now, upgrade later" dual-rate staged rollout is an acceptable production pattern | Operator explicitly disallowed it as a *steady-state* production strategy (though endorsed for bring-up/recovery); a device that never completes the second OTA is not acceptable | ROLLOUT.md §6 |
| Assembly of a board implies its hardware class is known | Kasami is assembled but has zero bare-board scan evidence anywhere in session storage; assembly and classification are asserted as independent gates | ROLLOUT.md §1, §3 |
| A closed enforcement/compile todo (`multi-board-ota-safety`) means the fleet is closer to flashable | It closes a process risk, not a physical/measurement fact (no device flashed, no canary run, Kasami still unclassified) | ROLLOUT.md §5 |

## 4. Evidence gaps that blocked full resolution (direct evidence)

- No committed firmware source for the exact "gold_validation" sketch that produced the two early
  captures — only a SHA-256-verified flash backup on a remote, unreachable host (REPORT.md §1).
- No config-change record (I2S driver settings/gain/sample format) between the saturated-capture
  epoch and the zero-capture epoch (REPORT.md §4).
- No reboot marker between the two epochs — uptime continuity could not be confirmed without the
  raw serial log referenced in the receipt but not present in the session (REPORT.md §4).
- Kasami has no bare-board pin/flash/PSRAM scan anywhere in session storage, blocking its hardware
  classification independent of its assembly status (ROLLOUT.md §1, §3).
- The absolute µs cost of the 3× scene-FFT cadence increase cannot be resolved without booting
  hardware; the code has the counters, but per this task's constraints no board was booted
  (48khz-build-matrix-benchmark.md, "CPU headroom").

## 5. Prioritized features / fixes for a generalized independent acoustic monitoring system

**P0 — must exist before any fleet-wide trust decision is made on audio data:**
1. Replace boolean "active" health flags with **statistical acceptance bounds** on every raw
   sensor channel: a clipping/saturation ceiling (e.g. reject sustained samples at/above ~95% of
   full-scale), a noise-floor lower bound (exact-zero or below-self-noise-floor is a fail, not a
   "quiet" pass), and a **variation-over-time check** so a stuck/rail-pinned or dead channel cannot
   register as "healthy" merely because a flag derived from the same broken pipeline says so.
   [Directly modeled on REPORT.md §5's proposed criteria and ROLLOUT.md's later "audio is judged
   on statistics" hardening.]
2. Require an **explicit, timestamped stimulus window** in the same telemetry stream (not a side
   narrative) for any acoustic-response acceptance test, with a defined pass condition (e.g. ≥2×/
   ≥6dB RMS rise over the immediately preceding quiescent baseline, decaying back afterward) and a
   requirement that supporting timing signals (e.g. a PPS/clock reference) be strictly
   monotonic/non-stalled through the entire judged window, so a timing confound cannot be
   misattributed to the sensor.
3. **Provenance binding for validation firmware/binaries**: every artifact that produces
   acceptance evidence must be traceable to a committed source revision (or at minimum a recorded,
   verifiable hash tied to a build manifest) reachable by the same process that consumes the
   evidence — a flash backup on an unreachable host is not sufficient provenance for a
   pass/fail decision.
4. **Independent, non-conflatable gates for physical facts vs. process facts**: hardware
   classification (chip/flash/PSRAM bus mode), assembly status, and pipeline/contract enforcement
   must be tracked and reported as separate booleans that cannot be summarized into one "ready"
   status; a closed build/enforcement task must never be interpretable as "device verified."
5. **Cross-node corroboration requirement** before any single-node detection is accepted as fleet
   truth (≥2 independent nodes plus a corroborating platform), preventing a newly-enrolled or
   partially-validated node from silently producing false positives/negatives.

**P1 — needed for safe fleet operation at scale:**
6. **Compile-time hardware-class binding enforced in the release/build pipeline**, not just
   documentation: partition size, PSRAM bus mode, and flash size must be explicit, class-matched
   build parameters (never left to a tool's silent default) and verified against a manifest/SHA
   before any artifact reaches a device.
7. **Conditional compilation of board-optional peripherals** (storage, I2C, etc.) at the
   `#include` level, not just the call-site level, to avoid consuming flash/DRAM for features a
   given hardware class doesn't have.
8. **On-device durable buffering commensurate with the reboot/brownout risk** for boards without
   local storage — either accept and document the data-loss window explicitly per hardware class,
   or add a minimal persistent store, rather than relying solely on a volatile ring buffer as "the
   entire record."
9. **Runtime cost instrumentation must be read as part of any migration sign-off**, not deferred
   indefinitely — a migration that changes a processing lane's cadence (e.g. sample-rate change
   affecting a downstream FFT lane) should require a live-hardware readout of its own existing
   counters before being marked safe, with an explicit checklist item rather than an open-ended
   "recommend reading X" note.
10. **Single canonical sample-rate/format contract enforced fail-closed** across manifest, build
    flags, on-device status, preflight checks, and device gating — with an explicit disallowance of
    "ship the low-fidelity path now, upgrade later" as a *steady-state* production pattern (while
    still permitting it as a bounded bring-up/recovery technique).

**P2 — process hygiene:**
11. **Freeze/snapshot discipline for benchmarking and validation runs**: any measurement taken
    against a working tree must record whether the tree was stable and concurrently edited, and
    numbers taken during active edits must be explicitly marked non-authoritative.
12. **Single-writer-of-record for contradictory operator/agent statements** (e.g. pin mappings):
    require a single, reconciled, authoritative record (ideally checked against physical continuity
    or connector labels) rather than accepting sequential narrative corrections as ground truth.
13. **Explicit two-gate model for "board is ready"**: assembled ≠ classified ≠ canary-passed ≠
    fleet-corroborated; each must be its own recorded fact with its own evidence file, never
    inferred from another.

## 6. Architectural principles for the generalized system

1. **Evidence over inference, always recorded, always attributable.** Every acceptance decision
   should point to a specific artifact/counter, not a narrative claim; conflicting narrative
   claims are treated as unresolved until reconciled against physical/measurable ground truth.
2. **Independent gates compose; they never collapse into a single "done."** Hardware identity,
   process/build enforcement, live canary verification, and fleet corroboration are four different
   kinds of fact and must never be summarized as one status.
3. **Fail-closed by contract, not by convention.** A single enforced fleet-wide contract (rate,
   format, hardware class) should refuse non-compliant artifacts at every layer it touches
   (manifest, build flag, on-device status, preflight, device gate) rather than relying on any one
   layer catching a mismatch.
4. **Statistical, not boolean, acceptance criteria for sensor health.** Any "active"/"healthy" flag
   must be backed by bounds-checked statistics over the raw signal, derived independently of the
   flag it's meant to validate.
5. **Provenance is part of the evidence, not an afterthought.** Validation firmware/binaries must
   be traceable to source or a verifiable manifest reachable by the same audit process that
   consumes their output.
6. **Corroboration over single-source trust.** No single node's detection is fleet truth; require
   multi-node/multi-modality agreement before elevating a signal to an actioned event.
7. **Explicit, bounded exceptions for bring-up vs. steady-state.** Patterns acceptable for initial
   bring-up/recovery (e.g. a conservative low-rate fallback) must be explicitly distinguished from
   what is allowed as an ongoing production deployment shape.
8. **Design for the honest data-loss/uncertainty window.** Where local durability isn't present,
   document and bound the exposure rather than letting a volatile buffer silently become "the
   entire record" without an explicit risk acceptance.

## 7. Confidence and boundaries of this analysis

This document draws only on artifacts already present in this session's file store; it does not
call the `mrpink` host, does not flash or query live hardware, and does not modify any product
code. Where the underlying evidence itself states a conclusion is unprovable from available
material (e.g. REPORT.md §4's binary/config-diff gap, or the CPU-cadence-cost gap in the
benchmark), this document preserves that as an open item rather than resolving it by assumption.
