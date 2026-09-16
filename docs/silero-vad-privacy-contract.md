# Silero VAD v5 and the zero-audio speech purge contract

**Status: design. Nothing here is built and no model file exists in this repository.** This
document is the contract a `hear-vad` implementation has to satisfy before it is allowed to run
against `clips/` on the pool. Every number is either a published property of the upstream model,
a value already measured elsewhere in this tree with its source named, or a **PROPOSED DEFAULT**
that has to be re-set from a measured distribution before the lane is armed.

The lane exists for one purpose: **no human speech survives on disk.** Not as a clip, not as a
copy, not as an embedding, not as a spectrogram. Where this document and a pipeline that stores
audio disagree, this document wins, because everything else in the pipeline is recoverable and a
recorded conversation is not.

Related: `docs/data-governance.md` §2 (privacy zones and edge redaction) states the policy this
implements; `docs/clip-pipeline.md` describes the clip lane whose files are purged;
`docs/acoustic-stack.md` §0.3–0.4 and `hear/resample.py` own the rate path this consumes.

---

## ⚠️WHAT THIS CONTRACT DOES NOT ESTABLISH

**0. THIS DOCUMENT ALREADY SHIPPED ONE SILENT FALSE-NEGATIVE DEFECT, AND IT IS KEPT ON THE
PAGE.** PR #254 stated the model input as 512 samples. It is **576** — a 64-sample context tail
prefixed to the 512-sample hop — and the wrong version does not fail: it returns ~0.001 for
unambiguous speech, so a lane built on it would have written confident `NO_SPEECH` receipts over
every conversation it was handed. Measured 2026-09-15 against the real artifact; corrected in
§2.3, with the measured evidence in §2.5 and a mandatory startup canary in §2.4. The falsified
version is recorded rather than quietly removed, because the reasoning that produced it —
"32 ms at 16 kHz is 512 samples, therefore that is the input" — is correct arithmetic about the
hop and will be produced again by the next reader who does not check the graph.

**1. A VAD score is not consent, and a purge is not a legal basis.** Purging speech after the
fact does not make capture lawful. `docs/data-governance.md` §1 still requires a documented
basis, notice method and accountable owner *before* recording where people may be heard. This
lane reduces harm from incidental capture; it does not authorise deliberate capture.

**2. Silero VAD answers "is this speech?", never "who is speaking?" and never "what was said?".**
It emits one probability per 32 ms frame. It carries no speaker identity, no transcript, no
language. Nothing downstream of it may be presented as identification of a person.

**3. This model will miss speech.** Distant, wind-masked, reverberant or heavily band-limited
speech at the levels this fleet records (clips measured inaudible without gain —
`docs/clip-pipeline.md` §6) is exactly the regime where a VAD is weakest, and no per-clip miss
rate has been measured on this fleet's audio. **A clip that survives the purge is not a clip
proven free of speech.** It is a clip the model did not flag. Retention policy must stay
conservative on that basis; a green VAD verdict is not a licence to extend retention.

**4. A purge is a delete on the storage this deployment actually has, and that storage is not a
secure-erase device.** §7 states exactly what is and is not guaranteed. "Cryptographic wipe" is
only truthful on the crypto-erase path in §7.3; on the plain-PVC path the honest claim is
"unlinked and unreadable through the filesystem", not "irrecoverable from the medium".

**5. No threshold here has been validated against this fleet's clips.** The defaults are the
upstream reference values. The listening set at `testdata/clip-labels-2026-09-10.jsonl` was
scored for scene content, not for speech presence, so it does not close this gap. §10 is the
gate.

---

## 1. Model card — Silero VAD v5

| Field | Value |
|---|---|
| Model | Silero VAD, v5 generation |
| Task | Frame-level voice activity detection (speech / not-speech probability) |
| Upstream | `snakers4/silero-vad` |
| Licence | **MIT** — permissive, redistribution and commercial use allowed with attribution |
| Artifact | `silero_vad.onnx`, single file, **~2 MB** weights |
| Runtime | ONNX Runtime, CPU execution provider, single-threaded |
| Input rates | 16 000 Hz and 8 000 Hz **only** |
| Hop (advance) | **512 samples @ 16 kHz (32 ms)**; 256 @ 8 kHz |
| **Model input window** | **576 samples @ 16 kHz** = 64-sample context + 512-sample hop (§2.3, measured); 288 @ 8 kHz, unverified |
| Graph IO (measured) | in: `input` `[B, N]` f32, `state` `[2, B, 128]` f32, `sr` int64 scalar · out: `output` `[B, 1]`, `stateN` |
| Output | One `float32` speech probability in `[0, 1]` per frame, plus updated recurrent state |
| Latency | **~0.5–1.0 ms per frame** on one CPU core (upstream claim; **UNMEASURED here**) |
| Streaming | Stateful — recurrent `state` **and** a 64-sample context tail carried hop to hop (§3.2) |
| Training data | Not redistributed; multi-language corpus described upstream |

**Why this model and not the tagger already in the tree.** `tools/hear_tag.py` maps AudioSet
classes and its `speech` group (`hear_tag.py:247`) covers `Speech`, `Male speech, man speaking`,
`Conversation` and friends — but `docs/clip-calibration-2026-09-10.md` §2 measured that
**`Speech` is that model's null response**: 21 of 69 clips return it top-1, on insect, dog and
nothing. A null response cannot drive a destructive action. A dedicated VAD with a calibrated
probability can. The tagger's speech group stays what it is — an observation, never a gate.

**Licence is load-bearing.** BirdNET was refused in this project partly for CC BY-NC-SA weights
(`docs/clip-pipeline.md` §3 of the preamble). MIT is why Silero is admissible where BirdNET was
not: it can ship inside a self-hosted image and be redistributed with it.

**Attribution obligation.** The MIT notice ships with the artifact. A model file that reaches the
pool without its licence text alongside it is a provenance defect and the lane refuses to load
it (§9, `vad_model_unverified`).

---

## 2. Input contract

### 2.1 The two source streams

| Source | Rate | Where it comes from | Path to model input |
|---|---|---|---|
| Native acoustic stream | 48 000 Hz, 16-bit mono | node microphone; the 5.0 s / 480,044 B clip WAV of `docs/clip-pipeline.md` §1 | decimate 48 k → 16 k, **L=1 M=3** |
| Decimated stream | 16 000 Hz | already-decimated node feed (`FS_NOMINAL_HZ = 16000`, `hear/tags.py:49`) | consumed directly |

`hear/resample.py` already owns this: `snap()` refuses any header rate outside **0.5 %** of
48 000 Hz rather than stretching it (`SNAP_TOLERANCE`), and the anti-alias filter is Kaiser with
**72 dB** stopband, transition width 10 % of the passband edge — the fold is put under the int16
floor of the source. 48 k → 16 k is an integer decimation by 3, so every band the VAD reads is
measurement, not interpolation.

⚠️**A rate that is not 48 kHz or 16 kHz is refused, never resampled to fit.** Same rule as the
tagger's. A clip whose `wav_header_fs_hz` disagrees with its `fs_hz` (both are stored side by
side in `clips/index.jsonl` precisely so the disagreement is visible) is **not** scored; it is
routed to the fail-closed branch in §9.

### 2.2 The 8 kHz path

8 kHz is supported by the model and is documented here for completeness, but **it is not the
deployment path.** Decimating to 8 kHz discards the 4–8 kHz band, which carries fricative energy
that separates speech from wind and insect noise. Use 8 kHz only where an upstream feed is
natively 8 kHz. Never downsample 16 kHz to 8 kHz to save time: the cost saved is ~0.5 ms/frame
and the cost paid is sensitivity in the band that matters, on a detector whose misses are silent.

### 2.3 Frame slicing — hop 512, **window 576**

⚠️⚠️**THE MODEL INPUT IS 576 SAMPLES AT 16 kHz, NOT 512, AND FEEDING IT 512 FAILS SILENTLY
TOWARDS RETENTION.** This document said 512 when it was first merged (PR #254). That was wrong,
and it was wrong in the most dangerous available direction. Measured against the real artifact on
**2026-09-15, onnxruntime 1.27.0** (see §2.5): a bare 512-sample call returns **~0.001 for
everything, including unambiguous speech** — mean 0.001 / max 0.002 on a synthetic speech signal
that scores mean 0.747 / max 0.998 when called correctly. No exception, no warning, no shape
error. A lane built on the 512 reading would have written `NO_SPEECH` receipts over every
conversation it was handed and reported a healthy run while doing it.

The 512 figure is the **hop**. The model's input is that hop prefixed with a **64-sample context
tail carried from the previous hop**:

```
window_t = concat(context_t, hop_t)            # 64 + 512 = 576 samples
context_{t+1} = hop_t[-64:]                    # the last 64 samples of the hop just fed
context_0 = zeros(64, float32)                 # start of every clip
```

| Constant | 16 kHz (the deployment path) | 8 kHz |
|---|---|---|
| `CHUNK_SAMPLES` (hop) | **512** (32 ms) | 256 (32 ms) |
| `CONTEXT_SAMPLES` | **64** (4 ms) | 32 (4 ms) — **UNVERIFIED against the artifact** |
| `WINDOW_SAMPLES` (model input) | **576** | 288 — **UNVERIFIED** |
| advance per call | `CHUNK_SAMPLES` | `CHUNK_SAMPLES` |
| dtype | `float32` in `[-1, 1]`, from int16 by `/ 32768.0` | same |
| tail handling | **discard** a partial final hop; never zero-pad | same |

```
frames_per_clip = floor(n_samples / CHUNK_SAMPLES)
5.0 s clip @ 16 kHz = 80,000 samples -> 156 frames, 64 samples discarded
```

Frame count and `tail_samples_dropped` are unchanged by the correction: the advance is still the
hop. Only the tensor handed to the model changed, and the context tail is now part of the stream
state (§3).

⚠️**The 8 kHz row is arithmetic, not measurement.** 32 and 288 follow the 16 kHz ratio and have
not been run against the artifact here. 8 kHz is not the deployment path (§2.2); anyone enabling
it measures those two numbers first and replaces this note with the result.

⚠️**The partial tail is discarded, not zero-padded.** A zero-padded frame is a frame the model
scores as near-silence by construction, and at the end of a clip that is exactly where a
truncated word sits. Discarding ≤ 31 ms is honest; padding invents a low score. The count of
discarded samples is recorded on the receipt (`tail_samples_dropped`) so it is a number rather
than a silence.

⚠️**Neither the hop nor the window is a tunable**, and the loader may not trust either to a
constant in this document. At startup it reads the model's **declared input dimension** and
asserts every call's length against it; a length mismatch is `vad_window_contract_violated`
(§9), which fails closed to purge. A constant that silently disagrees with the graph is exactly
the defect this section was written to correct.

### 2.4 The window canary — a startup check, not a comment

Because the failure is silent, the contract requires a **positive** check that the window is
being fed correctly, run once per process before any clip is judged:

1. Score a deterministic, in-process synthetic speech-like signal (formant trajectories with a
   ~4 Hz syllable envelope) through the normal streaming path.
2. Assert `max(probs) >= 0.5`.
3. Score the same signal through a deliberate bare-hop call (no context).
4. Assert that path's `max(probs) < 0.1` — i.e. the degenerate mode is reproducible and the
   normal path is demonstrably not in it.

Failing either assertion is `vad_window_contract_violated`: the lane stops, and any clip in
flight is purged. The canary signal is generated in code, contains no recording, and is not a
model-accuracy test — it is a wiring test, and it is the only thing standing between a silent
graph-shape regression and a receipt file full of false `NO_SPEECH`.

### 2.5 Measured reference probabilities

Measured by the `silero-vad-tests` lane on **2026-09-15**, real Silero VAD v5 ONNX, onnxruntime
**1.27.0**, 2 s synthetic signals at 16 kHz, 512-sample hops with the 64-sample context, stateful
streaming, 62 chunks. Graph IO as measured: inputs `input` `[B, N]` float32, `state`
`[2, B, 128]` float32, `sr` int64 scalar; outputs `output` `[B, 1]`, `stateN`.

| Signal | mean | max | `frac > 0.5` |
|---|---|---|---|
| synthetic speech (formants + fricatives + plosives + ~4 Hz envelope) | 0.75 | **0.998** | 0.82 |
| the same speech, state **and context** reset before every chunk | 0.25 | — | 0.21 |
| the same speech, bare 512-sample call (no context) | **0.001** | 0.002 | 0.00 |
| speech upsampled to 48 kHz then decimated back to 16 kHz | 0.766 | — | — |
| speech clipped to ±1 / with +0.3 DC / with pink noise added | ≥ 0.71 | — | — |
| silence | — | 0.009 | 0.00 |
| white noise, quiet and loud | — | 0.041 | 0.00 |
| pink noise | — | 0.028 | 0.00 |
| bird chirp up-sweeps, 3.5 → 7 kHz | — | 0.012 | 0.00 |
| 1 kHz pure tone | — | 0.006 | 0.00 |
| **static (non-time-varying) formant drone** | — | **0.863** | — |

Four things this table settles, and one it does not:

- **The 48 k → 16 k decimation is transparent to the detector.** 0.766 against 0.75 native. The
  rate path of §2.1 costs nothing measurable here.
- **A dropped or per-chunk-reset state costs most of the detector**, 0.75 → 0.25 mean and 0.82 →
  0.21 above threshold. §3's reset discipline is a correctness requirement, not hygiene.
- **The non-speech signals this fleet actually records sit far below any usable threshold** —
  bird sweeps at 0.012, pink noise at 0.028, tone at 0.006, against a 0.50 gate. The purge is not
  going to fire on a dawn chorus.
- **Robustness to clipping, DC offset and additive pink noise is real** (≥ 0.71).
- ⚠️**A static formant drone reaches 0.863 and would be purged.** Do not claim this model
  rejects non-speech harmonic structure; it does not, and a resonant machine, a tonal alarm or a
  wind-excited cavity can plausibly cross the gate. That is a **false positive, which destroys a
  clip of a machine** — the tolerable direction — but it must be stated rather than discovered,
  and it is why §10 gate 2 requires a dry run with a measured base rate before arming.

⚠️**These are synthetic signals, not this fleet's audio.** They establish that the wiring is
correct and that the obvious negatives score low. They do **not** establish a miss rate on
distant, wind-masked, reverberant speech at the levels these nodes record (preamble item 3), and
they do not replace §10 gate 3.

---

## 3. State tensor lifecycle

Silero VAD is recurrent. Its per-stream memory is the contract's sharpest edge, because carrying
it wrong is not an error — it is a wrong answer that looks fine.

### 3.1 Shapes

| Generation | Inputs | State shape | Note |
|---|---|---|---|
| v4 | `input`, `sr`, `h`, `c` | `h`, `c` each `(2, batch, 64)` `float32` | LSTM hidden/cell pair, carried separately |
| **v5 (this contract)** | `input`, `sr`, `state` | `state` `(2, batch, 128)` `float32` | the `h`/`c` pair fused into one tensor; output `stateN` feeds the next call |

Write the loader against the **declared signature of the loaded file**, not against a hardcoded
assumption of which generation it is. If the model's inputs are `{input, sr, state}`, run the v5
path; if they are `{input, sr, h, c}`, run the v4 path; anything else is refused
(`vad_model_unverified`). `sr` is an `int64` scalar, 16000 or 8000, and must match the actual
sample rate of the frames being fed — the model does not check it for you.

### 3.2 Stream state is **two** objects, not one

⚠️**The 64-sample context tail of §2.3 is stream state and is governed by every rule below.** It
is not a buffering detail. Resetting the recurrent `state` while carrying a stale context, or the
reverse, is a partial reset — and a partial reset is measured to cost most of the detector:
resetting state *and* context before every chunk drops synthetic speech from mean **0.75** to
**0.25**, and from 82 % of frames above threshold to 21 % (§2.5). `reset()` clears **both**:
`state = zeros((2, 1, 128), float32)` and `context = zeros(64, float32)`.

1. **Zero-initialise both at the start of every clip.** State `(2, 1, 128)`, context `64`.
2. **Carry both forward, in order.** The output `stateN` becomes the next call's `state`; the
   last 64 samples of the hop just fed become the next call's context. Frame order is sample
   order; frames are never scored out of order or in parallel within one clip.
3. **Reset between clips, unconditionally.** Two clips are not one stream: they may be from
   different nodes, different boots, hours apart (`docs/clip-pipeline.md` §3 — `sample` restarts
   at 0 on every boot). Carrying either object across a clip boundary leaks the tail of one
   clip's audio into the head of another's decision — and the context tail leaks it as literal
   samples of the previous clip.
4. **Reset both on any exception**, and mark the clip fail-closed (§9). Never resume a stream
   from a state produced by a failed call.
5. **One state and one context per stream. Batch size is 1.** If a future implementation batches,
   each row owns its own state slice and its own context, and rows are never reordered inside a
   batch.
6. **Neither is persisted, logged, or exported.** Both are functions of the audio and inherit its
   classification (`raw_audio`, `docs/data-governance.md` §1) — the context tail is *literally 4
   ms of PCM* and must never reach a receipt, a log line or a crash dump. They live in process
   memory, are overwritten at the next clip, and are dropped when the process exits.

⚠️**A stale state is the failure mode with no symptom.** A forgotten reset changes probabilities
by a little, not by a lot; nothing throws, nothing logs, and the per-clip verdict drifts. The
implementation therefore asserts, at frame index 0 of every clip, that **both** `state.sum() == 0`
and `context.sum() == 0`, and records `state_reset_confirmed: true` on the receipt. An assertion
that runs on every clip is cheaper than a distribution shift nobody can date.

---

## 4. Thresholding and decision policy

### 4.1 Parameters

| Parameter | **PROPOSED DEFAULT** | Meaning |
|---|---|---|
| `speech_threshold` | **0.50** | probability at or above which a frame opens a speech segment |
| `neg_threshold` | **0.35** | probability strictly below which a frame may close one |
| `min_speech_duration_ms` | **250** | a segment shorter than this is discarded as a blip |
| `min_silence_duration_ms` | **300** | silence shorter than this does **not** close a segment; it is bridged |
| `speech_pad_ms` | **30** | margin added to each end of an accepted segment |
| `purge_threshold` | **= `speech_threshold`** | see §4.4 — deliberately not a separate, looser knob |

These are the upstream reference values. They are **PROPOSED**, not measured on this fleet (§10
gate 3). Every one of them is recorded on the receipt of every clip it judged, so a later change
is dateable and a past verdict is reproducible.

### 4.2 Hysteresis, and why two thresholds

A single threshold at 0.5 chatters: a frame sequence of `0.52, 0.48, 0.53` becomes three
segments and two silences. Dual thresholds separate the opening decision from the closing one —
**open at ≥ 0.50, close only below 0.35** — so a segment survives ordinary probability wobble
and only ends when the model is affirmatively confident the speech stopped. The gap between the
two is the noise immunity; narrowing it to zero reintroduces the chatter.

### 4.3 The state machine

```
state := SILENCE ; seg_start := none ; silence_run := 0

for i, p in enumerate(frame_probs):              # 32 ms per frame
    t := i * FRAME_MS
    if state is SILENCE:
        if p >= speech_threshold:
            state := SPEECH ; seg_start := t ; silence_run := 0
    else:                                        # state is SPEECH
        if p < neg_threshold:
            silence_run += FRAME_MS
            if silence_run >= min_silence_duration_ms:
                close_segment(seg_start, t - silence_run)   # bridged silence not included
                state := SILENCE ; silence_run := 0
        else:
            silence_run := 0                     # short gap bridged, segment continues

if state is SPEECH:
    close_segment(seg_start, len(frame_probs) * FRAME_MS)   # clip ended mid-speech

# acceptance, applied at close_segment:
#   duration < min_speech_duration_ms          -> discard as blip
#   otherwise                                   -> accept, then pad both ends by speech_pad_ms,
#                                                  clamped to [0, clip_duration]
```

Padding is applied **after** the duration test, never before: padding a 120 ms blip to 180 ms and
then testing it against 250 ms would be testing the pad, not the speech. Padding exists so that a
retained (non-purged) neighbourhood does not begin one frame after a word ends; it is a margin of
safety on the destructive side, so it is never negative and never zero-configured to squeeze more
audio through.

⚠️**A clip ending mid-speech is closed at the clip boundary and flagged `truncated: true`**, and
`min_speech_duration_ms` is **not** applied to a truncated segment. A 150 ms fragment at the tail
of a 5.0 s clip is very likely the head of a sentence that continued into the clip the node
recorded next. Discarding it as a blip would be the one case where the blip filter destroys the
evidence that the purge was needed.

### 4.4 Why `purge_threshold` is not a looser separate knob

The tempting design is a high `speech_threshold` for segmentation and a lower `purge_threshold`
so that marginal speech still triggers destruction. It is rejected as written, for a reason the
policy already states: `docs/data-governance.md` §2 — *"operators must treat uncertain redaction
as unredacted data and apply the stricter retention and access rules."* Two knobs invite the
opposite drift, where the purge knob is raised to keep more audio. The single knob, plus the
fail-closed branches of §9, keeps the bias pointed at destruction. An operator who wants a
stricter purge lowers `speech_threshold`; there is no knob that makes the purge lazier than
segmentation.

---

## 5. Clip verdicts

Exactly three, and every scored clip lands on one of them.

| Verdict | Condition | Action |
|---|---|---|
| `SPEECH_DETECTED` | ≥ 1 accepted segment (§4.3), or any fail-closed condition of §9 | **purge** (§7), receipt, tags |
| `NO_SPEECH` | scored end to end, zero accepted segments | audio retained under the ordinary clip cap; receipt written |
| `NOT_SCORED` | the clip's audio is already gone (`audio_pruned_at` set, or `outcome` not `stored`) | nothing to purge; receipt records the reason |

⚠️**`NO_SPEECH` is "not flagged", not "no speech".** See the preamble, item 3. The verdict string
is deliberately not `CLEAN`.

---

## 6. Zero-audio purge policy

When a clip is `SPEECH_DETECTED`, the ordered obligation is:

1. **Stop propagation first.** No copy of the audio leaves the process: no upload, no
   `.loud.wav` listening copy (`tools/hear_listen.py`), no export, no attachment on an alert. If
   a copy was already created in this run, it is purged in the same transaction as the original.
2. **Purge the raw WAV on disk** — `clips/<day>/<node>/<basename>.wav` and any
   `<basename>.wav.<pid>.tmp` part-file, by the method of §7.
3. **Purge derived audio-equivalent artifacts** of that clip: cached decimations, spectrogram
   PNG/NPY, any embedding vector stored on a tag row (`hear/tags.py` schema 2 carries
   `embedding`). Bounded scalar features that cannot reconstruct audio (level, band energies,
   duration) may be retained.
4. **Write the zero-audio receipt** (§8) — after the delete, never before, and carrying the
   delete's own result.
5. **Append the index/tag fields** of §8.3 so every downstream consumer sees
   `human_speech_purged: true` without having to join the receipt file.
6. **Emit nothing else.** No alert body containing a waveform, no "sample of what was purged",
   no operator preview.

**No audio, and no near-audio, is retained under any role.** There is no evidence-custodian
exception, no reviewer exception and no debugging exception in this lane. A deployment that needs
audio of speech for a stated lawful purpose does not get it by weakening this contract; it
declares that purpose, gets it approved under `docs/data-governance.md` §1, and runs a different,
separately-audited lane.

**Idempotent, and safe to re-run.** Purging a clip whose file is already gone is a success with
`already_absent: true`, not an error. The receipt log is append-only and a second receipt for the
same `clip_key` is legitimate (re-scan, re-drain); consumers take the union, and any receipt with
`SPEECH_DETECTED` is terminal for that `clip_key` — a later `NO_SPEECH` can never un-flag it.

**The clip is never re-fetched.** `hear-drain` must treat a purged `clip_key` as terminal, in the
same way it treats `evicted_before_fetch` (`hear/clips.py`, the never-re-probe set). Without that
rule the next 15-minute drain re-downloads the speech that was just destroyed, and the lane
becomes a loop that transports a conversation across the network every quarter hour.

---

## 7. What "wipe" actually means on this storage

Honesty here matters more than the strong word.

### 7.1 Ordering, on any path

```
fsync(dirfd of the clip's directory)   # the entry is known-durable before we act
purge the bytes                        # 7.2 or 7.3
os.unlink(path)
fsync(dirfd)                           # the unlink is durable before the receipt is written
write receipt                          # 8
```

A receipt written before the unlink is durable can survive a crash that leaves the WAV in place —
an audit record asserting a destruction that did not happen. That is the worst single outcome
this lane can produce, worse than an un-purged clip with no receipt, because it is a false
assurance. Hence the ordering, and hence the receipt carries the post-unlink `os.path.exists`
result as `verified_absent`.

### 7.2 Plain PVC path — overwrite then unlink

Open `r+b`, write `os.urandom(n)` over the full length, `flush`, `os.fsync(fileno)`, truncate to
zero, `fsync`, close, `unlink`, `fsync(dirfd)`.

⚠️**On the storage this project actually has, a single-pass overwrite is not a proven
destruction of the medium.** `docs/pool-backup-restore.md` records the measured fact that the
node's ext4 root is a VHDX on a Windows disk; the pool is a PVC on that stack, and flash
translation layers, copy-on-write layers and VHDX block allocation can all leave the old blocks
readable at a level the filesystem cannot address. The truthful claim for this path is
**"unlinked, truncated, and unreadable through the filesystem"**. The receipt says exactly that
in `purge_method: "overwrite_unlink"` and `medium_guarantee: "filesystem_only"`. Do not write
"cryptographically wiped" on this path.

### 7.3 Crypto-erase path — the only one that earns the word

Where clips are written through the Phase 3 object-encryption contract
(`docs/phase3-object-encryption-contract.md`), each object has a per-object data key. The purge is
then: **destroy the per-object key, then unlink the ciphertext.** The ciphertext that may survive
on the medium is unreadable without the key, so the destruction claim does not depend on the
medium honouring an overwrite. Receipt: `purge_method: "crypto_erase"`,
`medium_guarantee: "key_destroyed"`, plus the key identifier that was destroyed (the identifier —
never the key).

**Crypto-erase is the target path.** The plain path exists because the encryption contract is not
yet in force everywhere, and a lane that refuses to run until it is would leave speech on disk in
the meantime. The receipt makes which path was taken a fact on the record rather than an
assumption.

### 7.4 Backups and replicas

⚠️**A purge that does not reach the backups is a partial purge, and the receipt must say so.**
`docs/pool-backup-restore.md` describes generational encrypted archives to a second disk. A
speech-bearing clip captured into an archive before it was purged still exists inside that
archive. Two obligations:

- The receipt carries `backup_generations_possibly_affected` — the generation identifiers whose
  window covers the clip's `fetched_at` — or `[]` when the clip never lived across a backup run.
- Restoring any archive listed on any `SPEECH_DETECTED` receipt requires replaying the purge over
  the restored tree **before** the restored pool is readable by any role. That replay is exactly
  why the receipt keeps `clip_key`, path and body `sha256`: they are enough to find and destroy
  the file again, and they are not audio.

---

## 8. The zero-audio audit receipt

### 8.1 Rules

- **`hear.vad.purge.receipt.v1`**, one JSON object per line, append-only, at
  `clips/vad_purge.jsonl` on the pool. Never pruned; it is the durable record, exactly as
  `index.jsonl` and `tags.jsonl` are (`docs/clip-pipeline.md` §3).
- **It contains no audio and nothing reconstructable into audio.** No samples, no per-frame
  probability array, **no context tail** (§3.2 — the 64-sample context is literally 4 ms of PCM),
  no recurrent state, no embedding, no spectrogram, no transcript, no speaker attribute. Scalars
  and identifiers only.
- **It contains no content description.** "What was said", "how many voices", "language",
  "adult/child" are all out of scope and out of the schema. The receipt proves a destruction; it
  is not a redacted summary of a conversation.
- The body `sha256` of the purged file **is** carried. A hash of destroyed bytes is an identifier
  for the destruction, not a copy of it, and it is already in `clips/index.jsonl` as an integrity
  field (`hear/clips.py:259`) — omitting it here would hide the join, not protect anything.
- `provenance` is the literal string `"model"`, following `hear/tags.py`: this verdict is a
  model's opinion that triggered an irreversible action, and the row says so in a machine-readable
  field.

### 8.2 Fields

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `"hear.vad.purge.receipt.v1"` |
| `receipt_key` | string | `sha256("vadpurge\x1f" + clip_key + "\x1f" + purged_at)[:32]` |
| `clip_key` | string | the clip's identity from `hear/clips.py` — the name, not the bytes |
| `node` | string | node identity (`nyquist`, `mach`, `rankine`, …) |
| `day` | string | UTC day, or the literal `unanchored` |
| `path_purged` | string | pool-relative path; the file no longer exists |
| `purged_sha256` | string\|null | body hash from the index row; `null` if never stored |
| `purged_bytes` | int\|null | size of the destroyed file |
| `purged_at` | float | UTC epoch seconds of the unlink |
| `clip_duration_s` | float | scored duration (5.0 s nominal) |
| `fs_model_hz` | int | 16000 or 8000 — the rate the model actually read |
| `fs_source_hz` | int | 48000 or 16000 — the rate the audio was captured at |
| `frames_scored` | int | e.g. 156 |
| `tail_samples_dropped` | int | partial final hop, §2.3 |
| `window_samples` | int | model input length actually fed — 576 at 16 kHz. Present so a silent window regression (§2.3) is dateable from the receipt file alone |
| `verdict` | string | `SPEECH_DETECTED` / `NO_SPEECH` / `NOT_SCORED` |
| `speech_confidence_max` | float | max frame probability over the clip |
| `speech_confidence_mean_in_segments` | float\|null | mean probability inside accepted segments |
| `speech_total_ms` | int | summed accepted-segment duration, padding included |
| `speech_segment_count` | int | number of accepted segments |
| `speech_segments_ms` | list[[int,int]] | segment start/end offsets in ms, **relative to clip start** |
| `truncated_segment` | bool | a segment ran to the clip boundary (§4.3) |
| `provenance` | string | literal `"model"` |
| `model` | object | `{name: "silero-vad", version: "v5", sha256, runtime: "onnxruntime", runtime_version}` |
| `policy` | object | the six §4.1 parameters as used, plus `policy_version` |
| `state_reset_confirmed` | bool | §3.2 assertion result — **both** `state` and `context` zeroed at frame 0 |
| `window_canary_passed` | bool | §2.4 startup canary result for the process that produced this verdict |
| `purge_method` | string | `overwrite_unlink` / `crypto_erase` / `already_absent` |
| `medium_guarantee` | string | `filesystem_only` / `key_destroyed` / `none` |
| `crypto_key_id` | string\|null | identifier of the destroyed key; **never the key** |
| `verified_absent` | bool | post-unlink existence check |
| `already_absent` | bool | the file was gone before this purge |
| `derived_purged` | list[string] | kinds destroyed, e.g. `["embedding", "spectrogram_cache"]` |
| `backup_generations_possibly_affected` | list[string] | §7.4 |
| `audio_retained` | bool | **always `false`** on a `SPEECH_DETECTED` receipt; a `true` here is a contract violation and a gate failure |
| `fail_closed_reason` | string\|null | §9 code, when the verdict came from a failure rather than a score |

⚠️**`speech_segments_ms` carries offsets, never content**, and it is the one field an
implementer may reasonably argue about. It is kept because the operator question after a purge is
"was this a two-second passer-by or forty seconds of conversation at the fence", and the answer
changes the siting decision. It is bounded by construction: a 5.0 s clip cannot hold more than a
handful of segments, and offsets into destroyed audio reconstruct nothing.

### 8.3 What the rest of the pipeline sees

Appended to the clip's `clips/index.jsonl` row (append-only; `read_index` keeps the last line per
key, exactly as `audio_pruned_at` works today — `hear/clips.py:462`):

```json
{"human_speech_purged": true, "audio_pruned_at": 1789526400.0,
 "vad_receipt_key": "…", "vad_verdict": "SPEECH_DETECTED", "vad_policy_version": "v1"}
```

⚠️`outcome` **stays `stored`.** Same rule as pruning: the row is the durable record that these
bytes came off that node at that time, and rewriting the outcome would make a destroyed clip look
like a clip that never arrived, which is a different and false claim.

Every downstream tag, detection, event or alert derived from that clip carries
**`human_speech_purged: true`** and must not carry any audio-derived payload from it. Consumers
treat the flag as terminal: a record with `human_speech_purged: true` is never a training row
(`hear/tags.py` already refuses model output as a label; this is the stricter case), never an
export candidate, and never attached to an alert with media.

### 8.4 Example

```json
{"schema":"hear.vad.purge.receipt.v1","receipt_key":"b41f…","clip_key":"7c9a…",
 "node":"nyquist","day":"2026-09-15","path_purged":"clips/2026-09-15/nyquist/…​.wav",
 "purged_sha256":"e3b0…","purged_bytes":480044,"purged_at":1789526400.0,
 "clip_duration_s":5.0,"fs_model_hz":16000,"fs_source_hz":48000,"frames_scored":156,
 "tail_samples_dropped":64,"window_samples":576,"verdict":"SPEECH_DETECTED","speech_confidence_max":0.94,
 "speech_confidence_mean_in_segments":0.78,"speech_total_ms":1180,"speech_segment_count":2,
 "speech_segments_ms":[[640,1360],[2880,3340]],"truncated_segment":false,
 "provenance":"model",
 "model":{"name":"silero-vad","version":"v5","sha256":"…","runtime":"onnxruntime",
          "runtime_version":"1.18.0"},
 "policy":{"policy_version":"v1","speech_threshold":0.5,"neg_threshold":0.35,
           "min_speech_duration_ms":250,"min_silence_duration_ms":300,"speech_pad_ms":30},
 "state_reset_confirmed":true,"window_canary_passed":true,"purge_method":"overwrite_unlink",
 "medium_guarantee":"filesystem_only","crypto_key_id":null,"verified_absent":true,
 "already_absent":false,"derived_purged":["embedding"],
 "backup_generations_possibly_affected":[],"audio_retained":false,"fail_closed_reason":null}
```

---

## 9. Failure modes — every one of them fails closed

**Fail closed means purge.** Not "retain and warn". The asymmetry is deliberate: the cost of
destroying a clip of wind is one clip of wind, and the cost of retaining a clip of a conversation
is unbounded and unrecoverable.

| Condition | `fail_closed_reason` | Verdict | Action |
|---|---|---|---|
| ONNX Runtime missing or model file absent | `vad_unavailable` | `SPEECH_DETECTED` | purge, and **raise the lane's health alarm** |
| Model `sha256` not in the pinned allow-list | `vad_model_unverified` | `SPEECH_DETECTED` | purge; refuse further clips until resolved |
| Model signature is neither the v5 nor the v4 shape (§3.1) | `vad_model_unverified` | `SPEECH_DETECTED` | as above |
| Inference raised, or returned NaN/out-of-range | `vad_inference_error` | `SPEECH_DETECTED` | purge; reset state |
| Header rate not 48 kHz / 16 kHz, or `fs_hz` ≠ `wav_header_fs_hz` | `rate_refused` | `SPEECH_DETECTED` | purge; the clip cannot be honestly scored |
| WAV truncated, not RIFF, or shorter than one frame | `clip_unreadable` | `SPEECH_DETECTED` | purge |
| Per-clip deadline exceeded mid-scan | `vad_timeout` | `SPEECH_DETECTED` | purge the clip it was scanning |
| `state_reset_confirmed` assertion failed (state **or** context non-zero at frame 0) | `state_contract_violated` | `SPEECH_DETECTED` | purge; **stop the lane**, the whole run's scores are suspect |
| Call length ≠ the model's declared input dimension (e.g. 512 fed to a 576 graph) | `vad_window_contract_violated` | `SPEECH_DETECTED` | purge; **stop the lane** — see §2.3, this mode returns ~0.001 for speech |
| §2.4 startup window canary failed | `vad_window_contract_violated` | `SPEECH_DETECTED` | refuse to judge any clip; nothing scored in this process is trustworthy |
| Purge itself failed (`OSError`) | `purge_failed` | `SPEECH_DETECTED` | retry once, then alarm and **stop the lane**; do not continue scanning while a known speech-bearing file is on disk |
| Audio already gone | `null` | `NOT_SCORED` | receipt only |

⚠️**`vad_unavailable` purging every clip is the correct behaviour and it will look like a
catastrophe.** A missing `.onnx` destroys the whole backlog. That is the design: the lane is armed
only after §10's gates, its health is a first-class alarm, and the model artifact is a hard
dependency of the job — not an optional enhancement that degrades quietly to "keep the audio".
The alternative, degrading to retention, means a deleted file silently turns the privacy control
off, which is the one failure this document exists to prevent.

---

## 10. Gates before this lane is armed

Written here so they can be checked off in public, in the style of the tagger's unsuspend gate
(`docs/clip-pipeline.md` §6). None is met.

1. ❌ **The artifact exists and is pinned.** `silero_vad.onnx` on the pool, `sha256` in a
   checked-in allow-list, MIT licence text beside it, ONNX Runtime version pinned in
   `requirements/`.
2. ❌ **Dry-run mode ships first and runs for ≥ 7 days.** `--dry-run` scores and writes receipts
   with `purge_method: "dry_run"` and destroys nothing. Arming without a measured base rate means
   nobody knows whether the first armed run deletes 3 clips or 300.
3. ❌ **The thresholds are set from a measured distribution, not from this page.** A human listens
   to a node-balanced sample (`tools/hear_listen.py` already stages this), records speech
   presence per clip, and the result is written into a dated `docs/clip-calibration-<date>.md`
   with the operating point and its measured miss rate on that set. The 2026-09-10 set does not
   count: it was labelled for scene content, not speech presence.
4. ❌ **The purge is proven on a scratch tree.** A test destroys a fixture WAV, asserts
   `verified_absent`, asserts the receipt carries no sample data, asserts the index row keeps
   `outcome: "stored"` and gains `human_speech_purged`, and asserts re-running is a no-op with
   `already_absent: true`.
5. ❌ **Every fail-closed branch of §9 is exercised by a test**, including `vad_unavailable`
   purging rather than retaining. A fail-closed policy nobody has fired is a hope.
6. ❌ **The re-fetch loop is closed.** `hear-drain` treats a purged `clip_key` as terminal (§6),
   with a test.
7. ❌ **The backup obligation is written into the restore procedure** (§7.4) in
   `docs/pool-backup-restore.md`, with the replay step ordered before the restored pool is
   readable.
8. ❌ **The health alarm exists and has been seen to fire**, so `vad_unavailable` is loud on the
   first clip rather than discovered in a receipt file a week later.
9. ❌ **The §2.4 window canary ships, runs at startup, and has been seen to fail.** The 576-vs-512
   defect (§2.3) produced no error and no log line; it produced quiet, confident `NO_SPEECH`. A
   lane without a positive wiring check is one refactor away from that state again, and the only
   evidence would be a receipt file that looks healthy.
10. ❌ **The 8 kHz context and window lengths are measured**, or the 8 kHz path is refused at
   startup rather than run on arithmetic (§2.3).

---

## 11. Where this runs

| Placement | Status | Note |
|---|---|---|
| Pool-side job over `clips/` (`hear-vad`) | **the design target** | Touches the PVC and never a node, exactly like `hear-tag`. Purges before the tagger or any export reads the audio; ordering with `hear-tag` is a hard dependency, not a preference. |
| Drain-side, inside `hear_drain.py` before the first durable write | future | Strictly better — speech never lands on the pool at all — and strictly harder: the drain is on a 15-minute CronJob with `activeDeadlineSeconds: 840` and runs already take 218–307 s (`docs/clip-pipeline.md` §4). 156 frames × ~1 ms ≈ 0.16 s per clip against a 96-clip-per-node budget is ~46 s of added work, which is affordable on paper and **UNMEASURED**. |
| On-node, before the WAV is written to SD | not possible today | ~2 MB of weights plus an ONNX runtime does not fit the ESP32-S3 node budget (`docs/node-hardware.md`, `docs/faketec-pin-budget.md`). Stating it as future work would be stating a wish. |

⚠️**Until this ships, the speech control in force is the one already written down**:
`docs/data-governance.md` §2 — zone definitions, excluded zones, directional placement, and
treating uncertain redaction as unredacted. This document does not relax any of it, and nothing
here may be cited as a reason to enable recording in a place where people can be heard.

---

## 12. What actually shipped, and where it disagrees with this contract

⚠️**Part of this lane is no longer a design.** `hear/privacy/purge.py`, `tools/hear_privacy_purge.py`
and `tests/test_privacy_purge.py` (44 tests) landed on main in PR #255, written against an
earlier revision of this page and in places against a different one. **Where the shipped code and
this document disagree, the code is what runs**, and the disagreements are listed here rather
than silently resolved in either direction — a contract that quietly edits itself to match the
code stops being able to say the code is wrong.

`hear/privacy/silero_vad.py` does **not** exist yet. The engine is the open half.

### 12.1 What shipped, and is better than what this page asked for

- **The receipt is key-allow-listed, not merely documented.** `assert_privacy_safe()` refuses any
  key not in `RECEIPT_KEYS`, *and* refuses any value that is a sequence of numbers, so a feature
  vector cannot be smuggled into a permitted field. §8.1 described a policy; the code enforces a
  mechanism, and the module docstring names the real failure mode — a privacy pipeline leaks by
  accretion of one more harmless field, never by a decision to leak.
- **`vad_engine` is on every receipt.** A fleet-wide claim about what was purged is only as
  strong as the weakest detector that produced it, and that is now visible in the record rather
  than inferred from a deployment date. This page did not ask for it. It should have.
- **The overwrite path makes the same refusal this page makes** (§7.2): `zero_audio_retained`
  means *this pipeline retained nothing*, explicitly not *unrecoverable*.
- **`--dry-run` does not append to the audit log at all**, where §8 assumed a `dry_run` receipt
  would be written. The shipped choice is the safer one: a dry run must be safe to point at a
  pool someone else is draining, and writing to a shared JSONL is a side effect. **The code
  wins; §10 gate 2 now means "receipts on stdout/`--json`", not "receipts in the audit log".**

### 12.2 Field-name divergence — the code wins, and the gap is real

Shipped receipt: `purged_receipts.jsonl`, `schema_version: 1`, keys `timestamp, node, clip,
duration_s, peak_speech_prob, speech_s, speech_spans_s, purged_sha256, purge_reason, vad_engine,
zero_audio_retained, dry_run`. §8.2's `hear.vad.purge.receipt.v1` name and field spelling are
**not** what is on disk.

The names are a wash — `peak_speech_prob` is `speech_confidence_max` in a different shirt. What
is missing is not:

| §8.2 field | Status in the shipped receipt | Why it still matters |
|---|---|---|
| `verified_absent`, `already_absent` | absent | §7.1's ordering claim is unprovable from the record, and an idempotent re-run cannot be distinguished from a first purge |
| `purge_method`, `medium_guarantee` | absent | §7.3's crypto-erase path cannot be told apart from §7.2's overwrite path by an auditor reading receipts |
| `clip_key` | `clip` (a path) | a path does not join to `clips/index.jsonl`; §8.3's index update and §6's never-re-fetch rule both need the key |
| `fail_closed_reason` | absent | a purge that happened because the detector was broken reads identically to one that happened because someone spoke |
| `window_samples`, `window_canary_passed` | absent | §2.3's silent-false-negative regression would leave no trace in the record |
| `backup_generations_possibly_affected` | absent | §7.4's restore-replay obligation has nothing to key off |

Adding any of these is a deliberate act with a reviewer attached, which is exactly the property
`RECEIPT_KEYS` was built to have. **Nothing here is a defect in the shipped code** — it is the
gap between a working purge and an auditable one, and it is recorded so the next reviewer sees a
list rather than a feeling.

### 12.3 The disagreement that is not cosmetic: `load_vad("auto")` falls back

§9 says a missing model is `vad_unavailable` and **purges**, loudly. The shipped `load_vad("auto")`
instead falls back to `BandEnergyVAD` — a stdlib+numpy voiced-band/harmonicity detector with no
model file — so a node without onnxruntime still enforces *a* policy rather than destroying its
whole backlog.

That is a third option this page did not consider, and it is arguably the better one: purging
everything on a missing file is correct but operationally violent, and retention was never on the
table. It is accepted **on one condition, which is not yet met**:

⚠️**A fallback detector with an unmeasured recall weakens the privacy control in exactly the way
retention does — quietly.** `vad_engine` makes *which* detector ran visible, but no number
anywhere says what `BandEnergyVAD` misses. Until its recall is measured against the same set that
sets the thresholds (§10 gate 3), a receipt that says `vad_engine: band_energy` is a record of a
destruction, not evidence that the clips left behind are speech-free. Two acceptable resolutions,
and the operator picks one before the lane is armed:

1. Measure `BandEnergyVAD`'s miss rate on the speech-labelled set and publish it, so a
   `band_energy` run carries a known weaker claim; **or**
2. Make `auto` fail closed per §9 in any deployment where the model is *expected* to be present,
   and reserve the fallback for deployments that declare they will never have onnxruntime.

Until one of those lands, **this page treats a `band_energy` receipt as an uncertain redaction**
under `docs/data-governance.md` §2, with the stricter retention and access rules applying to
whatever that run left behind.

### 12.4 Gate status after PR #255

Gate 4 (purge proven on a scratch tree) and most of gate 5 (fail-closed branches exercised) are
**met by `tests/test_privacy_purge.py`**, 44 tests, green on py3.12 and py3.13. Gates 1, 2, 3, 6,
7, 8, 9 and 10 remain open, and gate 5 keeps one hole: the branches this page names
(`vad_window_contract_violated`, `state_contract_violated`) belong to an engine that does not
exist yet.
