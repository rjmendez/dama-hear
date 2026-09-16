# The 5-Tier Acoustic Architecture

**Status: architecture. One tier is shipped, one is partly shipped, three are design.** This
document is the platform-wide synthesis of the acoustic model stack: what runs where, what each
tier is allowed to claim, what it costs in latency and memory, what licence it carries, and the
gate that has to go green before the next tier is allowed to consume its output.

It is the companion to **`docs/acoustic-stack.md`**, which holds the measurements, the live
failures and the staged delivery plan (S0–S5). Where the two disagree, `acoustic-stack.md` is the
measurement of record and this file is wrong. Nothing here restates a number without saying where
it came from, and every number is one of:

| marker | meaning |
|---|---|
| ✅**MEASURED** | taken from this repo or this fleet, with the file or the command named |
| ⚠️**ASSUMPTION** | a defensible estimate, arithmetic from measured inputs, not itself measured |
| ⛔**UNMEASURED** | nobody has measured it; it is an open number and it blocks something |

**The rule that generated the tiering.** Each tier is a *different representation with a different
refusal*. A tier may hand its output downward only as a decision ("look at these four seconds"),
never as a feature vector for a stage that was not fitted on it. §6.4 of `acoustic-stack.md`
states the consequence of ignoring this: 1024-d YAMNet and 1024-d BirdNET embeddings are mutually
confusable in `tools/hear_bridge.py`, three consumers drop a width mismatch silently, and
`audio_anomaly_score.py` answers `0.0` / "not anomalous", which is worse than dropping because it
looks like an answer.

---

## 1. The tiers at a glance

| tier | name | where it runs | in | out | status |
|---|---|---|---|---|---|
| **0** | Node Edge / Micro-DSP | ESP32-S3 node (`firmware/hear_node`) | PDM/I2S samples @48 kHz | 172 B sketch, 20×4 scene row, 5 s clip, PPS-anchored timestamp | ✅ shipped (STA/LTA + vector FFT + FPGA beamform: design) |
| **1** | Ingest Privacy & Compliance | ingest pod, before the pool writes | raw 5 s WAV | purge verdict + zero-retention receipt | ⛔ design, nothing built |
| **2** | Coarse Event Triage | `hear-tag` pod (CPU) | 32 kHz clip (decimated from 48) | 527-class AudioSet scores + 960-d embedding | ✅ shipped (`tools/hear_tag.py`) |
| **3** | Bioacoustic & Ecological Foundation | `hear-embed` pod (GPU) | 32 kHz 5 s window | 1536-d Perch embedding, species posteriors | ⚠️ gated (S1 gates 1 and 3) |
| **4** | Multi-Sensor Spatial & Replay | `hear-tdoa` / `hear-spatial` (CPU) | arrivals + survey | position, bearing cone, GeoJSON event | ⚠️ partly shipped, blocked on the RTK survey (S4) |

### 1.1 Block diagram — the data plane

```
                             THE NODE (Tier 0)                     ESP32-S3 + PDM mic
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │  PDM mic ──I2S_PDM_DSR_8S, clk = fs*64──▶ 48 kHz PCM ──▶ 60 s retrospective ring  │
 │     │                                         │                    │             │
 │     │                              257-tap int16 FIR, /3           │             │
 │     │                                         ▼                    │             │
 │     │                                    16 kHz mel banks          │             │
 │     │                                    ├── scene 20×4 @1.024 s ──┼──▶ SD        │
 │     │                                    └── sketch 20×8, 172 B ───┼──▶ SD+radio  │
 │     ▼                                                              │             │
 │  amplitude gate (detect.py)  ── fires ──▶ 5.0 s WAV (1 s pre / 4 s post) ──▶/clips│
 │  GPS-PPS: tAcc 24–28 ns ──────────────▶ absolute utc_us on every artefact          │
 │  [DESIGN] STA/LTA onset · esp-dsp vector FFT · iCE40 delay-and-sum beamform        │
 └──────────────────────────┬───────────────────────────────────────────────────────┘
                            │  GET /audio · GET /detections?cursor= · GET /sd
                            ▼
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │  Tier 1  INGEST PRIVACY GATE  [DESIGN]                                            │
 │  Silero VAD v5 (ONNX) ──speech? ──yes──▶ DESTROY the samples, keep the receipt     │
 │                                └──no───▶ pass the clip, stamp the receipt          │
 └──────────────────────────┬───────────────────────────────────────────────────────┘
                            │ clips/index.jsonl  +  clips/<day>/<node>/*.wav
              ┌─────────────┴─────────────┐
              ▼                           ▼
 ┌────────────────────────┐   ┌───────────────────────────────────────────────┐
 │ Tier 2  COARSE TRIAGE  │   │ Tier 3  BIOACOUSTIC FOUNDATION   [GATED]      │
 │ EfficientAT mn10_as    │   │ Perch 2.0  1536-d  Apache-2.0                 │
 │ 527 classes, 960-d     │──▶│ BioLingual (zero-shot text query) [UNVERIFIED]│
 │ MIT, CPU, 48 ms/clip   │   │ species heads (site-fitted, few-shot)         │
 └───────────┬────────────┘   └──────────────────┬────────────────────────────┘
             │                                   │
             │  BirdNET is an ORACLE, never a weight: puc/BirdWeather 4066 labels
             ▼                                   ▼
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │  Tier 4  SPATIAL & REPLAY                                                         │
 │  arrivals ─▶ associate ─▶ TDoA multilateration ─▶ point/shockwave solve ─▶ GeoJSON │
 │  hugbot 38.1 mm array ─▶ bearing cone (≤4501 Hz)                                  │
 │  c(T) = 331.3 + 0.606·T  ─▶ [DESIGN] ray-traced atmospheric propagation correction │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Block diagram — the control plane, and the one direction that is forbidden

```
   Tier 0 ──decision──▶ Tier 1 ──decision──▶ Tier 2 ──decision──▶ Tier 3 ──▶ Tier 4
      ▲                                          │                   │
      └──── pull trigger (GET /audio, budgeted) ─┘                   │
                                                                     │
   ✗ FORBIDDEN: Tier 2's 960-d embedding into a Tier 3 consumer, or Tier 3's 1536-d
     into a Tier 2 consumer, or EITHER into a 1024-d-expecting bridge consumer.
     Every embedding record carries dim, model, model_version, fs_native, fs_source,
     and the consumer DISPATCHES ON WIDTH BEFORE ANYTHING ELSE.
```

---

## 2. Tier 0 — Node Edge / Micro-DSP

**Claim:** "something happened at this absolute time, here is 172 bytes describing its shape and
20×4 bands of what the world sounded like around it." Nothing more. Tier 0 never names a source.

### 2.1 What is shipped and measured

| property | value | source |
|---|---|---|
| acquisition rate | **48 kHz**, the ceiling for this part | ✅ `firmware/gen_decim.py`: the PDM mic tops out at a 4.0 MHz clock and the ESP32 drives `fs*64` in `I2S_PDM_DSR_8S`, so fs ≤ 62.5 kHz; only /2 (32 k) and /3 (48 k) reach the mel banks' 16 kHz by an integer |
| decimation | /3, 257-tap int16 FIR | ✅ `firmware/gen_decim.py`, regenerated and diffed in CI (`generated-headers` job) |
| passband ripple | ≤ **0.08 dB** over 62.5–7812.5 Hz | ✅ measured on the **quantised** taps |
| worst alias fold | **−63.4 dB** | ✅ ditto; a 33-tap halfband measured −7.7 dB and was rejected |
| filter group delay | 224 samples = **4.67 ms** | ✅ ditto — this is a fixed, known bias on every Tier 0 timestamp |
| sketch | 20 bands × 8 frames, **172 B**, `[300, 20000]` Hz, event-gated | ✅ `hear/sketch.py`, golden vectors both sides |
| scene row | 20 bands × 4, `[62.5, 7812.5]` Hz, **ungated, every 1.024 s** | ✅ `firmware/gen_mel_scene.py`; 82,225 rows pooled at the 2026-09-09 ledger |
| clip | 5.0 s WAV, 1.0 s pre-trigger + 4.0 s post, 48 kHz 16-bit mono, **480,044 B** | ✅ `docs/clip-pipeline.md` |
| retrospective ring | **60 s**, addressable = ring − 16 s overwrite guard | ✅ `/audio`: `cap_samples` 3,840,000, `addressable_samples` 3,584,000 |
| onset timing | constant-fraction at `ONSET_FRAC = 0.20`, sketch start walked back one hop (`SKETCH_BACK_S = 0.004`) | ✅ `hear/node/detect.py`; nested grouped CV over 228 labelled events: peak 0.9634, back ≤2 ms 0.9746, back ≤25 ms 0.9443 |
| retrigger policy | `RETRIGGER_S = 0.060`, `GUARD_S = 0.025`, `REARM_FRAC = 0.35`; retriggers are **reported, not suppressed** | ✅ `hear/node/detect.py` |
| clock | GPS-PPS, tAcc **24–28 ns**, 0 glitches, PPS spread **6–9 µs** | ✅ 2026-09-09 ledger |

### 2.2 What the shipped gate gets wrong, and why Tier 0 needs a second detector

The only thing that produces a sketch is a **broadband amplitude gate**. `acoustic-stack.md` §6.2
measures the consequence: all 48 in-run detections of a 12.081 h capture peaked in mel band 0
(312.5–500 Hz), which exceeded the mean of the four bands above 4 kHz by a **median 24.6 dB**. Low
frequency rumble sets the envelope, and a sustained tonal source raises the mean without producing
the peak the gate looks for. **Birds, insects and aircraft never produce a sketch**, so no tier
above ever sees them, and the symptom is a low event rate and a green pipeline.

### 2.3 Tier 0 design work — NOT BUILT, and named so it is not mistaken for capability

⚠️Each item below returns **zero matches** across this tree today (`grep -ril 'sta_lta|stalta|
beamform|ice40'` → nothing). They are the tier's roadmap, not its inventory.

**0-A. STA/LTA onset, alongside the amplitude gate, not replacing it.** The classical
short-term/long-term average ratio `STA(τ_s)/LTA(τ_l)` is scale-free: it fires on a *change* in
the envelope rather than on an absolute level, which is precisely the failure mode §6.1 of
`acoustic-stack.md` documents for an absolute-dB model on an uncalibrated fleet (mach's ambient is
**3.04 dB** above nyquist's on identical hardware, 41 % of the model's entire p=0.1→p=0.9 range).
Two counters per band and one divide per hop — affordable on the S3. ⛔ Its per-target recall is
UNMEASURED and it must be fitted from an envelope distribution on **that node's own quiet
capture**, never from a fleet median, and never validated on the samples it was derived from.

**0-B. `esp-dsp` vector FFT.** The S3 has 128-bit SIMD and `esp-dsp` ships an assembly-optimised
radix-2/4 FFT. The mel frontend today is a fixed-point filterbank; an FFT frontend buys spectral
flatness and envelope autocorrelation, which is what `modules/bioacoustic/detect.py`'s `TonalGate`
(51 tests, complete, **no caller**) needs to run on-node instead of centrally. ⛔ Cycle cost,
heap cost and the effect on `audio_pump()` starvation are all UNMEASURED. The starvation risk is
real and already measured elsewhere: a `/sd` transfer with no `audio_pump()` in its write loop
costs roughly 100 % audio loss for its duration.

**0-C. iCE40 delay-and-sum beamforming.** A sub-$10 FPGA can delay-and-sum N PDM streams at the
bit-clock and hand the S3 one steered channel per look direction. ⛔ **There is no such board and
no such firmware.** More decisively, `hear/nodeclass.py` has **no mic-spacing or aperture field at
all** and `can_bear()` is literally `mic_count >= 2`, so a beamformer's aliasing ceiling (`c/2d`)
cannot even be stated for a hypothetical node. The registry change is a prerequisite for the
hardware, not a follow-up to it. This is S5 work.

### 2.4 Tier 0 budget

| resource | value | basis |
|---|---|---|
| CPU, steady state | mel + gate on the acquisition core, continuous | ✅ shipped and stable at 48 kHz |
| added latency | **4.67 ms** filter group delay + ≤4 ms onset walk-back | ✅ computed from the shipped constants |
| RAM | 60 s ring = 3,840,000 samples × 2 B = **7.32 MiB** in PSRAM | ✅ `/audio` `cap_samples` |
| SD | `/clips` rolling cache, firmware FIFO tracks newest **128** names | ✅ `docs/clip-pipeline.md` |
| STA/LTA add | ⚠️ 2 accumulators × 20 bands, < 1 KiB, ASSUMPTION |
| vector FFT add | ⛔ UNMEASURED |

---

## 3. Tier 1 — Ingest Privacy & Compliance

**Claim:** "no clip that contains human speech survived this boundary, and here is the receipt that
says so." Tier 1 is the only tier whose product is a **deletion**.

⛔ **Nothing in this tier is built.** `grep -ril 'silero|\bvad\b'` returns nothing in this tree.
The entire section is a contract to build against.

### 3.1 Why it sits between Tier 0 and Tier 2, and nowhere else

A 5 s clip at 48 kHz is intelligible speech. It is fetched off an SD card over plain HTTP on a
property with neighbours, landed on a PVC, and — once Tier 2 writes an embedding — becomes
effectively permanent, because `clips.prune()` deletes the **WAV** at a 2 GiB cap while the
embedding is kept precisely so later clustering is possible. **Any purge that runs after Tier 2
purges the audio and keeps a 960-d representation of the speech.** The gate must therefore run
before the first durable write, not before the first *audio* write.

### 3.2 The model

| property | value | status |
|---|---|---|
| model | Silero VAD v5, ONNX export | design choice |
| licence | MIT | ⛔ **UNVERIFIED IN THIS REPO** — gate G1.1 below |
| input | 16 kHz mono (the decimated Tier 0 rate is already 16 kHz-clean), 30 ms frames | design |
| size | ⚠️ ~2 MB ASSUMPTION; the pinned artefact's byte count and sha256 are UNMEASURED until the export lands |
| runtime | `onnxruntime` only — the same dependency Tier 2 already carries, so the pod gains no new supply chain | design |
| per-clip cost | ⚠️ ASSUMPTION: ≈1 ms per 30 ms frame single-thread ⇒ ~170 ms per 5 s clip. ⛔ UNMEASURED on this hardware |

Silero is chosen over WebRTC VAD because WebRTC's energy-and-GMM design has the same absolute-level
dependence that §6.1 already proved hostile on this uncalibrated fleet, and over a full ASR model
because **the answer must be a boolean, and a model that can transcribe is a model that can leak.**
Tier 1 must be structurally incapable of producing text.

### 3.3 The purge contract

1. **Decide before durability.** The VAD runs on the in-memory clip. Nothing is written to
   `clips/<day>/<node>/` until the verdict exists.
2. **Speech ⇒ destroy the samples, immediately and irrecoverably.** No quarantine directory, no
   "review queue", no low-rate copy. A quarantine is a retention policy with an apology attached.
3. **Keep the receipt, never the evidence.** The receipt is append-only and contains: `clip_id`
   (content address), `node`, `utc_us`, `duration_s`, `model`, `model_version`, `model_sha256`,
   `threshold`, `speech_prob`, `verdict ∈ {purged, passed}`, `purged_bytes`, `receipt_sha256`, and
   the previous receipt's hash so the log is chained and a deletion from it is detectable.
4. **A receipt is not a transcript.** No text, no phonemes, no timestamps *within* the clip finer
   than the clip itself, no embedding of a purged clip.
5. **Fail closed.** If the VAD errors, the model hash mismatches, or the runtime is missing, the
   clip is **purged and the receipt says `verdict=purged, reason=gate_unavailable`.** A privacy
   gate that fails open is not a gate. This inverts the rule the rest of the platform follows —
   elsewhere this repo refuses to filter at ingest because it has twice lost data to a filter
   chosen too early (`hear/pool.py:721`) — and the inversion is deliberate and stated here so the
   next reader does not "fix" it into consistency.
6. **The counter is published.** `purged_total`, `passed_total`, `gate_unavailable_total` per node
   per day, beside the existing lane instruments. Zero purges over a week is a **finding to
   investigate**, not a success: it most likely means the gate is not wired.

### 3.4 Tier 1 budget

| resource | value |
|---|---|
| latency | ⚠️ ~170 ms/clip ASSUMPTION, entirely inside the drain's existing per-clip fetch cost |
| memory | ⚠️ ~2 MB model + one 5 s float buffer (960 kB) ASSUMPTION |
| storage | **negative** — this tier only ever deletes |
| failure mode | purge (fail closed) |

---

## 4. Tier 2 — Coarse Event Triage

**Claim:** "this clip contains something from the AudioSet ontology, with this score distribution,
and here is a 960-d embedding of it." ✅**SHIPPED** — `tools/hear_tag.py`, `deploy/k8s/hear-tag.yaml`.

### 4.1 The model, and why it is not YAMNet

| | YAMNet (removed 2026-09-10) | **EfficientAT `mn10_as`** |
|---|---|---|
| AudioSet mAP | 0.306 | **0.471** |
| params | 3.7 M | 4.88 M |
| GMACs | — | 0.54 |
| classes | 521 | **527** (full ontology) |
| embedding | 1024 | **960** |
| licence | Apache-2.0 | **MIT** |
| runtime | ai-edge-litert | onnxruntime |
| per clip | 12 ms | **48 ms** incl. resample |

✅ All rows measured; see `acoustic-stack.md` §6.2b. 48 ms against 12 ms is 0.03 % duty on one core
at ~20 events/hour; the shortlist's rule was *pick on accuracy and licence, ignore latency*.

**960 is load-bearing.** YAMNet's 1024 and BirdNET V2.4's 1024 are mutually confusable in
`tools/hear_bridge.py`. 960 is neither, and every row carries `embedding_dim`.

### 4.2 Supply chain — pinned at both ends, verified before every load

```
mn10_as_mAP_471.pt   0bd7dc24…   19,708,753 B    upstream release v0.0.1, MIT
  → tools/export_mn10_onnx.py                    in this repo, byte-reproducible
    → mn10_as.onnx   1b718a05…   24,016,402 B    verified before every load
```

✅ The mel frontend is baked into the graph (reproducing `AugmentMelSTFT` to **8.4e-05** max abs
error), `torch.stft` is replaced by a DFT `conv1d`, and the exported graph reproduces PyTorch to
**7.6e-06** on logits and **1.0e-06** on embeddings. The pod needs onnxruntime and numpy — no
torch, no torchaudio, no librosa. **The job verifies and refuses; it never builds, and it never
downloads a model it cannot hash** (`--verify-weights` exits 2 unless both shas match).

### 4.3 The two rules Tier 2 must keep

- **Resample only from 48 kHz.** `hear/resample.py` decimates 48 → 32 (L=2, M=3), so every band the
  model reads is measurement, never interpolation. A header rate that does not snap to 48 kHz is
  refused into a **counted** bucket.
- **Store the whole score picture, never a hard top-1.** Every class above `SCORE_FLOOR`, plus
  `max_unstored_score` so the discarded tail is a number rather than an absence.

### 4.4 What Tier 2 structurally cannot do

AudioSet has `Bird`, `Owl`, `Hoot`, `Chirp` and stops. 527 classes cannot separate Barred Owl from
Great Horned Owl **no matter how good the backbone gets**. That is the entire reason Tier 3 exists,
and it is an ontology limit, not a quality limit — a better AudioSet model does not close it.

### 4.5 Tier 2 budget

| resource | value | basis |
|---|---|---|
| latency | **48 ms/clip** incl. resample | ✅ measured |
| CPU | 0.54 GMACs/clip; 0.03 % of one core at 20 events/h | ✅ arithmetic on measured inputs |
| RSS | ⚠️ ONNX graph 24 MB + runtime arenas; ASSUMPTION ~300 MB, ⛔ the pod's actual RSS ceiling is UNMEASURED |
| storage | 527 scores above floor + 960 × 4 B embedding ≈ **4 kB/clip** ⚠️ |
| GPU | **none** — Tier 2 is deliberately CPU-only so it survives the GPU contention in §S1 gate 3 |

---

## 5. Tier 3 — Bioacoustic & Ecological Foundation

**Claim:** "here is a 1536-d bioacoustic representation of this window, and — where a site-fitted
head exists and was validated on held-out site data — a species posterior." ⚠️**GATED.** The code
path is designed, the GPU is contended, and the pull trigger is not yet inside the ring window.

### 5.1 Perch 2.0 — the backbone, chosen on licence

> The measured survey behind this table — architecture, dataset provenance, training objectives,
> the documented non-avian failures, the alternatives that were refused and the codec question
> for spooling audio to it: **`docs/survey-bioacoustic-foundation-models.md`**.

| property | value |
|---|---|
| licence | **Apache-2.0** ✅ |
| input | 32 kHz, 5 s window (160,000 samples) |
| trunk | EfficientNet-B3, ~12 M params |
| embedding | **1536-d** |
| head | 1536 × 14,795 classifier — **dropped**, ~91 MB fp32, larger than the trunk |

**BirdNET V2.4 is comparable in the few-shot benchmark** ("Perch and BirdNET 2.3 obtain similar
performance") **and is refused on licence**: its models are CC BY-NC-SA 4.0, and ShareAlike
plausibly follows every probe onto a fleet that also ships an Android APK. **Take BirdNET's
answers, not its weights** — via puc's existing BirdWeather station 4066 feed, which makes it an
*oracle* (a label source) rather than a dependency.

⚠️**Exclude Perch's insect head by name in code.** On 19 UK Orthoptera species it measured macro
F1 **0.071** and macro AUC **0.454 — below chance**. "Insecta appears in the training-data table"
is not evidence the head works.

### 5.2 BioLingual — the zero-shot query surface, and it is not cleared

BioLingual is a CLAP-style contrastive audio–text model for bioacoustics: it accepts a free-text
query ("a barred owl calling") and scores audio against it without a fitted head. That is exactly
the right shape for a site with **zero operator labels** and a corpus nobody has listened to, and
it is why it is in the architecture at all.

⛔ **It is not cleared for use.** Three things are UNMEASURED and each independently blocks it:

1. **Licence of the weights**, distinctly from the licence of the code. Gate G3.2.
2. **Embedding width and native rate**, which must be pinned into the width-dispatch contract
   before a single record is written, or it becomes the third confusable width in the bridge.
3. **Whether zero-shot text scores are calibrated at all** on a 3.5-acre wooded site at 48 kHz-
   derived audio. A zero-shot score that is uncalibrated is a ranking, not a probability, and it
   must be published as a ranking.

Until all three close, BioLingual is a **research lane that may not write the pool**, by the same
rule §3.4 of `acoustic-stack.md` applies to `hear-score`.

### 5.3 Specialised species classifiers

Small heads fitted **on this site's own labelled data**, over frozen Perch embeddings, one per
target. The label source is the BirdWeather oracle plus operator tags — the same
better-instrumented-sensor-labels-a-worse-one pattern as `dama-rtk-labeler`. ⛔ How many BirdWeather
detections exist, over what span, at what confidence, is **UNMEASURED**, and that single number
decides whether a site bird probe is trainable this month or next year. It is one API pull.

**Rejected here, with the measurement:** AST / PaSST / BEATs, at 87–90 M params for ~1 mAP over
`mn10_as` at 4.88 M. The decisive measurement is not size: AudioSet-trained embeddings **lose** to
bird-trained embeddings on all six bioacoustic datasets tested, bats and marine mammals included
(VGGish scored 0.04 top-1 on marine mammals). Three of this site's four named targets are
bioacoustic. And **PULSE as shipped**: 96 kHz UK field recordings — 2× the fleet's best rate — with
a weights URL that is a literal `XXX` placeholder. Take the method, not the artefact.

### 5.4 Tier 3 budget

| resource | value | basis |
|---|---|---|
| input window | 5 s / 160,000 samples @32 kHz | ✅ model card |
| trunk | ~12 M params | ✅ |
| VRAM | ⚠️ size against the **2080 Ti's ~10.7 GB and pin the device**. `nvidia.com/gpu: 32` allocatable is `pattern:"*"` × `timeSlicing.replicas:16` over **two physical cards**; time-slicing gives no VRAM isolation and no affinity, and the 4070 Ti has ~800 MiB free under vLLM. "31 free replicas" is a scheduling count, not headroom ✅ measured |
| runtime | ⛔ the 2080 Ti is Turing/sm_75 and the model card names TF 2.20.rc0; an ONNX mirror exists with **fidelity unvalidated**. One container run closes this |
| latency | ⛔ UNMEASURED on this hardware |
| storage | 1536 × 4 B = **6 kB/window** ⚠️ |

---

## 6. Tier 4 — Multi-Sensor Spatial & Replay

**Claim:** "this event was consistent with a point source here, to this DOP, on this survey — or it
was not, and here is the reason it did not solve." ⚠️**Partly shipped and honestly funnelled:**
`hear/solve/{point,shockwave,placement,consistency,soundspeed,calibrate}.py`,
`hear/backend/{survey,associate,pipeline}.py`, driven by `tools/hear_tdoa.py` and
`tools/hear_spatial.py`.

**The product is the funnel, not the events.** Every pool row reaches exactly one terminal verdict
and the run succeeds when it can explain why zero solved. `n_solved == 0` is exit 0; a run that
cannot account for its own inputs is exit 2. ✅ On the corpus this was written against the honest
answer is zero: across 30 one-minute bins in which all three surveyed nodes were simultaneously
detecting, there were **0** three-node coincidences satisfying their own pairwise geometry at zero
margin.

### 6.1 Sub-microsecond TDoA — a target, and the two things between here and it

The timing chain is good: ✅ tAcc **24–28 ns**, 0 glitches. The *aggregate* is not there yet: ✅ PPS
spread across nodes is **6–9 µs**, which is 2.1–3.1 mm of path at `c ≈ 345 m/s` — good, and an order
of magnitude short of "sub-microsecond". Stating the gap precisely:

1. **Geometry dominates, not timing.** ✅ Node GNSS positions disagree by **4–17 m** under canopy.
   `hear/solve/placement.py` measured the consequence: shifting a track ±6 m past three
   **same-side** nodes changed the TDoAs by **0.000 ms**, and past straddling nodes by up to
   **117 ms**. A nanosecond clock on a 17 m position is a decorated guess. **No TDoA product ships
   before the RTK survey** (S4).
2. **The unmeasured group delays are per-sensor constants, and must be omitted, not zeroed.** Tier
   0's FIR contributes a known **4.67 ms**; ⛔ hugbot's ESP acoustic→ADC group delay is UNMEASURED,
   and a zero there is a fabricated 0 µs bias that looks like data.

### 6.2 The hugbot acoustic locator

✅ hugbot is the **only co-located multi-mic array** on the property: 38.1 mm intra-mic spacing,
therefore a spatial-aliasing ceiling of `c/2d` = **4501 Hz**, and therefore the only bearing that
can be cross-checked against a TDoA solution. It **emits, never serves** — battery robot, flaky
wifi, a Pi at 84 °C with a `CPUQuota` on every audio unit.

Three live constraints, all measured, all still open:

- ⚠️ The deployed `hugbot-esp-doa.service` runs `--f-hi 20000` against that 4501 Hz limit — **4.4×
  past the aliasing ceiling**, which returns a confident bearing to a mirror image.
- ⚠️ `hear/nodeclass.py` has **no hugbot class**, no mic-spacing field, and `can_bear()` is
  `mic_count >= 2`. It must **refuse when spacing is unset** rather than returning `True`.
- ⚠️ hugbot rows carry `source='phone'` in the pool because `source` is an argument to `key()`, the
  content address. **Document the consequence; do not "fix" it** — re-keying duplicates every
  record in the MQTT corpus.

The association cross-check `|dt_i − dt_j| ≤ 2d/c` only discriminates below ~0.35 m spacing, so ✅ it
works **inside hugbot's array and nowhere else on the property**.

### 6.3 Atmospheric propagation correction — design, and the arithmetic that sizes it

✅ `hear/solve/soundspeed.py` states the governing constant: `c(T) = 331.3 + 0.606·T`, so
**0.606 m/s per °C is 0.176 % of c**, and recovering T to ±1 °C over a 36 m path (105 ms) needs that
path timed to **±185 µs**. Every range this project quotes is a time multiplied by c, and ✅ c was
never measured — `c = 345.238 m/s` (23 °C, from the operator's recollection) is a free parameter
propagated into every bearing, every TDoA bound and every shockwave fit.

⚠️ **Three receivers and one unknown source cannot determine c.** N receivers on a shared clock give
N−1 independent delays per event; an unknown ground source costs 2 unknowns plus the shared c.
Three ground receivers: 2 equations, 3 unknowns, under-determined by exactly one **for any number
of events** — extra rounds from the same spot repeat the same two equations.

⛔ **Ray-traced propagation is NOT BUILT** and is the largest single piece of design in this
document. What it would add over the constant-c model: a vertical temperature and wind gradient
bends rays, so the straight-line assumption is wrong by an amount that grows with range and with
the gradient; over a 119 m site the error is ⚠️ small compared to the 4–17 m GNSS error and
therefore **explicitly not worth building until after the RTK survey**. Sequencing it earlier would
be correcting a millimetre term while a metre term is open. The prerequisites, in order: RTK survey
→ a measured c from a surveyed baseline (`speed_from_baseline()`, needs a clock-synced receiver at
the source) → a site temperature/wind profile → then ray tracing.

### 6.4 3D bearing extraction

⛔ Not available from the xiao nodes: they are single-mic and **structurally cannot bear**. The only
in-principle second array is puc (two mics on a shared PDM clock, which makes a *relative* bearing
independent of its ±1.1–3.1 m NTP bound) — but it has no ring, no SD, no `/audio`, needs firmware
nobody has written, and its **aperture appears nowhere in this repo**, so its aliasing ceiling
cannot be stated. S5.

### 6.5 Tier 4 budget

| resource | value | basis |
|---|---|---|
| solve cost | CPU, seconds per corpus pass | ✅ `tools/hear_tdoa.py` runs over the whole pool |
| timing budget | ✅ 24–28 ns tAcc; ✅ 6–9 µs PPS spread; ✅ 4.67 ms known FIR delay; ⛔ hugbot ADC delay |
| geometry budget | ✅ 4–17 m GNSS, **the dominant term**; RTK survey is the fix |
| c budget | 0.176 %/°C; ✅ c itself unmeasured |
| traversal | sound crosses the site's 119 m square equivalent in **347 ms** ✅ |

---

## 7. Latency and memory budget, whole stack

Per event, from acoustic arrival to a spatial verdict. ✅ measured, ⚠️ assumption, ⛔ unmeasured.

| stage | latency | memory | note |
|---|---|---|---|
| T0 FIR group delay | **4.67 ms** ✅ | 7.32 MiB ring ✅ | fixed, known, correctable |
| T0 onset walk-back | ≤ 4 ms ✅ | — | `SKETCH_BACK_S` |
| T0 → drain: `/detections` poll | ~**35 ms** per poll ✅ (7.3 s degraded during a large transfer ✅) | ~600 B/event ✅ | this is the pull trigger |
| T0 → drain: pool round-trip | **900 s** cadence + 53–78 s job wall time ✅ | — | ⚠️**15–22× too late** for a ~44–64 s addressable ring |
| T1 VAD | ~170 ms/clip ⚠️ | ~3 MB ⚠️ | ⛔ unmeasured |
| T2 mn10_as | **48 ms**/clip ✅ | ~300 MB RSS ⚠️ | includes 48→32 kHz resample |
| T3 Perch | ⛔ | ⚠️ size to 2080 Ti ~10.7 GB, **pin the device** ✅ | ⛔ runtime on sm_75 unverified |
| T4 solve | seconds/pass ⚠️ | pool-resident ⚠️ | funnel, not stream |
| **storage/clip** | — | 480,044 B WAV ✅ + ~4 kB T2 ⚠️ + 6 kB T3 ⚠️ | WAVs prune at a 2 GiB cap; embeddings persist |

**The single most important line in this table is the fourth.** A verdict computed from the pool
cannot reach back into a ring that has already overwritten itself. The pull trigger must be driven
from `/detections` — the live ring in RAM — with `hear-drain` left doing archival only. This is
S1 gate 1 and it is not a formality.

---

## 8. Licence compatibility matrix

The fleet ships an **Android APK** as well as server code. That single fact is what makes licence a
first-class selection criterion here rather than a footnote: a copyleft or non-commercial term that
follows a probe onto a distributed application is a different risk from one that stays on a
private server.

| tier | artefact | code licence | **weights** licence | distributable in the APK? | verdict |
|---|---|---|---|---|---|
| 0 | `firmware/hear_node`, `gen_decim.py` | this repo | n/a | yes | ✅ ships |
| 0 | `model_sketch_15.json` (172 B logistic) | this repo | this repo, fitted here | yes | ✅ ships |
| 1 | Silero VAD v5 | MIT ⛔ verify | MIT ⛔ verify | ⛔ blocked on G1.1 | design |
| 2 | **EfficientAT `mn10_as`** | **MIT** ✅ | **MIT** ✅ | **yes** | ✅ **shipped** |
| 3 | **Perch 2.0** | **Apache-2.0** ✅ | **Apache-2.0** ✅ | yes (server-side today) | ⚠️ gated |
| 3 | BirdNET V2.4 | — | **CC BY-NC-SA 4.0** ❌ | **no** | ❌ **weights refused; answers taken via the BirdWeather oracle** |
| 3 | BioLingual | ⛔ unverified | ⛔ unverified | ⛔ | blocked on G3.2 |
| 3 | PULSE | — | weights URL is a literal `XXX` | n/a | ❌ method only |
| 2 | YAMNet (removed) | Apache-2.0 | Apache-2.0 | yes | ❌ refused on **measurement**, not licence (§8 of `acoustic-stack.md`) |
| 4 | `hear/solve/*`, `hear/backend/*` | this repo | n/a | yes | ✅ ships |

### 8.1 The four weight-distribution rules

1. **A permissive code licence does not license the weights.** They are separate artefacts with
   separate terms, and this table has a separate column for exactly that reason. Any new model
   enters with **both** cells filled or it does not enter.
2. **Non-commercial or ShareAlike weights never enter the tree**, not even behind a flag, not even
   for evaluation on a branch — because a branch is a distribution and the APK is a distribution.
   Their *outputs*, obtained from a third-party service the operator already runs (BirdWeather
   4066), are labels, and labels are fine.
3. **Every weight is hash-pinned and verified before load**, and the job refuses rather than
   downloads. Tier 2 is the reference implementation: two shas, an in-repo byte-reproducible
   exporter between them, `--verify-weights` exiting 2 on mismatch.
4. **CC-BY weights, if ever admitted, carry an attribution obligation that must land in the APK's
   licence screen and in `deploy/images/README.md` in the same change** that admits them. An
   attribution term discovered after shipping is a compliance incident, not a TODO.

---

## 9. Verification gates

A tier may not consume the tier below until that tier's gates are green. Each gate names the
command, and each is designed to **fail loudly on a wrong implementation** rather than read
permanent green — the failure mode §0.2 of `acoustic-stack.md` documents, where a check that pairs
`/audio`'s span with `cap_samples` produces a ratio that can never reach 1.02 and therefore *cannot
fail*.

### G0 — Tier 0

| gate | assertion | command |
|---|---|---|
| G0.1 | every generated header matches its generator, byte for byte | `python firmware/gen_decim.py > firmware/hear_node/decim.h && git diff --quiet` (CI: `generated-headers`) |
| G0.2 | golden sketch vectors agree on both sides of the wire | `python -m pytest -q tests/ -k sketch` |
| G0.3 | ring wall span reads within **~0.003 of 1.000** on a known-healthy node, **each endpoint divided by its own sample count** (`/status`→`cap_samples`, `/audio`→`addressable_samples`) | self-test shipped with the check; a wrong-denominator implementation fails immediately |
| G0.4 | "no scene file of any name" is a **failure**, never UNKNOWN | `hear-drain-check` |
| G0.5 ⛔ | STA/LTA per-target recall, fitted on that node's own quiet capture and validated on held-out data | not built |

### G1 — Tier 1

| gate | assertion |
|---|---|
| G1.1 ⛔ | the Silero v5 **weights** licence is read from the release artefact and recorded in §8 with a URL and a date — not inferred from the repo's code licence |
| G1.2 ⛔ | the ONNX artefact's byte count and sha256 are pinned, and the loader **refuses** a mismatch (Tier 2's `--verify-weights` is the reference) |
| G1.3 ⛔ | a positive-control clip containing known speech is **purged**, and the pool contains no WAV, no embedding and no score for it |
| G1.4 ⛔ | with the runtime removed from the image, the clip is **purged** with `reason=gate_unavailable` — fail closed, proven by removal, not by reading the code |
| G1.5 ⛔ | the receipt chain verifies end to end and contains no text field |
| G1.6 ⛔ | `purged_total` is published per node per day, and a week at zero raises an investigation |

### G2 — Tier 2 ✅ all green today

| gate | assertion | command |
|---|---|---|
| G2.1 | both weight shas match, or exit 2 | `python3 tools/hear_tag.py --model-dir … --verify-weights` |
| G2.2 | the exported graph reproduces PyTorch to 7.6e-06 (logits) / 1.0e-06 (embeddings) | `tools/export_mn10_onnx.py` self-check |
| G2.3 | a header rate that does not snap to 48 kHz is refused into a **counted** bucket | `python3 tools/hear_tag.py --census` |
| G2.4 | the lane is flowing | `python3 tools/hear_tag.py --pool … --check` (exit 1 if not) |
| G2.5 | every record carries `embedding_dim` and no consumer accepts a width it did not dispatch on | `tests/test_hear_tag.py` |

### G3 — Tier 3

| gate | assertion |
|---|---|
| G3.1 ⚠️ | **S1 gate 1**: the pull trigger runs off `/detections` (cursor-v1 paging, explicit `gap.kind = overrun\|reboot`), inside the ring's addressable window, with a measured end-to-end latency stated **against that window** |
| G3.2 ⛔ | BioLingual's weights licence, embedding width and native rate are recorded in §8 and in the width-dispatch contract **before** the first record is written |
| G3.3 ⛔ | **S1 gate 3**: the chosen Perch runtime loads on the 2080 Ti (sm_75) in one container run, and the pod is **pinned** to a named device — not scheduled against a time-sliced `"*"` pattern |
| G3.4 | Perch's insect head is excluded **by name in code**, with the 0.071 F1 / 0.454 AUC measurement cited at the exclusion |
| G3.5 ⛔ | no Tier 3 record reaches a 1024-d-expecting consumer; width dispatch happens before anything else |
| G3.6 ⛔ | a site species head is validated on **held-out site data**, never on the BirdWeather detections it was fitted from |

### G4 — Tier 4

| gate | assertion | command |
|---|---|---|
| G4.1 | the run accounts for **every** input row; `n_solved == 0` is exit 0, an unexplained input is exit 2 | `python3 tools/hear_tdoa.py --pool … --check` |
| G4.2 | a coincidence count is reported against its **shuffle null**, so it has a referent | `--census` |
| G4.3 ⚠️ | the RTK survey has landed and per-node σ is published; no position is quoted from a 4–17 m GNSS fix | S4 |
| G4.4 | `can_bear()` **refuses when mic spacing is unset**, and no bearing is emitted above `c/2d` | `hear/nodeclass.py` + `tests/` |
| G4.5 ⛔ | hugbot's acoustic→ADC group delay is measured against an external reference, or its arrivals are **omitted** — never zeroed |
| G4.6 | every quoted range states the c it used and whether c was measured or assumed | `hear/solve/soundspeed.py` |

---

## 10. What this architecture refuses to claim

- **That the fleet detects birds, insects or aircraft today.** Tier 0's gate is an amplitude
  detector behind band-0 masking; ⛔ per-target recall for those three is UNMEASURED and believed
  near zero. Tier 0 is **the supersonic pull trigger**, not fleet-wide triage.
- **That any Tier 0 verdict has a calibrated level term.** ✅ mach's ambient sits 3.04 dB above
  nyquist's on identical hardware, against a model whose entire p=0.1→p=0.9 range is **7.37 dB** of
  `ref_db`. Either estimate a per-node offset from a co-heard event, or run shape-only at 0.9450,
  or refuse to score uncalibrated nodes — but publish which.
- **That Tier 3 is running.** It is gated on a pull trigger, a GPU pin and a runtime check.
- **That Tier 4 produces positions.** ✅ It has produced **zero** solved three-node coincidences on
  the corpus it was written against, and it says so.
- **That privacy is protected today.** ⛔ Tier 1 does not exist. Clips containing speech are fetched,
  landed and tagged like any other clip. This is the highest-priority unbuilt item in this document.

---

## 11. Build order, and why this one

1. **Tier 0 repairs** (S0.1–S0.3) — the scene lane, the drain cadence, the ConfigMap generalisation.
   Nothing new; these are repairs that unblock everything above.
2. **Tier 1** — because every day it does not exist is another day of clips landing unfiltered, and
   because it is the only tier whose cost is *negative* storage. It needs no GPU, no survey and no
   phone release.
3. **Tier 3 gates** (S1 gate 1: `/detections`-driven pull; gate 3: the GPU pin) — these are gates,
   not models, and they are what make Tier 3 possible at all.
4. **Labels in parallel** (S2) — the BirdWeather harvest is the single highest-leverage item in
   `acoustic-stack.md`, and it costs one API pull to size.
5. **Tier 4 after the RTK survey** (S4) — geometry beats timing by two orders of magnitude here, so
   no spatial product ships before the survey, and no ray-traced propagation model is built before
   the metre-scale term is closed.

---

## 12. Cross-references

| topic | document |
|---|---|
| measurements, live failures, stages S0–S5 | `docs/acoustic-stack.md` |
| what the clip lane does and does **not** establish | `docs/clip-pipeline.md` |
| model lifecycle, promotion and rollback | `docs/ml-lifecycle.md` |
| PPS, tAcc and the timing ladder | `docs/timing.md` |
| node hardware and the microphone's real bandwidth | `docs/node-hardware.md`, `docs/esp32s3-lora-node.md` |
| image build, pinning and the licence screen | `deploy/images/README.md` |
| retention and audit obligations Tier 1 must satisfy | `docs/data-governance.md` |
